import asyncio
import base64
import hashlib
import io
import time
import zipfile
from collections.abc import Mapping
from dataclasses import replace
from itertools import groupby
from pathlib import Path
from typing import Any, Literal

import aiofiles
import httpx
import orjson
from sanic.log import logger
from sanic.request.form import File
from tortoise import Tortoise, timezone
from tortoise.expressions import F, Q, RawSQL
from tortoise.transactions import atomic, in_transaction

from app.core.config import KaloscopeConfig
from app.core.constants import ENCODING
from app.core.cookiejar import SQLiteCookieJar
from app.core.exceptions import (
    ErrorCode,
    ForbiddenException,
    KaloscopeException,
    NotFoundException,
)
from app.core.flow.context import AUTH_KEY, MANUAL_KEY, START_KEY
from app.core.flow.diff import changed_paths
from app.core.flow.fetcher import fetch_origin, save_icon
from app.core.flow.nodes.base import Node, NodeGroup, NodeSchema, start_config
from app.core.flow.validation import (
    comparison_document,
    compress_document,
    validate_document,
)
from app.models.flow import (
    FlowDraftRevision,
    FlowGraph,
    FlowInstance,
    FlowJob,
    FlowLog,
    FlowRepository,
    FlowRunReceipt,
    FlowTemplate,
    FlowTrigger,
    FlowVariable,
    GraphBasics,
    GraphCategory,
    GraphChange,
    GraphDocument,
    GraphDraftCreate,
    GraphPreview,
    GraphQuery,
    GraphRef,
    GraphState,
    GraphValidation,
    GraphWrite,
    JobQuery,
    JobState,
    JobUpsert,
    RepositoryAdd,
)
from app.models.user import (
    HistoryType,
    PermType,
    UserHistory,
    UserInfo,
    UserPermission,
    UserRole,
)
from app.services.base import BaseService
from app.utils import json
from app.utils.disk import delete_path


class FlowRepositoryService(BaseService[FlowRepository], model=FlowRepository):
    """The service class for all flow repository related operations."""

    GITHUB_PREFIX = "https://github.com/"
    GIT_SUFFIX = ".git"

    @classmethod
    async def add(cls, add: RepositoryAdd) -> FlowRepository:
        """Add a flow repository.

        Args:
            add: Repository to add.

        Returns:
            Added flow repository.
        """

        # extract the repository name
        repo_name = add.repo
        if repo_name.startswith(cls.GITHUB_PREFIX):
            repo_name = repo_name[len(cls.GITHUB_PREFIX) :]
        if repo_name.endswith(cls.GIT_SUFFIX):
            repo_name = repo_name[: -len(cls.GIT_SUFFIX)]
        repo = FlowRepository(repo_name=repo_name)

        # call the GitHub API to get the repository information
        # https://docs.github.com/en/rest?apiVersion=2022-11-28
        client: httpx.AsyncClient = cls.app_ctx().httpx
        url = f"https://api.github.com/repos/{repo_name}"
        try:
            headers = {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            response = await client.get(url, headers=headers)
            if response.status_code == 404:
                raise NotFoundException
            if response.status_code == 200:
                data = response.json()
                repo.repo_name = data.get("full_name", repo_name)
                repo.repo_url = data.get("clone_url")
                repo.repo_description = data.get("description")
                if owner := data.get("owner"):
                    repo.owner_name = owner.get("login")
                    repo.owner_url = owner.get("html_url")
                    repo.owner_avatar = owner.get("avatar_url")
        except httpx.RequestError:
            logger.error("An error occurred while requesting %s.", url, exc_info=True)

        # check if the repository already exists
        if _repo := await FlowRepository.get_or_none(repo_name=repo_name):
            if not repo.repo_url:
                # if the API request failed, return the existing repository
                await fetch_origin(_repo)
                return _repo
            repo.id = _repo.id

        # make sure the repository URL is set
        if not repo.repo_url:
            repo.repo_url = f"{cls.GITHUB_PREFIX}{repo_name}{cls.GIT_SUFFIX}"

        # save and synchronize the repository
        await repo.save(force_update=repo.id is not None)
        await fetch_origin(repo)
        return repo

    @classmethod
    async def delete(cls, id: int):
        """Delete a flow repository.

        Args:
            id: Flow repository ID.
        """
        repo = await FlowRepository.get(id=id)
        await repo.delete()
        # delete the repository directory according to filesystem trash mode
        repo_dir = Path(KaloscopeConfig.get_workspace("repositories"))
        repo_path = repo_dir / repo.repo_name
        if repo_path.exists() and repo_path.is_dir():
            delete_path(repo_path)


class FlowTemplateService(BaseService[FlowTemplate], model=FlowTemplate):
    """The service class for all flow template related operations."""

    @classmethod
    async def reference_template(cls, id: int, name: str) -> FlowGraph:
        """Reference a template to create a new flow graph.

        Args:
            id: Flow template ID.
            name: Name of the new flow graph.

        Returns:
            Created flow graph instance.
        """
        tmpl = await FlowTemplate.get(id=id)
        if not tmpl:
            raise NotFoundException

        if await FlowGraph.filter(name=name).count() > 0:
            raise KaloscopeException(ErrorCode.NAME_ALREADY_EXISTS)

        # create a non-editable flow graph
        return await FlowGraph.create(
            tmpl_id=tmpl.id,
            name=name,
            icon=tmpl.icon,
            description=tmpl.description,
            category=tmpl.category,
            revision=tmpl.revision,
            draft=tmpl.definition,
            state=GraphState.DRAFT,
            editable=False,
        )

    @classmethod
    async def copy_template(cls, id: int, name: str) -> FlowGraph:
        """Copy a template to create a new flow graph.

        Args:
            id: Flow template ID.
            name: Name of the new flow graph.

        Returns:
            Created flow graph instance.
        """
        tmpl = await FlowTemplate.get(id=id)
        if not tmpl:
            raise NotFoundException

        if await FlowGraph.filter(name=name).count() > 0:
            raise KaloscopeException(ErrorCode.NAME_ALREADY_EXISTS)

        # create an editable flow graph
        return await FlowGraph.create(
            name=name,
            icon=tmpl.icon,
            description=tmpl.description,
            category=tmpl.category,
            draft=tmpl.definition,
            state=GraphState.DRAFT,
            editable=True,
        )

    @classmethod
    async def get_newest(cls, tmpl: dict) -> dict | None:
        """Get newest template for the same repo and path.

        Args:
            tmpl: Serialized template dict.

        Returns:
            Serialized newest template dict, or `None` if no newer template exists.
        """
        newest = (
            await FlowTemplate.filter(
                repo_id=tmpl["repo"]["repo_name"],
                path=tmpl["path"],
                newest=True,
            )
            .exclude(id=tmpl["id"])
            .first()
        )
        if newest is None:
            return None
        return await cls.dump(newest)


class FlowGraphService(BaseService[FlowGraph], model=FlowGraph):
    """Manage workflows, their published configuration and access rules."""

    @classmethod
    async def indexer_auth(
        cls, graph_id: int, cookies: SQLiteCookieJar | None = None
    ) -> dict | None:
        """Read the existing indexer login variable or configured cookie.

        Args:
            graph_id: Indexer whose login state is requested.
            cookies: Application cookie jar, when available.

        Returns:
            Cached authentication or a cookie marker, otherwise `None`.
            Callers must check workflow access before exposing this state.
        """
        var = await FlowVariable.get_or_none(
            Q(graph_id=graph_id, key=AUTH_KEY)
            & (Q(expires__gte=time.time()) | Q(expires__isnull=True))
        )
        if var is not None and isinstance(var.value, dict) and "name" in var.value:
            return var.value
        config = (await start_config(graph_id, "auth"))["auth"] or {}
        cookie = config.get("cookie")
        if (
            cookies is not None
            and isinstance(cookie, dict)
            and (domain := cookie.get("domain"))
            and cookies.get_cookie(
                domain=domain, path=cookie.get("path"), name=cookie.get("name")
            )
            is not None
        ):
            return {"name": ""}
        return None

    @classmethod
    async def list_graphs(cls, query: GraphQuery, user: UserInfo) -> dict[str, Any]:
        """List workflows within the user's authorized scope.

        Args:
            query: Pagination and workflow filters.
            user: Authenticated user with permissions loaded by `authorize`.

        Returns:
            Workflow page without drafts, definitions, or logs.
        """
        queries = []
        if query.name:
            queries.append(Q(name__icontains=query.name))
        if query.state:
            queries.append(Q(state=query.state))
        if query.states:
            queries.append(Q(state__in=query.states))
        if query.category:
            queries.append(Q(category=query.category))
        if user.role != UserRole.ADMIN:
            queries.append(Q(id__in=user.perms.indexer_ids if user.perms else []))
        page = await FlowGraph.page(*queries, **query.page_params)
        result = await cls.dump_page(page, exclude={"draft", "definition", "logs"})
        for graph in result["items"]:
            graph["downloadable_search"] = (
                graph["category"] == GraphCategory.INDEXER
                and graph["state"] != GraphState.DRAFT
                and "search_start" in graph["node_types"]
                and not graph["only_preview"]
            )
            if tmpl := graph["tmpl"]:
                graph["newest_tmpl"] = await FlowTemplateService.get_newest(tmpl)
        return result

    @classmethod
    async def get_accessible(cls, id: int, user: UserInfo) -> FlowGraph:
        """Load a graph within the user's authorized workflow scope.

        Args:
            id: Graph ID to read.
            user: Authenticated user with permissions loaded by `authorize`.

        Returns:
            Graph accessible to the user.

        Raises:
            NotFoundException: If the graph does not exist.
            ForbiddenException: If the user cannot access the graph.
        """
        graph = await FlowGraph.get_or_none(id=id)
        if graph is None:
            raise NotFoundException
        if user.role != UserRole.ADMIN and (
            user.perms is None or id not in user.perms.indexer_ids
        ):
            raise ForbiddenException(ErrorCode.PERMISSION_DENIED)
        return graph

    @classmethod
    async def get_details(cls, id: int, user: UserInfo) -> dict[str, Any]:
        """Read a workflow and expand its saved node schemas.

        Args:
            id: Graph ID to read.
            user: Authenticated user with permissions loaded by `authorize`.

        Returns:
            Workflow metadata, draft, and published definition.

        Raises:
            NotFoundException: If the workflow does not exist.
            ForbiddenException: If the user cannot access the workflow.
        """
        graph = await cls.get_accessible(id, user)
        if isinstance(graph.draft, dict):
            graph.draft = Node.decompress(graph.draft)
        if isinstance(graph.definition, dict):
            graph.definition = Node.decompress(graph.definition)
        return await cls.dump(graph)

    @classmethod
    def get_node_schemas(
        cls,
        category: GraphCategory | None = None,
        node_type: str | None = None,
        *,
        include_examples: bool = True,
    ) -> dict[str, list[NodeSchema]]:
        """Group node schemas available to a workflow category.

        Args:
            category: Workflow category; `None` includes all categories.
            node_type: Exact node type; `None` includes all node types.
            include_examples: Include read-only example fields; `False` keeps the
                editable contract while leaving full examples available on demand.

        Returns:
            Schemas ordered and grouped as in the workflow designer.
        """
        schemas = [
            schema
            if include_examples
            else replace(
                schema,
                fields=[
                    field
                    for field in schema.fields
                    if not getattr(field, "readonly", False)
                ],
            )
            for schema in Node.schemas
            if (not category or not schema.categories or category in schema.categories)
            and (node_type is None or schema.node_type == node_type)
        ]
        groups = groupby(
            sorted(schemas, key=lambda s: (s.group.order, s.order)),
            key=lambda s: s.group,
        )
        return {group.name.lower(): list(items) for group, items in groups}

    @classmethod
    async def unique_name(cls, name: str) -> str:
        """Generate a unique name for the flow graph.

        Args:
            name: Base name to check for uniqueness.

        Returns:
            A unique name that does not conflict with existing flow graphs.
        """
        graphs = await FlowGraph.filter(name__startswith=name)
        if not graphs:
            return name
        # find the next available name
        names = [g.name for g in graphs]
        i = 1
        while True:
            new_name = f"{name} ({i})"
            if new_name not in names:
                return new_name
            i += 1

    @classmethod
    async def upsert(cls, obj: GraphBasics) -> FlowGraph:
        """Create or update a flow graph.

        Args:
            obj: Flow graph basics.

        Raises:
            KaloscopeException: If the name already exists.

        Returns:
            Flow graph instance.
        """
        # check if the name already exists
        filter = Q(name=obj.name)
        if obj.id:
            filter &= ~Q(id=obj.id)
        if await FlowGraph.filter(filter).count() > 0:
            raise KaloscopeException(ErrorCode.NAME_ALREADY_EXISTS)

        if obj.id:
            # update the flow graph
            await FlowGraph.filter(id=obj.id).update(
                name=obj.name,
                icon=await save_icon(obj.image) or obj.icon,
                description=obj.description,
            )
            graph = await FlowGraph.get(id=obj.id)
        else:
            # create the flow graph
            graph = await FlowGraph.create(
                name=obj.name,
                icon=await save_icon(obj.image),
                description=obj.description,
                editable=True,
                category=obj.category,
                state=GraphState.DRAFT,
                draft={"nodes": [], "edges": []},
            )

        return graph

    @classmethod
    def require_editor(cls, user: UserInfo) -> None:
        """Enforce workflow editing permissions for every service caller.

        Args:
            user: Current authenticated account.

        Raises:
            ForbiddenException: If the account cannot edit workflows.
        """
        if user.role != UserRole.ADMIN:
            raise ForbiddenException(ErrorCode.PERMISSION_DENIED)

    @classmethod
    async def validate_graph(
        cls, id: int, document: GraphDocument, user: UserInfo
    ) -> GraphValidation:
        """Check a proposed draft without saving it or executing any node.

        Args:
            id: Workflow whose category constrains the graph.
            document: Proposed editor contents.
            user: Current authenticated account.

        Returns:
            Complete validation results with node and field locations.

        Raises:
            ForbiddenException: If the account cannot edit workflows.
            NotFoundException: If the workflow does not exist.
        """
        cls.require_editor(user)
        graph = await cls.get_accessible(id, user)
        return await validate_document(document, graph.category)

    @classmethod
    async def save_graph(cls, id: int, body: GraphWrite, user: UserInfo) -> FlowGraph:
        """Save a structurally valid draft with an atomic version check.

        Args:
            id: Workflow to edit.
            body: Draft contents and the version the caller read.
            user: Current authenticated account.

        Returns:
            Saved workflow, including its new draft version.

        Raises:
            ForbiddenException: If editing is not permitted.
            NotFoundException: If the workflow does not exist.
            KaloscopeException: If the draft is malformed or its version is stale.
        """
        cls.require_editor(user)
        graph = await cls.get_accessible(id, user)
        if not graph.editable:
            raise ForbiddenException(ErrorCode.FLOW_NOT_EDITABLE)
        cls._check_version(graph, body.draft_version)
        validation = await validate_document(body, graph.category, complete=False)
        if not validation.valid:
            raise KaloscopeException(ErrorCode.FLOW_GRAPH_INVALID, status_code=400)
        draft = compress_document(body)
        if graph.draft == draft:
            return graph
        state = GraphState.DRAFT
        if graph.state != GraphState.DRAFT:
            state = (
                GraphState.PUBLISHED
                if draft == graph.definition
                else GraphState.MODIFIED
            )
        return await cls._write_draft(graph, draft, state=state)

    @classmethod
    def _check_version(cls, graph: FlowGraph, expected: int) -> None:
        """Reject writes based on an outdated editor snapshot.

        Args:
            graph: Current persisted workflow.
            expected: Version read by the caller.

        Raises:
            KaloscopeException: If another write has changed the workflow.
        """
        if graph.draft_version != expected:
            raise KaloscopeException(ErrorCode.FLOW_DRAFT_CONFLICT, status_code=409)

    @classmethod
    async def preview_draft(
        cls, id: int, body: GraphWrite, user: UserInfo
    ) -> GraphPreview:
        """Compare a proposed graph with the exact saved version being edited.

        Args:
            id: Workflow to preview.
            body: Proposed contents and their base version.
            user: Current authenticated account.

        Returns:
            Node and edge differences, validation results, and an internal editor path.

        Raises:
            ForbiddenException: If editing is not permitted.
            NotFoundException: If the workflow does not exist.
            KaloscopeException: If the base version is stale.
        """
        preview, _ = await cls.preview_draft_with_baseline(id, body, user)
        return preview

    @classmethod
    async def preview_draft_with_baseline(
        cls,
        id: int,
        body: GraphWrite,
        user: UserInfo,
        baseline: GraphDocument | None = None,
    ) -> tuple[GraphPreview, int]:
        """Compare proposed and captured graphs with the same saved draft.

        Args:
            id: Workflow to preview.
            body: Proposed contents and their base version.
            user: Current authenticated account.
            baseline: Captured contents before the proposed changes, if available.
                Only the proposed graph receives complete validation.

        Returns:
            Proposed preview and the number of pre-existing node or edge changes.
            The count is zero when no captured baseline is supplied.

        Raises:
            ForbiddenException: If editing is not permitted.
            NotFoundException: If the workflow does not exist.
            KaloscopeException: If the base version is stale.
        """
        cls.require_editor(user)
        graph = await cls.get_accessible(id, user)
        if not graph.editable:
            raise ForbiddenException(ErrorCode.FLOW_NOT_EDITABLE)
        cls._check_version(graph, body.draft_version)
        saved = graph.draft or {"nodes": [], "edges": []}
        preview = GraphPreview(
            workflow_id=id,
            draft_version=graph.draft_version,
            changes=cls.compare_documents(saved, compress_document(body)),
            validation=await validate_document(body, graph.category),
            editor_path=f"/settings/workflows/graphs/{id}",
        )
        baseline_changes = (
            len(cls.compare_documents(saved, compress_document(baseline)))
            if baseline is not None
            else 0
        )
        return preview, baseline_changes

    @staticmethod
    def compare_documents(previous: dict, proposed: dict) -> list[GraphChange]:
        """List canonical node and edge differences between two compressed graphs.

        Args:
            previous: Graph before the change, such as a saved draft.
            proposed: Graph after the change.

        Returns:
            Ordered differences ignoring transient editor state, with bounded
            JSON Pointers and exact path counts.
        """
        before_document = comparison_document(previous)
        after_document = comparison_document(proposed)
        changes = []
        collections: tuple[tuple[str, Literal["node", "edge"]], ...] = (
            ("nodes", "node"),
            ("edges", "edge"),
        )
        for collection, kind in collections:
            before = {item["id"]: item for item in before_document[collection]}
            after = {item["id"]: item for item in after_document[collection]}
            for item_id in sorted(before.keys() | after.keys()):
                paths, path_count = changed_paths(
                    before.get(item_id), after.get(item_id)
                )
                if path_count:
                    changes.append(
                        GraphChange(
                            kind=kind,
                            id=item_id,
                            before=before.get(item_id),
                            after=after.get(item_id),
                            operation=(
                                "add"
                                if item_id not in before
                                else "remove"
                                if item_id not in after
                                else "modify"
                            ),
                            changed_paths=paths,
                            changed_path_count=path_count,
                            omitted_changed_paths=path_count - len(paths),
                        )
                    )
        return changes

    @classmethod
    @atomic()
    async def create_draft(cls, body: GraphDraftCreate, user: UserInfo) -> FlowGraph:
        """Create a structurally valid workflow without publishing its contents.

        Args:
            body: New workflow metadata and draft contents.
            user: Current authenticated account.

        Returns:
            New editable workflow and its first saved version.

        Raises:
            ForbiddenException: If the account cannot edit workflows.
            KaloscopeException: If the name exists or the draft is malformed.
        """
        cls.require_editor(user)
        graph = await cls.upsert(
            GraphBasics(
                name=body.name, description=body.description, category=body.category
            )
        )
        return await cls.save_graph(
            graph.id,
            GraphWrite(**body.document.model_dump(), draft_version=graph.draft_version),
            user,
        )

    @classmethod
    async def list_drafts(cls, id: int, user: UserInfo) -> list[dict]:
        """List up to twenty retained draft versions without their graph contents.

        Args:
            id: Workflow whose saved history to inspect.
            user: Current authenticated account.

        Returns:
            Version numbers and retention timestamps, newest first.

        Raises:
            ForbiddenException: If the account cannot edit workflows.
            NotFoundException: If the workflow does not exist.
        """
        cls.require_editor(user)
        await cls.get_accessible(id, user)
        return (
            await FlowDraftRevision.filter(graph_id=id)
            .limit(20)
            .values("version", "created_at")
        )

    @classmethod
    async def get_draft(cls, id: int, version: int, user: UserInfo) -> dict:
        """Read one retained draft without changing the workflow's active state.

        Args:
            id: Workflow whose history to read.
            version: Retained version returned by `list_drafts`.
            user: Current authenticated account.

        Returns:
            Compressed editor document from the selected historical version.

        Raises:
            ForbiddenException: If the account cannot edit workflows.
            NotFoundException: If the workflow or retained version no longer exists.
        """
        cls.require_editor(user)
        await cls.get_accessible(id, user)
        revision = await FlowDraftRevision.get_or_none(graph_id=id, version=version)
        if revision is None:
            raise NotFoundException
        return revision.draft

    @classmethod
    async def restore_draft(
        cls, id: int, version: int, expected: int, user: UserInfo
    ) -> FlowGraph:
        """Restore a retained draft as a new version without changing publication.

        Args:
            id: Workflow to restore.
            version: Retained historical version to copy.
            expected: Current draft version read by the caller.
            user: Current authenticated account.

        Returns:
            Workflow containing the restored draft and a new version number.

        Raises:
            ForbiddenException: If editing is not permitted.
            NotFoundException: If the workflow or historical version is missing.
            KaloscopeException: If the current version changed or validation fails.
        """
        draft = await cls.get_draft(id, version, user)
        return await cls.save_graph(
            id, GraphWrite(**draft, draft_version=expected), user
        )

    @classmethod
    @atomic()
    async def _write_draft(
        cls, graph: FlowGraph, draft: dict, **changes: Any
    ) -> FlowGraph:
        """Atomically replace a draft and retain its previous version.

        Start with the conditional update to avoid promoting an older SQLite
        read snapshot after another connection has already written to the graph.

        Args:
            graph: Workflow snapshot to compare against the database.
            draft: Compressed replacement document.
            **changes: Publication fields changed by the same atomic write.

        Returns:
            Updated workflow while the transaction still owns the write lock.

        Raises:
            KaloscopeException: If the persisted version changed before the update.
        """
        updated = await FlowGraph.filter(
            id=graph.id, draft_version=graph.draft_version
        ).update(
            draft=draft,
            draft_version=graph.draft_version + 1,
            updated_at=timezone.now(),
            **changes,
        )
        if not updated:
            raise KaloscopeException(ErrorCode.FLOW_DRAFT_CONFLICT, status_code=409)
        await FlowDraftRevision.create(
            graph_id=graph.id,
            version=graph.draft_version,
            draft=graph.draft or {"nodes": [], "edges": []},
        )
        await FlowDraftRevision.filter(
            graph_id=graph.id, version__lt=graph.draft_version - 19
        ).delete()
        return await FlowGraph.get(id=graph.id)

    @classmethod
    async def publish_graph(
        cls, id: int, body: GraphWrite, user: UserInfo
    ) -> FlowGraph:
        """Publish a fully validated graph without overwriting a newer draft.

        Args:
            id: Workflow to publish.
            body: Editor contents and the version the caller read.
            user: Current authenticated account.

        Returns:
            Published workflow with an updated draft version.

        Raises:
            ForbiddenException: If the account cannot edit workflows.
            NotFoundException: If the workflow does not exist.
            KaloscopeException: If validation fails or the version is stale.
        """
        cls.require_editor(user)
        graph = await cls.get_accessible(id, user)
        cls._check_version(graph, body.draft_version)
        document = (
            body
            if graph.editable
            else GraphDocument.model_validate(graph.draft or {"nodes": [], "edges": []})
        )
        validation = await validate_document(document, graph.category)
        if not validation.valid:
            code = validation.issues[0].code
            raise KaloscopeException(
                code if code.startswith("ai_") else ErrorCode.FLOW_GRAPH_INVALID,
                status_code=400,
            )
        definition = compress_document(document)
        if graph.state == GraphState.PUBLISHED and graph.definition == definition:
            return graph
        revision = graph.revision
        if graph.editable or not revision:
            revision = max(int(timezone.now().timestamp()), (revision or 0) + 1)
        return await cls._write_draft(
            graph,
            definition,
            definition=definition,
            revision=revision,
            state=GraphState.PUBLISHED,
        )

    @classmethod
    @atomic()
    async def upgrade_graph(cls, id: int, tmpl: dict) -> FlowGraph:
        """Upgrade the flow graph by referencing a new template.

        Args:
            id: Flow graph ID.
            tmpl: Serialized template dict to reference.

        Returns:
            Upgraded flow graph instance.
        """
        graph = await FlowGraph.get(id=id)
        # check if the graph is editable
        if graph.editable:
            return graph
        # retract the graph first
        await cls.retract_graph(id)
        # refresh the version changed by retraction before applying the template
        await graph.refresh_from_db()
        return await cls._write_draft(
            graph,
            tmpl["definition"],
            tmpl_id=tmpl["id"],
            category=tmpl["category"],
            revision=tmpl["revision"],
            state=GraphState.DRAFT,
        )

    @classmethod
    @atomic()
    async def retract_graph(cls, id: int):
        """Retract the flow graph and pause all running associated jobs.

        Args:
            id: Flow graph ID.
        """
        state = GraphState.DRAFT
        if (
            await FlowGraph.filter(id=id, state__not=state).update(
                state=state,
                draft_version=F("draft_version") + 1,
                updated_at=timezone.now(),
            )
            == 1
        ):
            running_jobs = await FlowJob.filter(graph_id=id, state=JobState.RUNNING)
            if running_jobs:
                engine = cls.app_ctx().flow_engine
                for job in running_jobs:
                    await engine.pause_job(job.id)

    @classmethod
    async def export_graphs(cls, ids: list) -> bytes | None:
        """Export the published flow graphs as a zip file.

        Args:
            ids: List of flow graph IDs to export.

        Returns:
            A bytes object with the zip data, or `None` if empty.
        """
        if not ids:
            return None

        graphs = await FlowGraph.filter(id__in=ids, state__not=GraphState.DRAFT)
        if not graphs:
            return None

        async def _encode(icon: str | None) -> str | None:
            """Encodes the icon file as a base64 string.

            Args:
                icon: Icon file path.

            Returns:
                A base64 encoded string of the icon file, or `None` if not found.
            """
            if not icon:
                return None
            icon_dir = Path(KaloscopeConfig.get_workspace("images")) / "icons"
            icon_path = icon_dir / icon[6:]  # remove the `icons/` prefix
            if not icon_path.exists():
                return None
            # read the icon file and encode it as base64
            async with aiofiles.open(icon_path, "rb") as f:
                b64str = base64.b64encode(await f.read()).decode(ENCODING)
                return f"data:image/webp;base64,{b64str}"

        # create a zip file in memory
        zip = io.BytesIO()
        with zipfile.ZipFile(zip, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for graph in graphs:
                name = f"{graph.name}.json"
                data = {
                    "name": graph.name,
                    "icon": await _encode(graph.icon),
                    "description": graph.description,
                    "category": graph.category,
                    "revision": graph.revision,
                    "definition": graph.definition,
                }
                zf.writestr(name, json.pretty(data))
        return zip.getvalue()

    @classmethod
    @atomic()
    async def import_graphs(cls, zip: File) -> int:
        """Import flow graphs from a zip file.

        Args:
            zip: Zip file containing flow graph definitions.

        Returns:
            Number of created flow graphs.
        """
        created = 0
        with zipfile.ZipFile(io.BytesIO(zip.body), mode="r") as zf:
            for filename in zf.namelist():
                path = Path(filename)
                if path.suffix != ".json" or "__MACOSX" in path.parts:
                    continue

                data = json.loads(zf.read(filename))
                graph = await FlowGraph.get_or_none(name=data["name"])
                if graph is None:
                    # create a new flow graph
                    await FlowGraph.create(
                        name=data["name"],
                        icon=await save_icon(data.get("icon")),
                        description=data.get("description"),
                        category=data["category"],
                        draft=data.get("definition"),
                        revision=data.get("revision"),
                        state=GraphState.DRAFT,
                    )
                    created += 1
                elif graph.category == data["category"]:
                    if graph.editable is False:
                        # skip if the graph is not editable
                        continue
                    if graph.revision and graph.revision >= data.get("revision", 0):
                        # skip if the revision is not newer
                        continue
                    # update the existing flow graph
                    graph.icon = await save_icon(data.get("icon")) or graph.icon
                    graph.description = data.get("description", graph.description)
                    graph.draft = data.get("definition", graph.draft)
                    graph.revision = data.get("revision", graph.revision)
                    if graph.state != GraphState.DRAFT:
                        graph.state = GraphState.MODIFIED
                    await graph.save()

        return created

    @classmethod
    @atomic()
    async def delete(cls, id: int):
        """Delete a flow graph.

        Args:
            id: Flow graph ID.
        """
        if await FlowGraph.filter(id=id, state=GraphState.DRAFT).delete() > 0:
            # also delete the related user histories and permissions
            await UserHistory.filter(rel_type=HistoryType.SEARCH, rel_id=id).delete()
            await UserPermission.filter(rel_type=PermType.INDEXER, rel_id=id).delete()


class FlowLogService(BaseService[FlowLog], model=FlowLog):
    """The service class for all flow log related operations."""

    @classmethod
    async def get_accessible_logs(
        cls, graph_id: int, user: UserInfo
    ) -> list[list[dict]]:
        """Read logs after checking workflow access.

        Args:
            graph_id: Graph ID whose logs should be read.
            user: Authenticated user with permissions loaded by `authorize`.

        Returns:
            Retained execution groups in their existing order.

        Raises:
            NotFoundException: If the workflow does not exist.
            ForbiddenException: If the user cannot access the workflow.
        """
        await FlowGraphService.get_accessible(graph_id, user)
        return await cls.get_logs(graph_id)

    @classmethod
    async def get_logs(cls, graph_id: int) -> list[list[dict]]:
        """Get execution logs of the flow graph.

        Args:
            graph_id: Flow graph ID.

        Returns:
            A list of groups of log entries. Each group is a list of dictionaries with
            the following keys:
                - at: The timestamp of the log entry.
                - type: The type of the log entry (start, node_type, end).
                - data: The data associated with the log entry.
                - document: The document or additional information related to the log.
                - error: A translated error code when supplied by a failed node.
        """
        all = await FlowLog.filter(graph_id=graph_id)
        groups: list[list[dict]] = []
        for _, logs in groupby(all, key=lambda x: x.started_at):
            group: list[dict] = []
            for log in logs:
                if len(group) == 0:
                    group.append(
                        {
                            "at": log.started_at,
                            "type": "start",
                            "data": None,
                            "document": log.bootparams,
                        }
                    )
                if not log.ended_at:
                    group.append(
                        {
                            "at": log.created_at,
                            "type": log.node_type,
                            "data": log.node_data,
                            "document": log.exc_info,
                            "error": (log.node_data or {}).get("$error"),
                        }
                    )
                else:
                    group.append(
                        {
                            "at": log.ended_at,
                            "type": "end",
                            "data": None,
                            "document": log.retval,
                        }
                    )
            groups.append(group)
        return groups


# trial runs carry this server-owned marker through their startup parameters
TRIAL_KEY = "$trial"
# SQLite reads the marker key without loading every stored parameter set
TRIAL_KEY_SQL = """json_extract("bootparams", '$."$trial".key')"""
TRIAL_PARAMS_BYTES = 8192
# seconds one trial call may spend before reporting a background run
TRIAL_WAIT = 8.0
TRIAL_EFFECTS = {
    "http": "sends real HTTP requests",
    "script": "runs Python scripts",
    "cache": "saves workflow variables",
    "text": "writes files in media libraries",
    "ai": "calls the configured AI model",
}


def trial_marker(bootparams: Mapping[str, Any] | None) -> dict[str, str]:
    """Read the trial marker from stored startup parameters.

    Args:
        bootparams: Instance or log parameters.

    Returns:
        The marker's `key` and `digest`, or an empty mapping for other runs.
    """
    marker = (bootparams or {}).get(TRIAL_KEY)
    return marker if isinstance(marker, dict) else {}


class FlowRunService(BaseService[FlowInstance], model=FlowInstance):
    """Run exact reviewed workflow content once through the existing engine."""

    @classmethod
    def digest(cls, document: dict, entry: str, params: Mapping[str, Any]) -> str:
        """Fingerprint the executed definition, entry and parameters.

        Args:
            document: Compressed graph that will run.
            entry: Start node type.
            params: Caller startup parameters.

        Returns:
            Hex SHA-256 digest of the canonical request.
        """
        request = {"definition": document, "entry": entry, "params": params}
        return hashlib.sha256(
            json.dumps(request, option=orjson.OPT_SORT_KEYS)
        ).hexdigest()

    @classmethod
    def effects(cls, document: dict, entry: str) -> tuple[list[str], bool]:
        """Describe side effects of the nodes reachable from one entry.

        Args:
            document: Compressed graph that will run.
            entry: Start node type.

        Returns:
            Effect descriptions and whether an automatic repeat is safe, which
            holds only when the reachable nodes at most send GET/HEAD requests.
        """
        nodes = {node["id"]: node for node in document["nodes"]}
        targets: dict[str, list[str]] = {}
        for edge in document["edges"]:
            targets.setdefault(edge["source"], []).append(edge["target"])
        pending = [
            id for id, node in nodes.items() if node["data"].get("$schema") == entry
        ]
        reached = set(pending)
        while pending:
            for target in targets.get(pending.pop(), []):
                if target in nodes and target not in reached:
                    reached.add(target)
                    pending.append(target)
        types = {nodes[id]["data"].get("$schema") for id in reached}
        effects = [TRIAL_EFFECTS[type] for type in sorted(types & TRIAL_EFFECTS.keys())]
        methods = {
            str(nodes[id]["data"].get("method") or "GET").upper()
            for id in reached
            if nodes[id]["data"].get("$schema") == "http"
        } - {"GET", "HEAD"}
        if methods:
            effects.append(f"uses {'/'.join(sorted(methods))}, which may change data")
        return effects, not methods and (types & TRIAL_EFFECTS.keys()) <= {"http"}

    @classmethod
    async def check(
        cls, graph: FlowGraph, document: dict, entry: str, params: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        """Check the entry, parameters and static validity before a run.

        Args:
            graph: Workflow whose category constrains entries.
            document: Compressed graph that will run.
            entry: Requested start node type.
            params: Caller startup parameters.

        Returns:
            Located issues; empty when the run may start. Nothing is executed.
        """
        issues: list[dict[str, Any]] = []
        entries = sorted(
            schema.node_type
            for schema in Node.schemas
            if schema.group == NodeGroup.START
            and graph.category in schema.categories
            and schema.node_type != "auth_start"
        )
        if entry == "auth_start":
            issues.append(
                {
                    "path": "entry",
                    "message": "Login needs credentials; the user logs in from the "
                    "search page.",
                }
            )
        elif entry not in entries:
            issues.append(
                {"path": "entry", "message": f"Use one of: {', '.join(entries)}."}
            )
        elif not any(
            node["data"].get("$schema") == entry for node in document["nodes"]
        ):
            issues.append({"path": "entry", "message": f"The graph has no {entry}."})
        if any(str(key).startswith("$") for key in params):
            issues.append(
                {"path": "params", "message": "Keys starting with $ are reserved."}
            )
        if len(json.dumps(params)) > TRIAL_PARAMS_BYTES:
            issues.append({"path": "params", "message": "Parameters exceed 8 KiB."})
        validation = await validate_document(
            GraphDocument.model_validate(document), graph.category
        )
        issues.extend(
            {"path": "document", **issue.model_dump(exclude_none=True)}
            for issue in validation.issues
            if issue.severity == "error"
        )
        return issues[:8]

    @classmethod
    async def find(cls, key: str) -> tuple[FlowInstance | None, list[FlowLog]]:
        """Locate a trial's pending instance and retained log rows in any workflow.

        Args:
            key: Trial request key.

        Returns:
            The instance while queued, running or awaiting resumption, and the
            retained log rows in creation order. Callers compare the workflow, so
            a key reused for another workflow is detected.
        """
        instance = (
            await FlowInstance.annotate(trial_key=RawSQL(TRIAL_KEY_SQL))
            .filter(trial_key=key)
            .order_by("id")
            .only("id", "graph_id", "bootparams")
            .first()
        )
        logs = (
            await FlowLog.annotate(trial_key=RawSQL(TRIAL_KEY_SQL))
            .filter(trial_key=key)
            .order_by("id")
        )
        return instance, logs

    @classmethod
    async def saved_draft(
        cls, graph: FlowGraph, version: int, key: str
    ) -> tuple[dict, bool]:
        """Resolve the saved draft a request names, keeping its own run readable.

        Args:
            graph: Accessible workflow.
            version: Saved draft version the caller reviewed.
            key: Trial request key.

        Returns:
            The original definition for a recorded request, or the current draft.
            Recorded requests may only read their existing run.

        Raises:
            KaloscopeException: If the version differs from the recorded request,
                or an unsubmitted request names a stale version.
        """
        receipt = await FlowRunReceipt.get_or_none(key=key)
        if receipt is not None and receipt.graph_id == graph.id:
            if receipt.draft_version != version:
                raise KaloscopeException(ErrorCode.FLOW_DRAFT_CONFLICT, status_code=409)
            return receipt.definition, True
        FlowGraphService._check_version(graph, version)
        return graph.draft or {"nodes": [], "edges": []}, False

    @classmethod
    async def running(cls, graph_id: int) -> set[str]:
        """List the keys of trials still holding an instance for one workflow.

        Args:
            graph_id: Workflow whose instances to inspect.

        Returns:
            Keys of trials that are queued, running or awaiting resumption.
        """
        return set(
            await FlowInstance.filter(graph_id=graph_id)
            .annotate(trial_key=RawSQL(TRIAL_KEY_SQL))
            .filter(trial_key__not_isnull=True)
            .values_list("trial_key", flat=True)
        )

    @classmethod
    async def submit(
        cls, graph_id: int, document: dict, bootparams: dict[str, Any], version: int
    ) -> FlowInstance:
        """Claim a trial with a recoverable instance and queue it once.

        Args:
            graph_id: Workflow whose engine context, variables and logs are used.
            document: Exact compressed graph stored on the instance.
            bootparams: Startup parameters, including the trial marker.
            version: Reviewed saved draft version underlying the definition.

        Returns:
            Queued instance whose stored definition the engine executes.

        Raises:
            Exception: If queueing fails; the claim is then released for a retry.
        """
        marker = trial_marker(bootparams)
        async with in_transaction():
            receipt = await FlowRunReceipt.create(
                key=marker["key"],
                graph_id=graph_id,
                draft_version=version,
                digest=marker["digest"],
                definition=document,
            )
            instance = await FlowInstance.create(
                graph_id=graph_id,
                definition=document,
                bootparams=bootparams,
                context={},
                repeatable=True,
            )
        try:
            # no await after queue acceptance: a later lookup cannot undo the claim
            cls.app_ctx().flow_engine.enqueue_instance(instance)
        except Exception:
            # a failed queue write leaves nothing to run, so a retry may submit
            async with in_transaction():
                await instance.delete()
                await receipt.delete()
            raise
        return instance

    @classmethod
    async def run(
        cls,
        workflow_id: int,
        document: dict,
        *,
        entry: str,
        params: Mapping[str, Any],
        key: str,
        user: UserInfo,
        replay_only: bool = False,
        draft_version: int | None = None,
    ) -> dict[str, Any]:
        """Submit reviewed content once, then wait briefly for its outcome.

        The run is a recoverable instance holding this exact definition, so the
        engine never substitutes the published one; an interrupted run resumes
        like other recoverable runs. Ingest runs only preview candidates.

        Args:
            workflow_id: Workflow whose engine context, variables and logs are used.
            document: Exact compressed graph to execute.
            entry: Start node type.
            params: Caller startup parameters without `$` keys.
            key: Account-scoped request key; the same key never dispatches twice.
            user: Current administrator.
            replay_only: Only read an existing run, as for an outdated version.
            draft_version: Reviewed saved version; `None` uses the current version.

        Returns:
            Raw facts: `issues` (nothing ran), `conflict` (the key ran another
            workflow, content or parameters), `pending` (another call is
            submitting it), or the `instance` still running and the retained
            `logs`, with `replayed` when the key had already been submitted. An
            interrupted caller still completes its dispatch before the key is
            released, so a retry reads that run.

        Raises:
            ForbiddenException: If the account cannot edit workflows.
            NotFoundException: If the workflow is missing.
            KaloscopeException: If an outdated version has no run to read.
        """
        from filelock import Timeout

        from app.core.dl.syncer import submission_lock

        deadline = time.monotonic() + TRIAL_WAIT
        FlowGraphService.require_editor(user)
        graph = await FlowGraphService.get_accessible(workflow_id, user)
        digest = cls.digest(document, entry, params)
        bootparams = {
            **params,
            START_KEY: entry,
            TRIAL_KEY: {"key": key, "digest": digest},
        }
        if graph.category == GraphCategory.INGEST:
            # interactive ingest runs only preview metadata candidates
            bootparams[MANUAL_KEY] = True
        try:
            with submission_lock(f"trial:{key}"):
                receipt = await FlowRunReceipt.get_or_none(key=key)
                instance, logs = await cls.find(key)
                replayed = receipt is not None
                if replayed:
                    if (
                        receipt.graph_id != graph.id
                        or receipt.digest != digest
                        or (
                            draft_version is not None
                            and receipt.draft_version != draft_version
                        )
                    ):
                        return {"conflict": True}
                else:
                    if replay_only:
                        raise KaloscopeException(
                            ErrorCode.FLOW_DRAFT_CONFLICT, status_code=409
                        )
                    await graph.refresh_from_db()
                    if draft_version is not None:
                        FlowGraphService._check_version(graph, draft_version)
                    if issues := await cls.check(graph, document, entry, params):
                        return {"issues": issues}
                    submission = asyncio.ensure_future(
                        cls.submit(graph.id, document, bootparams, graph.draft_version)
                    )
                    try:
                        instance = await asyncio.shield(submission)
                    except asyncio.CancelledError:
                        # a cancelled call never splits the claim from its dispatch
                        await submission
                        raise
        except Timeout:
            return {"pending": True}
        while instance is not None and time.monotonic() < deadline:
            if not await FlowInstance.exists(id=instance.id):
                break
            await asyncio.sleep(0.2)
        instance, logs = await cls.find(key)
        return {"replayed": replayed, "instance": instance, "logs": logs}


class FlowTriggerService(BaseService[FlowTrigger], model=FlowTrigger):
    """The service class for all flow trigger related operations."""

    @classmethod
    @atomic()
    async def bind_triggers(
        cls, category: GraphCategory, rel_id: int, triggers: list[GraphRef] | None
    ):
        """Bind flow triggers to a specific category and relation ID.

        Args:
            category: Category to bind the triggers to.
            rel_id: Relation ID to bind the triggers to.
            triggers: A list of GraphRef objects representing the flow triggers.
        """
        await FlowTrigger.filter(category=category, rel_id=rel_id).delete()
        if triggers:
            await FlowTrigger.bulk_create(
                [
                    FlowTrigger(
                        category=category,
                        rel_id=rel_id,
                        graph_id=trigger.graph_id,
                        asynchronous=trigger.asynchronous,
                        priority=index + 1,
                    )
                    for index, trigger in enumerate(triggers)
                ]
            )

    @classmethod
    async def get_triggers(
        cls, category: GraphCategory, rel_id: int, *, unpublished: bool = False
    ) -> list[dict]:
        """Get flow triggers for a specific category and relation ID.

        Args:
            category: Graph category to filter the triggers.
            rel_id: Relation ID to filter the triggers.
            unpublished: Whether to include stored triggers whose workflow is an
                unpublished draft, for editing. Runtime callers keep the default,
                so drafts never execute.

        Returns:
            A list of dictionaries containing the flow trigger details in stored
            order. With `unpublished`, each also has a `published` flag.
        """
        conn = Tortoise.get_connection("default")
        triggers = await conn.execute_query_dict(
            """
            SELECT
                t.id,
                t.graph_id,
                g.name graph_name,
                t.asynchronous,
                g.state != 'draft' published
            FROM flow_trigger t
            INNER JOIN flow_graph g ON g.id = t.graph_id
            WHERE
                (? OR g.state != 'draft')
            AND g.category = ?
            AND t.rel_id = ?
            ORDER BY t.priority ASC
            """,
            [unpublished, category.value, rel_id],
        )
        for trigger in triggers:
            # convert the asynchronous flag to a boolean
            trigger["asynchronous"] = bool(trigger["asynchronous"])
            published = bool(trigger.pop("published"))
            if unpublished:
                trigger["published"] = published
        return triggers

    @classmethod
    async def fire(
        cls,
        category: GraphCategory,
        rel_id: int,
        bootparams: Mapping[str, Any] | None = None,
        *,
        repeatable: bool = False,
        recoverable: bool = True,
    ):
        """Fire the flow triggers for a specific category and relation ID.

        Args:
            category: Graph category to filter the triggers.
            rel_id: Relation ID to filter the triggers.
            bootparams: Boot parameters for execution. `None` adds no startup
                parameters.
            repeatable: Whether the execution is repeatable. Defaults to `False`.
            recoverable: Whether the execution is recoverable. Defaults to `True`.
        """
        graph_ids = [
            GraphRef(graph_id=trigger["graph_id"], asynchronous=trigger["asynchronous"])
            for trigger in await cls.get_triggers(category, rel_id)
        ]
        if not graph_ids:
            return

        # use the flow engine to execute the flow graphs
        engine = cls.app_ctx().flow_engine
        await engine.execute_batch(
            graph_ids, bootparams, repeatable=repeatable, recoverable=recoverable
        )


class FlowJobService(BaseService[FlowJob], model=FlowJob):
    """The service class for all flow job related operations."""

    @classmethod
    async def list_jobs(cls, query: JobQuery, user: UserInfo) -> dict[str, Any]:
        """List schedules within the account's workflow permissions.

        Args:
            query: Pagination, workflow name, state, and trigger filters.
            user: Authenticated user with permissions loaded by `authorize`.

        Returns:
            A schedule page with workflow names and existing schedule settings.
        """
        queries = []
        if user.role != UserRole.ADMIN:
            queries.append(Q(graph_id__in=user.perms.indexer_ids if user.perms else []))
        if query.name:
            queries.append(Q(graph__name__icontains=query.name))
        if query.state:
            queries.append(Q(state=query.state))
        if query.trigger:
            queries.append(Q(trigger=query.trigger))
        page = await FlowJob.page(*queries, **query.page_params)
        result = await cls.dump_page(page)
        graph_names = dict(
            await FlowGraph.filter(
                id__in={job.graph_id for job in page.items}
            ).values_list("id", "name")
        )
        for job in result["items"]:
            job["graph_name"] = graph_names.get(job["graph_id"])
        return result

    @classmethod
    @atomic()
    async def upsert(cls, obj: JobUpsert) -> FlowJob:
        """Create or update a flow job.

        Args:
            obj: Flow job data.

        Returns:
            Flow job instance.
        """
        engine = cls.app_ctx().flow_engine
        data = obj.model_dump(exclude={"id"})
        if obj.id:
            await FlowJob.filter(id=obj.id).update(**data)
            job = await FlowJob.get(id=obj.id)
            await engine.refresh_job(job.id)
        else:
            data["state"] = JobState.PENDING
            job = await FlowJob.create(**data)
            await engine.add_job(job.id)
        return job
