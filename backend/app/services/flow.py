import base64
import io
import zipfile
from collections.abc import Mapping
from itertools import groupby
from pathlib import Path
from typing import Any

import aiofiles
import httpx
from sanic.log import logger
from sanic.request.form import File
from tortoise import Tortoise, timezone
from tortoise.expressions import Q
from tortoise.transactions import atomic

from app.core.config import KaloscopeConfig
from app.core.constants import ENCODING
from app.core.exceptions import ErrorCode, KaloscopeException, NotFoundException
from app.core.flow.fetcher import fetch_origin, save_icon
from app.core.flow.nodes.base import Node
from app.models.flow import (
    FlowGraph,
    FlowJob,
    FlowLog,
    FlowRepository,
    FlowTemplate,
    FlowTrigger,
    GraphBasics,
    GraphCategory,
    GraphRef,
    GraphState,
    JobState,
    JobUpsert,
    RepositoryAdd,
)
from app.models.user import HistoryType, PermType, UserHistory, UserPermission
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
            add: The repository to add.

        Returns:
            The added flow repository.
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
        try:
            url = f"https://api.github.com/repos/{repo_name}"
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
            id: The flow repository ID.
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
            id: The flow template ID.
            name: The name of the new flow graph.

        Returns:
            The created flow graph instance.
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
            id: The flow template ID.
            name: The name of the new flow graph.

        Returns:
            The created flow graph instance.
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
        """Get the newest template for the same repo and path.

        Args:
            tmpl: The serialized template dict.

        Returns:
            The serialized newest template dict, or None if no newer template exists.
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
    """The service class for all flow graph related operations."""

    @classmethod
    async def unique_name(cls, name: str) -> str:
        """Generate a unique name for the flow graph.

        Args:
            name: The base name to check for uniqueness.

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
            obj: The flow graph basics.

        Raises:
            KaloscopeException: If the name already exists.

        Returns:
            The flow graph instance.
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
    async def save_graph(cls, id: int, draft: dict) -> FlowGraph:
        """Save the flow graph draft.

        Args:
            id: The flow graph ID.
            draft: The JSON content of the nodes and edges.

        Returns:
            The saved flow graph instance.
        """
        graph = await FlowGraph.get(id=id)
        # check if the graph is editable
        if not graph.editable:
            return graph

        graph.draft = Node.compress(draft)
        graph.updated_at = timezone.now()
        if graph.state != GraphState.DRAFT and draft != graph.definition:
            graph.state = GraphState.MODIFIED
        await graph.save()
        return graph

    @classmethod
    async def publish_graph(cls, id: int, definition: dict) -> FlowGraph:
        """Publish the flow graph.

        Args:
            id: The flow graph ID.
            definition: The JSON content of the nodes and edges.

        Returns:
            The published flow graph instance.
        """
        graph = await FlowGraph.get(id=id)
        definition = Node.compress(definition)
        # return if the graph has not changed
        if graph.state == GraphState.PUBLISHED and graph.definition == definition:
            return graph
        # check if the graph is editable
        if not graph.editable:
            if graph.draft:
                definition = graph.draft
            else:
                return graph

        # generate the revision number
        now = timezone.now()
        if graph.editable or not graph.revision:
            revision = int(now.timestamp())
            if graph.revision and graph.revision >= revision:
                revision = graph.revision + 1
            graph.revision = revision

        graph.draft = definition
        graph.definition = definition
        graph.updated_at = now
        graph.state = GraphState.PUBLISHED
        await graph.save()
        return graph

    @classmethod
    @atomic()
    async def upgrade_graph(cls, id: int, tmpl: dict) -> FlowGraph:
        """Upgrade the flow graph by referencing a new template.

        Args:
            id: The flow graph ID.
            tmpl: The serialized template dict to reference.

        Returns:
            The upgraded flow graph instance.
        """
        graph = await FlowGraph.get(id=id)
        # check if the graph is editable
        if graph.editable:
            return graph
        # retract the graph first
        await cls.retract_graph(id)
        # update the graph with the new template
        graph.tmpl_id = tmpl["id"]
        graph.category = tmpl["category"]
        graph.revision = tmpl["revision"]
        graph.draft = tmpl["definition"]
        graph.state = GraphState.DRAFT
        await graph.save()
        return graph

    @classmethod
    @atomic()
    async def retract_graph(cls, id: int):
        """Retract the flow graph and pause all running associated jobs.

        Args:
            id: The flow graph ID.
        """
        state = GraphState.DRAFT
        if await FlowGraph.filter(id=id, state__not=state).update(state=state) == 1:
            running_jobs = await FlowJob.filter(graph_id=id, state=JobState.RUNNING)
            if running_jobs:
                engine = cls.app_ctx().flow_engine
                for job in running_jobs:
                    await engine.pause_job(job.id)

    @classmethod
    async def export_graphs(cls, ids: list) -> bytes | None:
        """Export the published flow graphs as a zip file.

        Args:
            ids: The list of flow graph IDs to export.

        Returns:
            A bytes object with the zip data, or None if empty.
        """
        if not ids:
            return None

        graphs = await FlowGraph.filter(id__in=ids, state__not=GraphState.DRAFT)
        if not graphs:
            return None

        async def _encode(icon: str | None) -> str | None:
            """Encodes the icon file as a base64 string.

            Args:
                icon: The icon file path.

            Returns:
                A base64 encoded string of the icon file, or None if not found.
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
            zip: The zip file containing flow graph definitions.

        Returns:
            The number of created flow graphs.
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
            id: The flow graph ID.
        """
        if await FlowGraph.filter(id=id, state=GraphState.DRAFT).delete() > 0:
            # also delete the related user histories and permissions
            await UserHistory.filter(rel_type=HistoryType.SEARCH, rel_id=id).delete()
            await UserPermission.filter(rel_type=PermType.INDEXER, rel_id=id).delete()


class FlowLogService(BaseService[FlowLog], model=FlowLog):
    """The service class for all flow log related operations."""

    @classmethod
    async def get_logs(cls, graph_id: int) -> list[list[dict]]:
        """Get the execution logs of the flow graph.

        Args:
            graph_id: The flow graph ID.

        Returns:
            A list of groups of log entries. Each group is a list of dictionaries with
            the following keys:
                - at: The timestamp of the log entry.
                - type: The type of the log entry (start, node_type, end).
                - data: The data associated with the log entry.
                - document: The document or additional information related to the log.
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


class FlowTriggerService(BaseService[FlowTrigger], model=FlowTrigger):
    """The service class for all flow trigger related operations."""

    @classmethod
    @atomic()
    async def bind_triggers(
        cls, category: GraphCategory, rel_id: int, triggers: list[GraphRef] | None
    ):
        """Bind flow triggers to a specific category and relation ID.

        Args:
            category: The category to bind the triggers to.
            rel_id: The relation ID to bind the triggers to.
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
    async def get_triggers(cls, category: GraphCategory, rel_id: int) -> list[dict]:
        """Get the flow triggers for a specific category and relation ID.

        Args:
            category: The graph category to filter the triggers.
            rel_id: The relation ID to filter the triggers.

        Returns:
            A list of dictionaries containing the flow trigger details.
        """
        conn = Tortoise.get_connection("default")
        triggers = await conn.execute_query_dict(
            """
            SELECT
                t.id,
                t.graph_id,
                g.name graph_name,
                t.asynchronous
            FROM flow_trigger t
            INNER JOIN flow_graph g ON g.id = t.graph_id
            WHERE
                g.state != 'draft'
            AND g.category = ?
            AND t.rel_id = ?
            ORDER BY t.priority ASC
            """,
            [category.value, rel_id],
        )
        for trigger in triggers:
            # convert the asynchronous flag to a boolean
            trigger["asynchronous"] = bool(trigger["asynchronous"])
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
            category: The graph category to filter the triggers.
            rel_id: The relation ID to filter the triggers.
            bootparams: The boot parameters for execution.
            repeatable: Whether the execution is repeatable.
            recoverable: Whether the execution is recoverable.
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
    @atomic()
    async def upsert(cls, obj: JobUpsert) -> FlowJob:
        """Create or update a flow job.

        Args:
            obj: The flow job data.

        Returns:
            The flow job instance.
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
