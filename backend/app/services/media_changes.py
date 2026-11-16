"""Review and apply library scans and video rescrapes for the assistant and MCP."""

import asyncio
import hashlib
import os
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import UUID
from weakref import WeakValueDictionary

import orjson
from filelock import Timeout as LockTimeout
from sanic import Sanic
from sanic.log import logger
from tortoise.transactions import in_transaction

from app.core.dl.syncer import submission_lock
from app.core.exceptions import (
    ErrorCode,
    ForbiddenException,
    KaloscopeException,
    NotFoundException,
)
from app.core.flow.context import MANUAL_KEY
from app.core.media.common import ContentError
from app.core.media.naming import render_directory, render_filename, validate_template
from app.core.media.organizer import preview_organization
from app.core.media.shelver import (
    get_nfo_path,
    get_nfo_type,
    preview_nfo,
    read_nfo,
)
from app.models.flow import (
    FlowGraph,
    FlowTrigger,
    GraphCategory,
    GraphRef,
    GraphState,
)
from app.models.media import (
    LibraryConfig,
    LibraryScan,
    LibType,
    MediaAction,
    MediaBatch,
    MediaChangeApply,
    MediaChangeReceipt,
    MediaEvent,
    MediaItem,
    MediaLib,
    MediaMetadata,
    MediaRescrape,
)
from app.models.user import UserInfo, UserRole
from app.services.base import BaseService
from app.services.flow import FlowTriggerService
from app.services.media import (
    INGESTION_SCOPE,
    TV_INGESTION_LAYOUT,
    MediaItemService,
    MediaLibService,
    output_issue,
    workflow_fact,
)
from app.utils.extractor import extract_title
from app.utils.json import dumps, loads

VIDEO_TYPES = (LibType.MOVIE, LibType.TV_SHOW)
# a long scan or episode refresh keeps running after the tool answers
APPLY_WAIT = 8
CANDIDATE_LIMIT = 5
# a naming preview samples works and a few of their videos; it never covers everything
NAMING_SAMPLES = 3
NAMING_PATHS = 2
# the change model accepts at most 50 IDs for an explicit binding order
BINDING_ORDER_LIMIT = 50
PREVIEW_FIELDS = (
    "title",
    "originaltitle",
    "year",
    "premiered",
    "aired",
    "rating",
    "season",
    "site",
    "id",
    "genres",
    "plot",
    "poster",
    "backdrop",
)
MEDIA_UNCERTAIN = (
    "The change may already have taken effect; inspect the library or item. "
    "Repeating this request only reads its recorded outcome."
)
_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
_RUNNING: set[asyncio.Task] = set()


def _require_admin(user: UserInfo):
    """Allow only administrators, as the scan and metadata routes do.

    Args:
        user: Current account.

    Raises:
        ForbiddenException: If the account is not an administrator.
    """
    if user.role != UserRole.ADMIN:
        raise ForbiddenException(ErrorCode.PERMISSION_DENIED)


def _digest(value: Any) -> str:
    """Fingerprint JSON-compatible facts.

    Args:
        value: Facts to fingerprint.

    Returns:
        Short stable digest.
    """
    return hashlib.sha256(dumps(value, option=orjson.OPT_SORT_KEYS)).hexdigest()[:16]


def _file_digest(path: str | None) -> str | None:
    """Fingerprint a file's bytes so a review notices later edits.

    Args:
        path: File to read, or `None`.

    Returns:
        Digest, or `None` when the file is missing or unreadable.
    """
    if not path:
        return None
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def _short(value: Any, limit: int = 120) -> Any:
    """Shorten long text and lists for a readable preview.

    Args:
        value: Metadata value.
        limit: Maximum characters for text.

    Returns:
        The value, a truncated string or at most five list entries.
    """
    if isinstance(value, str) and len(value) > limit:
        return value[: limit - 1] + "…"
    if isinstance(value, list):
        return [_short(entry, 40) for entry in value[:5]] + (
            [f"+{len(value) - 5}"] if len(value) > 5 else []
        )
    return value


def _relative(lib: MediaLib, path: str | Path) -> str:
    """Show a path relative to its library root.

    Args:
        lib: Owning library.
        path: Absolute path inside the library.

    Returns:
        Relative POSIX path, `.` for the root, or the name if outside it.
    """
    try:
        return Path(path).relative_to(lib.dir).as_posix() or "."
    except ValueError:
        return Path(path).name


def candidate_key(candidate: dict[str, Any]) -> str:
    """Identify a candidate by source, ID and season, or by its content without an ID.

    Args:
        candidate: Raw metadata candidate.

    Returns:
        Twelve hexadecimal characters.
    """
    identity = (
        [str(candidate["site"]), str(candidate["id"]), candidate.get("season")]
        if candidate.get("site") and candidate.get("id")
        else candidate
    )
    return hashlib.sha256(dumps(identity, option=orjson.OPT_SORT_KEYS)).hexdigest()[:12]


def _change_facts(
    change: LibraryScan | MediaRescrape | LibraryConfig | MediaBatch,
) -> dict:
    """Read the fields that identify a reviewed change.

    Args:
        change: Reviewed change.

    Returns:
        JSON fields. A configuration keeps only what was requested, so an omitted
        setting differs from an explicitly cleared one. Empty add/remove/replace
        lists are no-ops, while an explicit empty order requires no bindings.
    """
    if not isinstance(change, LibraryConfig):
        return change.model_dump(mode="json")
    return {
        "action": change.action,
        **{
            name: value
            for name, value in change.model_dump(
                mode="json", exclude_unset=True
            ).items()
            if value != [] or name == "order_workflow_ids"
        },
    }


class MediaChangeService(BaseService[MediaChangeReceipt], model=MediaChangeReceipt):
    """Review and apply one library scan, video rescrape or configuration at a time."""

    @classmethod
    def change_request(
        cls,
        user_id: int,
        change: LibraryScan | MediaRescrape | LibraryConfig | MediaBatch,
        revision: str,
        nonce: bytes | None = None,
    ) -> UUID:
        """Issue a request ID that identifies one reviewed change and revision.

        Args:
            user_id: Reviewing account.
            change: Reviewed scan, rescrape with its candidate, or configuration.
            revision: Reviewed revision.
            nonce: Random half of an existing ID being checked; `None` issues one.

        Returns:
            A new ID, or the only ID this nonce can have for the same review.
        """
        nonce = nonce or os.urandom(8)
        digest = _digest(_change_facts(change))
        tag = hashlib.sha256(nonce + f"{user_id}:{digest}:{revision}".encode())
        return UUID(bytes=nonce + tag.digest()[:8])

    @staticmethod
    def request_key(user_id: int, request_id: UUID) -> str:
        """Name the receipt of one account's request.

        Args:
            user_id: Account that reviewed the request.
            request_id: Reviewed request ID.

        Returns:
            Account-scoped receipt key.
        """
        return hashlib.sha256(f"{user_id}:{request_id}".encode()).hexdigest()

    @classmethod
    async def _review_scan(
        cls, change: LibraryScan, user: UserInfo
    ) -> tuple[dict[str, Any], MediaLib]:
        """Describe what scanning one library would do.

        Args:
            change: Library to scan.
            user: Current administrator.

        Returns:
            The review and the library.

        Raises:
            ForbiddenException: If the account cannot scan libraries.
            NotFoundException: If the library does not exist.
        """
        _require_admin(user)
        lib = await MediaLibService.get_accessible(change.library_id, user)
        issues = []
        if lib.lib_type not in VIDEO_TYPES:
            issues.append(
                {
                    "path": "library_id",
                    "message": "Only movie and TV libraries are scanned here.",
                }
            )
        triggers = (
            await FlowTrigger.filter(category=GraphCategory.INGEST, rel_id=lib.id)
            .exclude(graph__state=GraphState.DRAFT)
            .order_by("priority")
            .select_related("graph")
        )
        bindings = [
            {"workflow_id": row.graph_id, "workflow_name": row.graph.name}
            for row in triggers
        ]
        scanning = Sanic.get_app().ctx.lib_watcher.is_scanning(lib.dir)
        notes = [
            "Finishes any interrupted organization, then compares files on disk "
            "with indexed items.",
            "New video files and items without an NFO run the bound ingest "
            "workflows in order; changed NFO files are reread."
            if bindings
            else "No published ingest workflow is bound, so new files are indexed "
            "without fetching metadata.",
            "Items whose files are gone are removed from the library; no files are "
            "deleted.",
            "Work continues in the background; how many items change is unknown "
            "until it finishes.",
        ]
        if lib.rename_template:
            notes.append(
                "Newly scraped items may be renamed or moved by the naming template."
            )
        if scanning:
            notes.append("A scan is already running; applying now would be refused.")
        revision = _digest(
            [
                lib.id,
                lib.dir,
                lib.lib_type,
                lib.rename_template,
                lib.language,
                [
                    [row.graph_id, row.graph.revision, _digest(row.graph.definition)]
                    for row in triggers
                ],
            ]
        )
        result: dict[str, Any] = {
            "valid": not issues,
            "issues": issues,
            "action": MediaAction.SCAN,
            "library_id": lib.id,
            "library_name": lib.name,
            "lib_type": lib.lib_type,
            "scanning": scanning,
            "queued_events": await MediaEvent.filter(lib_id=lib.id).count(),
            "bindings": bindings,
            "naming_template": bool(lib.rename_template),
            "notes": notes,
            "revision": revision,
        }
        if not issues:
            result["request_id"] = str(cls.change_request(user.id, change, revision))
        return loads(dumps(result)), lib

    @classmethod
    async def review_scan(cls, change: LibraryScan, user: UserInfo) -> dict:
        """Review one library scan without starting it.

        Args:
            change: Library to scan.
            user: Current administrator.

        Returns:
            The library, its scan state and queued events, the bound ingest
            workflows, the effects of scanning, located issues, the `revision`
            and, when valid, a `request_id` for this exact review.

        Raises:
            ForbiddenException: If the account cannot scan libraries.
            NotFoundException: If the library does not exist.
        """
        result, _ = await cls._review_scan(change, user)
        return result

    @staticmethod
    async def _workflows(lib: MediaLib) -> list[dict[str, Any]]:
        """List published workflows that can write this library's metadata.

        Args:
            lib: Movie or TV library.

        Returns:
            Bound workflows in binding order, then compatible unbound ones.
        """
        rows = (
            await FlowTrigger.filter(category=GraphCategory.INGEST, rel_id=lib.id)
            .order_by("priority")
            .select_related("graph")
        )
        bound = {row.graph_id for row in rows}
        graphs = [row.graph for row in rows] + [
            graph
            for graph in await FlowGraph.filter(category=GraphCategory.INGEST)
            .exclude(id__in=bound)
            .order_by("name")
        ]
        return [
            {
                "workflow_id": graph.id,
                "workflow_name": graph.name,
                "bound": graph.id in bound,
            }
            for graph in graphs
            if graph.state != GraphState.DRAFT
            and output_issue(lib.lib_type, graph.definition) is None
        ]

    @classmethod
    async def _target(
        cls, change: MediaRescrape, user: UserInfo
    ) -> tuple[dict[str, Any], MediaItem, FlowGraph | None]:
        """Resolve the item, its library and the scraper workflow.

        Args:
            change: Requested rescrape.
            user: Current administrator.

        Returns:
            Base facts with located issues, the item and the workflow, if usable.

        Raises:
            ForbiddenException: If the account cannot rescrape the item.
            NotFoundException: If the item does not exist.
        """
        _require_admin(user)
        item = await MediaItem.get_or_none(id=change.item_id).select_related("lib")
        if item is None:
            raise NotFoundException
        lib = await MediaLibService.get_accessible(item.lib_id, user)
        issues = []
        base: dict[str, Any] = {
            "action": MediaAction.RESCRAPE,
            "item_id": item.id,
            "title": item.title,
            "name": item.name,
            "year": item.year,
            "library_id": lib.id,
            "library_name": lib.name,
            "lib_type": lib.lib_type,
        }
        if lib.lib_type not in VIDEO_TYPES:
            issues.append(
                {
                    "path": "item_id",
                    "message": "Only movies and TV shows are rescraped.",
                }
            )
        if item.parent_id is not None:
            base["parent_id"] = item.parent_id
            issues.append(
                {
                    "path": "item_id",
                    "message": "Choose the top-level movie or show; ask before using "
                    "parent_id, whose episodes follow its update.",
                }
            )
        graph = None
        if not issues:
            available = await cls._workflows(lib)
            base["workflows"] = available[:10]
            usable = {entry["workflow_id"] for entry in available}
            workflow_id = change.workflow_id or next(
                (entry["workflow_id"] for entry in available if entry["bound"]),
                None,
            )
            if workflow_id is None:
                issues.append(
                    {
                        "path": "workflow_id",
                        "message": "No bound workflow; ask the user to choose one of "
                        "workflows.",
                    }
                )
            elif workflow_id not in usable:
                issues.append(
                    {
                        "path": "workflow_id",
                        "message": "Choose a published ingest workflow that writes "
                        "this library's metadata.",
                    }
                )
            else:
                graph = await FlowGraph.get(id=workflow_id)
                base.update(workflow_id=graph.id, workflow_name=graph.name)
                if workflow_id not in {
                    entry["workflow_id"] for entry in base["workflows"]
                }:
                    base["workflows"] = base["workflows"][:9] + [
                        entry
                        for entry in available
                        if entry["workflow_id"] == workflow_id
                    ]
        return {**base, "issues": issues}, item, graph

    @classmethod
    async def _search(
        cls, item: MediaItem, graph: FlowGraph, change: MediaRescrape
    ) -> tuple[dict[str, Any], list[tuple[dict[str, Any], dict[str, Any]]], str | None]:
        """Run the workflow's manual candidate preview like the metadata dialog.

        Args:
            item: Top-level movie or show.
            graph: Published ingest workflow.
            change: Requested search inputs.

        Returns:
            The query, raw candidates with their compact descriptions, and an
            error code when the workflow failed.
        """
        lib = item.lib
        query = {
            "title": change.title or item.title or extract_title(item.name),
            "year": change.year if change.year is not None else item.year,
            # the dialog searches season 1 when the item has no season
            "season": next(
                value for value in (change.season, item.season, 1) if value is not None
            ),
        }
        params = {
            MANUAL_KEY: True,
            "path": item.path,
            "name": item.name,
            "nfo_type": get_nfo_type(lib.lib_type),
            "language": lib.language,
            **query,
            "page_num": 1,
            "page_size": CANDIDATE_LIMIT,
        }
        try:
            results = await Sanic.get_app().ctx.flow_engine.execute(
                graph_id=graph.id, bootparams=params
            )
        except KaloscopeException as error:
            code = error.message if error.message in ErrorCode else "workflow_failed"
            return query, [], code
        except Exception:
            logger.exception("Failed to search metadata candidates")
            return query, [], "workflow_failed"
        candidates = []
        for raw in (results if isinstance(results, list) else [])[:CANDIDATE_LIMIT]:
            if not isinstance(raw, dict):
                continue
            raw = loads(dumps(raw))
            try:
                meta = await preview_nfo(lib.lib_type, raw)
            except ContentError:
                compact = {
                    "key": candidate_key(raw),
                    "title": _short(raw.get("title"), 80),
                    "usable": False,
                }
            else:
                compact = {
                    "key": candidate_key(raw),
                    "title": _short(meta.title, 80),
                    "year": meta.year,
                    "site": _short(meta.site, 40),
                    "id": _short(meta.id, 80),
                    "rating": meta.rating,
                    "plot": _short(meta.plot, 100),
                }
                if meta.originaltitle and meta.originaltitle != meta.title:
                    compact["originaltitle"] = _short(meta.originaltitle, 80)
                if lib.lib_type == LibType.TV_SHOW:
                    compact["season"] = meta.season
            candidates.append((raw, loads(dumps(compact))))
        return query, candidates, None

    @classmethod
    async def _preview(
        cls, item: MediaItem, graph: FlowGraph, candidate: dict[str, Any]
    ) -> dict[str, Any]:
        """Compare stored metadata with a candidate without writing anything.

        Args:
            item: Top-level movie or show, loaded with its library.
            graph: Workflow whose episode searches would run.
            candidate: Raw candidate to save.

        Returns:
            Field changes, NFO and artwork facts, the episode refresh scope,
            organization, notes, located issues and the local `revision`.
        """
        lib = item.lib
        nfo_path = item.nfo_path or get_nfo_path(item.path)
        issues = []
        try:
            after = asdict(await preview_nfo(lib.lib_type, candidate))
        except ContentError:
            after = None
            issues.append(
                {"path": "candidate", "message": "This candidate cannot be saved."}
            )
        current, _, _ = read_nfo(lib.lib_type, nfo_path)
        before = asdict(current) if current else {}
        if lib.lib_type == LibType.TV_SHOW:
            # a missing season leaves the stored value unchanged on save
            if before.get("season") is None:
                before["season"] = item.season
            if after is not None and after.get("season") is None:
                after["season"] = item.season
        changes = []
        if after is not None:
            for name in PREVIEW_FIELDS:
                old, new = before.get(name), after.get(name)
                if loads(dumps(old)) != loads(dumps(new)):
                    changes.append(
                        {"field": name, "before": _short(old), "after": _short(new)}
                    )
        episodes = None
        episode_facts = []
        if lib.lib_type == LibType.TV_SHOW:
            children = await MediaItem.filter(parent_id=item.id, lib_id=lib.id)
            # mirror the episode refresh rule using the old source and series ID
            series_id = candidate.get("id")
            same_series = (
                bool(series_id)
                and str(series_id) == str(item.unique_id)
                and candidate.get("site") == item.nfo_source
            )
            season = candidate.get("season", item.season)
            kept = 0
            for child in children:
                exists = bool(child.nfo_path) and Path(child.nfo_path).exists()
                if same_series and exists and child.season == season:
                    kept += 1
                episode_facts.append(
                    [
                        child.id,
                        child.path,
                        child.season,
                        child.episode,
                        child.nfo_path,
                        exists,
                        await asyncio.to_thread(
                            _file_digest, child.nfo_path or get_nfo_path(child.path)
                        ),
                    ]
                )
            episodes = {
                "total": len(children),
                "refresh": len(children) - kept,
                "keep": kept,
                "season": season,
                "unnumbered": sum(child.episode is None for child in children),
            }
        organization: dict[str, Any] = {"template": bool(lib.rename_template)}
        if lib.rename_template and after is not None:
            context = {**after, "parent": dict(after)}
            current_dir = Path(item.path if Path(item.path).is_dir() else item.dir)
            organization["current"] = _relative(lib, current_dir)
            try:
                target = render_directory(lib.rename_template, context, lib.lib_type)
                organization["expected"] = target.as_posix() or "."
                if lib.lib_type == LibType.MOVIE:
                    organization["expected_name"] = render_filename(
                        lib.rename_template, context, lib.lib_type
                    )
            except ValueError as error:
                organization["expected"] = None
                organization["issue"] = str(error)[:120]
        notes = [
            "The NFO is rewritten from this candidate; fields it lacks are not kept.",
            "Poster and backdrop stay references; no images are downloaded.",
        ]
        if episodes:
            notes.append(
                "Each refreshed episode runs this workflow with the selected season "
                "and its stored episode number; one without a result keeps its NFO. "
                "Missing episode numbers are not guessed."
            )
        if lib.rename_template:
            notes.append(
                "Saving queues organization; the organizer rechecks files and may "
                "move, rename, defer or refuse unsafe changes."
            )
        else:
            notes.append("No naming template is set, so no files are renamed or moved.")
        revision = _digest(
            {
                "item": [
                    item.id,
                    item.lib_id,
                    item.parent_id,
                    item.path,
                    item.nfo_path,
                    item.title,
                    item.year,
                    item.season,
                    item.unique_id,
                    item.nfo_source,
                ],
                "nfo": await asyncio.to_thread(_file_digest, nfo_path),
                "workflow": [
                    graph.id,
                    graph.state,
                    graph.revision,
                    _digest(graph.definition),
                ],
                "library": [lib.lib_type, lib.dir, lib.rename_template, lib.language],
                "episodes": episode_facts,
                "candidate": _digest(candidate),
            }
        )
        return loads(
            dumps(
                {
                    "issues": issues,
                    "changes": changes,
                    "nfo": {
                        "path": _relative(lib, nfo_path),
                        "exists": current is not None,
                    },
                    "episodes": episodes,
                    "organization": organization,
                    "notes": notes,
                    "revision": revision,
                }
            )
        )

    @classmethod
    async def review_rescrape(cls, change: MediaRescrape, user: UserInfo) -> dict:
        """List candidates, or preview replacing an item's metadata with one.

        The workflow's manual candidate search runs every time; nothing is
        saved and no file is organized. A valid review keeps its exact candidate
        so the confirmed change never searches again.

        Args:
            change: Item, workflow, search inputs and the chosen candidate key.
            user: Current administrator.

        Returns:
            The item, usable workflows, the query and compact `candidates`
            without a key; with a listed key, field changes, NFO, episode and
            organization effects, notes, the `revision` and a `request_id`.

        Raises:
            ForbiddenException: If the account cannot rescrape the item.
            NotFoundException: If the item does not exist.
        """
        base, item, graph = await cls._target(change, user)
        issues = base.pop("issues")
        if graph is None:
            return loads(dumps({"valid": False, "issues": issues, **base}))
        query, candidates, error = await cls._search(item, graph, change)
        base["query"] = query
        if error:
            issues.append(
                {"path": "workflow_id", "message": f"The search failed: {error}."}
            )
        elif not candidates:
            issues.append(
                {"path": "title", "message": "No candidates; adjust title or year."}
            )
        chosen = next(
            (pair for pair in candidates if pair[1]["key"] == change.candidate), None
        )
        if issues or change.candidate is None or chosen is None:
            if not issues:
                issues.append(
                    {
                        "path": "candidate",
                        "message": "Show these candidates and let the user choose one."
                        if change.candidate is None
                        else "That candidate was not returned now; choose a listed "
                        "key.",
                    }
                )
            return loads(
                dumps(
                    {
                        "valid": False,
                        "issues": issues,
                        **base,
                        "candidates": [compact for _, compact in candidates],
                    }
                )
            )
        raw, compact = chosen
        preview = await cls._preview(item, graph, raw)
        issues = preview.pop("issues")
        if not compact.get("usable", True):
            issues = issues or [
                {"path": "candidate", "message": "This candidate cannot be saved."}
            ]
        result = {"valid": not issues, "issues": issues, **base, "candidate": compact}
        result.update(preview)
        if not issues:
            request_id = cls.change_request(user.id, change, preview["revision"])
            key = hashlib.sha256(f"{user.id}:{request_id}".encode()).hexdigest()
            # the review alone records which candidate a confirmation saves
            await MediaChangeReceipt.create(
                key=key,
                action=MediaAction.RESCRAPE,
                lib_id=item.lib_id,
                item_id=item.id,
                graph_id=graph.id,
                candidate=raw,
            )
            result["request_id"] = str(request_id)
        return loads(dumps(result))

    @classmethod
    async def _naming_preview(cls, lib: MediaLib, template: str) -> dict[str, Any]:
        """Show sample works' current and proposed paths under a template.

        Args:
            lib: Video library, left unchanged.
            template: Validated proposed template.

        Returns:
            Bounded samples using the organizer's own plans, how many works exist,
            and whether a pending organization could still change these paths.
        """
        works = await MediaItem.filter(lib_id=lib.id, parent_id__isnull=True).order_by(
            "id"
        )
        # show works with metadata and one without, whose problem would otherwise hide
        chosen = [work for work in works if work.nfo_path][: NAMING_SAMPLES - 1]
        chosen += [work for work in works if not work.nfo_path][:1]
        chosen += [work for work in works if work not in chosen]
        samples = []

        def shown(text: str | None) -> str | None:
            """Hide the library root in an organizer message.

            Args:
                text: Organizer message.

            Returns:
                The bounded message without absolute library paths.
            """
            return _short(text.replace(f"{lib.dir}/", ""), 160) if text else None

        for work in chosen[:NAMING_SAMPLES]:
            plan = await preview_organization(lib, template, work)
            samples.append(
                {
                    "item_id": work.id,
                    "title": _short(work.title or work.name, 80),
                    "status": plan["status"],
                    "paths": [
                        {
                            "current": _short(_relative(lib, entry["current"]), 160),
                            "proposed": _short(_relative(lib, entry["proposed"]), 160),
                        }
                        for entry in plan["paths"][:NAMING_PATHS]
                    ],
                    "more_paths": max(0, len(plan["paths"]) - NAMING_PATHS),
                    "moved_files": plan["moved_files"],
                    "issue": shown(plan["issue"]),
                }
            )
        return {
            "template": template,
            "sampled_works": len(samples),
            "total_works": len(works),
            "samples": samples,
            "pending_organization": await MediaEvent.filter(
                lib_id=lib.id, event_type="organize"
            ).exists(),
        }

    @classmethod
    async def _review_config(
        cls, change: LibraryConfig, user: UserInfo, *, preview: bool = True
    ) -> tuple[dict[str, Any], MediaLib, dict[str, Any], list[GraphRef] | None]:
        """Check one library configuration against current settings and workflows.

        Args:
            change: Requested settings and binding changes.
            user: Current administrator.
            preview: Whether to plan sample paths for a new naming template.

        Returns:
            The review, the library, the settings to write and the complete
            resulting bindings, or `None` when they stay unchanged.

        Raises:
            ForbiddenException: If the account cannot configure libraries.
            NotFoundException: If the library does not exist.
        """
        _require_admin(user)
        lib = await MediaLibService.get_accessible(change.library_id, user)
        issues: list[dict[str, str]] = []
        if lib.lib_type not in VIDEO_TYPES:
            issues.append(
                {
                    "path": "library_id",
                    "message": "Only movie and TV libraries are configured here.",
                }
            )
        rows = (
            await FlowTrigger.filter(category=GraphCategory.INGEST, rel_id=lib.id)
            .order_by("priority")
            .select_related("graph")
        )
        graphs = {row.graph_id: row.graph for row in rows}
        modes = {row.graph_id: row.asynchronous for row in rows}
        order = [row.graph_id for row in rows]
        added: list[int] = []
        removed: list[int] = []
        replaced: list[tuple[int, int]] = []
        new_ids: list[tuple[str, int]] = []

        def problem(path: str, message: str):
            """Record one located issue.

            Args:
                path: Argument the issue belongs to.
                message: What to correct.
            """
            issues.append({"path": path, "message": message})

        for swap in change.replace_workflows:
            path = "replace_workflows"
            if swap.workflow_id not in order:
                problem(path, f"Workflow {swap.workflow_id} is not bound.")
            elif swap.new_workflow_id in order:
                problem(path, f"Workflow {swap.new_workflow_id} is already bound.")
            else:
                order[order.index(swap.workflow_id)] = swap.new_workflow_id
                # the replacement keeps the execution mode of what it replaces
                modes[swap.new_workflow_id] = modes[swap.workflow_id]
                replaced.append((swap.workflow_id, swap.new_workflow_id))
                new_ids.append((path, swap.new_workflow_id))
        for workflow_id in change.remove_workflow_ids:
            if workflow_id in order:
                order.remove(workflow_id)
                removed.append(workflow_id)
            else:
                problem("remove_workflow_ids", f"Workflow {workflow_id} is not bound.")
        for workflow_id in change.add_workflow_ids:
            if workflow_id in order:
                problem("add_workflow_ids", f"Workflow {workflow_id} is already bound.")
            else:
                order.append(workflow_id)
                modes[workflow_id] = False
                added.append(workflow_id)
                new_ids.append(("add_workflow_ids", workflow_id))
        reordered = False
        if change.order_workflow_ids is not None:
            if sorted(change.order_workflow_ids) != sorted(order):
                problem(
                    "order_workflow_ids",
                    "List every resulting binding exactly once.",
                )
            else:
                reordered = change.order_workflow_ids != order
                order = list(change.order_workflow_ids)
        new_graphs = []
        for path, workflow_id in new_ids:
            try:
                found, _ = await MediaLibService.ingestion_check(
                    lib.id, workflow_id, user
                )
            except NotFoundException:
                problem(path, f"Workflow {workflow_id} does not exist.")
                continue
            issues.extend(
                {"path": path, "message": f"Workflow {workflow_id}: {entry['message']}"}
                for entry in found
                if entry["path"] == "ingest_workflow_id"
            )
            graphs[workflow_id] = await FlowGraph.get(id=workflow_id)
            new_graphs.append(graphs[workflow_id])

        writes: dict[str, Any] = {}
        settings = []
        if "language" in change.model_fields_set and change.language != lib.language:
            writes["language"] = change.language
            settings.append(
                {"field": "language", "before": lib.language, "after": change.language}
            )
        template = lib.rename_template
        if "rename_template" in change.model_fields_set:
            try:
                template = validate_template(change.rename_template or "", lib.lib_type)
            except ValueError as error:
                problem("rename_template", str(error)[:200])
                template = lib.rename_template
            if template != lib.rename_template:
                writes["rename_template"] = template
                settings.append(
                    {
                        "field": "rename_template",
                        "before": lib.rename_template,
                        "after": template,
                    }
                )
        bindings = None
        if order != [row.graph_id for row in rows]:
            bindings = [
                GraphRef(graph_id=workflow_id, asynchronous=modes[workflow_id])
                for workflow_id in order
            ]
        if not (writes or bindings is not None or issues):
            problem(
                "library_id",
                "No change requested; show the current settings and ask what to "
                "change.",
            )

        def named(workflow_id: int) -> dict[str, Any]:
            """Identify a workflow in the binding change summary.

            Args:
                workflow_id: Workflow that exists in the review.

            Returns:
                Its ID and name, when known.
            """
            graph = graphs.get(workflow_id)
            return {
                "workflow_id": workflow_id,
                "workflow_name": graph.name if graph else None,
            }

        available = await MediaLibService.ingestion_bindings(lib.id, user)
        notes = [
            "Saving changes only this library's configuration; it starts no scan, "
            "rescrape, download or organization. Those keep their own reviews."
        ]
        if bindings is not None:
            notes.append(
                "Bindings are read when each ingest request fires: queued items not "
                "yet started use this order, running workflows finish unchanged, and "
                "existing items are not scraped again."
            )
            if added:
                notes.append("New bindings run synchronously, the editor's default.")
            if replaced:
                notes.append(
                    "A replacement keeps the replaced binding's position and mode."
                )
            if not order:
                notes.append(
                    "With no bindings, new items run no ingest workflows; existing "
                    "NFO files can still supply metadata."
                )
        if "language" in writes:
            notes.append(
                "Language applies to requests created after saving; queued ingest "
                "work keeps its recorded language and existing NFOs stay as they are."
            )
        if "rename_template" in writes:
            notes.append(
                "Files do not move on save. A work is organized under the current "
                "template when its metadata next changes, such as a new ingest, an "
                "NFO change or a rescrape; organization already planned finishes on "
                "its own paths."
                if template
                else "Clearing stops future renames; files stay where they are."
            )
            if template and lib.lib_type == LibType.TV_SHOW:
                notes.append(
                    "Episodes without readable metadata keep their file names; "
                    "numbers are never guessed."
                )
        if any(not graph_fact["executable"] for graph_fact in available["bindings"]):
            notes.append(
                "Unpublished bindings stay stored and do not run until published."
            )
        naming = None
        if preview and template and "rename_template" in writes and not issues:
            naming = await cls._naming_preview(lib, template)
        revision = _digest(
            [
                lib.id,
                lib.dir,
                lib.lib_type,
                lib.language,
                lib.rename_template,
                [
                    [
                        row.graph_id,
                        row.priority,
                        row.asynchronous,
                        row.graph.state,
                        row.graph.revision,
                        _digest(row.graph.definition),
                    ]
                    for row in rows
                ],
                [
                    [graph.id, graph.state, graph.revision, _digest(graph.definition)]
                    for graph in new_graphs
                ],
            ]
        )
        result: dict[str, Any] = {
            "valid": not issues,
            "issues": issues[:12],
            "action": MediaAction.CONFIGURE,
            "library_id": lib.id,
            "library_name": lib.name,
            "lib_type": lib.lib_type,
            "language": lib.language,
            "rename_template": lib.rename_template,
            "bindings": available["bindings"],
            "binding_count": available["binding_count"],
            "binding_order": [row.graph_id for row in rows[:BINDING_ORDER_LIMIT]],
            "settings": settings,
            "bindings_change": {
                "added": [named(entry) for entry in added],
                "removed": [named(entry) for entry in removed],
                "replaced": [
                    {
                        **named(old),
                        "new_workflow_id": new,
                        "new_workflow_name": graphs[new].name
                        if new in graphs
                        else None,
                    }
                    for old, new in replaced
                ],
                "reordered": reordered,
            }
            if bindings is not None
            else None,
            "proposed_bindings": [
                {
                    **workflow_fact(graphs[entry.graph_id], lib.lib_type),
                    "order": index,
                    "asynchronous": entry.asynchronous,
                }
                for index, entry in enumerate(bindings[:20], 1)
                if entry.graph_id in graphs
            ]
            if bindings is not None
            else None,
            "workflows": available["workflows"],
            "proposed_binding_count": len(order) if bindings is not None else None,
            "proposed_binding_order": order[:BINDING_ORDER_LIMIT]
            if bindings is not None
            else None,
            "workflow_count": available["workflow_count"],
            "unpublished_workflows": available["unpublished_workflows"][:5],
            "unpublished_count": available["unpublished_count"],
            "naming": naming,
            "queued_events": await MediaEvent.filter(lib_id=lib.id).count(),
            "scope": INGESTION_SCOPE
            + (TV_INGESTION_LAYOUT if lib.lib_type == LibType.TV_SHOW else ""),
            "notes": notes,
            "revision": revision,
        }
        if len(rows) > BINDING_ORDER_LIMIT or len(order) > BINDING_ORDER_LIMIT:
            result["more_bindings"] = (
                "Binding order lists contain only the first 50 IDs; counts include "
                "all bindings. Use the library editor to reorder more than 50."
            )
        if available["workflow_count"] > len(available["workflows"]):
            result["more_workflows"] = (
                "These are a sample; page list_workflows with category=ingest. Any "
                "published compatible ID is accepted."
            )
        if not available["workflow_count"] and not any(
            entry["compatible"] for entry in available["bindings"]
        ):
            result["next"] = (
                "No compatible published scraper exists. Ask only for missing source "
                "details, build one with the movie_scraper or tv_scraper recipe "
                "(create_workflow_draft, validate, trial). The user publishes it; "
                "then review this change again."
            )
        if not issues:
            result["request_id"] = str(cls.change_request(user.id, change, revision))
        return loads(dumps(result)), lib, writes, bindings

    @classmethod
    async def review_config(cls, change: LibraryConfig, user: UserInfo) -> dict:
        """Read a library's ingestion settings, or preview changing them.

        Nothing is saved. A naming template is previewed with the organizer's own
        path planning on sample works, without touching files or metadata.

        Args:
            change: Library plus the settings and bindings to change, if any.
            user: Current administrator.

        Returns:
            Current settings and bindings in execution order, workflow choices, the
            requested setting and binding changes, resulting order, naming samples,
            scope and timing notes, located issues, the `revision` and, when valid,
            a `request_id` for this exact review.

        Raises:
            ForbiddenException: If the account cannot configure libraries.
            NotFoundException: If the library does not exist.
        """
        result, _, _, _ = await cls._review_config(change, user)
        return result

    @classmethod
    async def _library_state(cls, lib: MediaLib) -> dict[str, Any]:
        """Read a library's stored ingestion settings.

        Args:
            lib: Stored library.

        Returns:
            Language, naming template and bound workflow IDs in order.
        """
        return {
            "language": lib.language,
            "rename_template": lib.rename_template,
            "workflow_ids": await FlowTrigger.filter(
                category=GraphCategory.INGEST, rel_id=lib.id
            )
            .order_by("priority")
            .values_list("graph_id", flat=True),
        }

    @classmethod
    async def _apply_config(
        cls, body: MediaChangeApply, change: LibraryConfig, key: str, user: UserInfo
    ) -> dict[str, Any]:
        """Recheck and save one configuration together with its receipt.

        Args:
            body: Confirmed request.
            change: Reviewed configuration.
            key: Account-scoped request identity.
            user: Current administrator.

        Returns:
            Shared outcome described by `apply_change`.
        """
        base: dict[str, Any] = {
            "action": MediaAction.CONFIGURE,
            "library_id": change.library_id,
        }
        try:
            review, lib, _, _ = await cls._review_config(
                change, user, preview=not body.confirmed
            )
        except NotFoundException:
            if not await MediaLib.exists(id=change.library_id):
                return {
                    "applied": False,
                    "outcome": "absent",
                    **base,
                    "message": "The library no longer exists; nothing was saved.",
                }
            raise
        base["library_name"] = lib.name
        refused = await cls._checked(body, review, base)
        if refused is not None:
            return refused
        try:
            async with in_transaction():
                # the library editor may have saved since the check above
                current, _, writes, bindings = await cls._review_config(
                    change, user, preview=False
                )
                refused = await cls._checked(body, current, base)
                if refused is not None:
                    return refused
                if writes:
                    await MediaLib.filter(id=lib.id).update(**writes)
                if bindings is not None:
                    await FlowTriggerService.bind_triggers(
                        GraphCategory.INGEST, lib.id, bindings
                    )
                result = loads(
                    dumps(
                        {
                            "applied": True,
                            "outcome": "configured",
                            **base,
                            "settings": current["settings"],
                            "bindings_change": current["bindings_change"],
                            "proposed_bindings": current["proposed_bindings"],
                            "proposed_binding_count": current["proposed_binding_count"],
                            "proposed_binding_order": current["proposed_binding_order"],
                            "path": f"/medialibs/{lib.id}",
                            "message": "Saved; nothing was scanned, rescraped or "
                            "organized. Later work uses these settings as reviewed.",
                        }
                    )
                )
                await MediaChangeReceipt.create(
                    key=key,
                    action=MediaAction.CONFIGURE,
                    lib_id=lib.id,
                    claimed=True,
                    result=result,
                )
        except Exception:
            logger.exception("Failed to save a library configuration")
            try:
                receipt = await MediaChangeReceipt.get_or_none(key=key)
            except Exception:
                return {
                    "applied": None,
                    "outcome": "uncertain",
                    **base,
                    "message": "The save outcome could not be read. Repeat this "
                    "request to recover; committed saves are replayed, and a "
                    "rolled-back save can be retried safely.",
                }
            if receipt is not None:
                return await cls._replay(receipt, user)
            return cls._failed(base)
        return {**result, "replayed": False}

    @classmethod
    async def apply_change(cls, body: MediaChangeApply, user: UserInfo) -> dict:
        """Apply a confirmed scan, rescrape or configuration at most once.

        Scans and rescrapes claim a receipt before dispatch; configuration writes
        and their receipt commit together. Retries replay committed outcomes.
        Work still running after a few seconds continues in the background and
        records its outcome later.

        Args:
            body: Request ID, the exact reviewed change and revision, and the
                user's confirmation.
            user: Current administrator; permissions and inputs are rechecked.

        Returns:
            `scanned`, `submitted`, `already_scanning`, `rescraped`, `partial` or
            `configured` outcomes, `running` work, `replayed` outcomes, or `conflict`,
            `pending`, `stale`, `confirmation_required`, located issues, an
            `absent` target, a `failed` change known not to have applied, or an
            `uncertain` outcome to inspect rather than repeat.

        Raises:
            ForbiddenException: If the account cannot change media.
        """
        _require_admin(user)
        nonce = body.request_id.bytes[:8]
        if (
            cls.change_request(user.id, body.change, body.revision, nonce)
            != body.request_id
        ):
            return {
                "applied": False,
                "conflict": True,
                "message": "This request_id was reviewed for another change or "
                "revision; review it again.",
            }
        key = cls.request_key(user.id, body.request_id)
        lock = _LOCKS.setdefault(key, asyncio.Lock())
        if lock.locked():
            return await cls._pending(key, user)
        task = asyncio.ensure_future(cls._apply_guarded(body, key, user, lock))
        # work keeps running if the caller stops waiting or is cancelled
        _RUNNING.add(task)
        task.add_done_callback(_RUNNING.discard)
        done, _ = await asyncio.wait({task}, timeout=APPLY_WAIT)
        if task in done:
            return task.result()
        receipt = await MediaChangeReceipt.get_or_none(key=key)
        if isinstance(body.change, MediaBatch):
            from app.services.media_batches import MediaBatchService

            return await MediaBatchService.running(
                receipt if receipt is not None and receipt.claimed else None
            )
        target = {}
        if isinstance(body.change, LibraryScan | LibraryConfig):
            target = {
                "library_id": body.change.library_id,
                "path": f"/medialibs/{body.change.library_id}",
            }
        elif receipt is not None:
            target = {
                "item_id": receipt.item_id,
                "library_id": receipt.lib_id,
                "path": f"/medialibs/{receipt.lib_id}/{receipt.item_id}",
            }
        return {
            "applied": None,
            "outcome": "running",
            "action": body.change.action,
            **target,
            "message": "Still running in the background; repeat this request later "
            "to read its outcome. Do not review it again.",
        }

    @classmethod
    async def _pending(cls, key: str, user: UserInfo) -> dict[str, Any]:
        """Answer a request another call is still applying.

        Args:
            key: Account-scoped request identity.
            user: Current administrator.

        Returns:
            The recorded outcome once available, otherwise `pending`.
        """
        receipt = await MediaChangeReceipt.get_or_none(key=key)
        if receipt is not None and (
            receipt.result is not None
            # a running batch reports its progress instead of an opaque wait
            or (receipt.action == MediaAction.BATCH and receipt.claimed)
        ):
            return await cls._replay(receipt, user)
        return {
            "applied": False,
            "pending": True,
            "message": "A media change is still being applied; repeat this "
            "request later to read its outcome.",
        }

    @classmethod
    async def _apply_guarded(
        cls, body: MediaChangeApply, key: str, user: UserInfo, lock: asyncio.Lock
    ) -> dict[str, Any]:
        """Hold the request's process and file locks while applying it.

        Args:
            body: Confirmed request.
            key: Account-scoped request identity.
            user: Current administrator.
            lock: This process's lock for the request.

        Returns:
            Shared outcome described by `apply_change`.
        """
        async with lock:
            try:
                with submission_lock(f"media-change:{key}"):
                    receipt = await MediaChangeReceipt.get_or_none(key=key)
                    if receipt is not None and receipt.claimed:
                        # holding both locks proves no other call is applying it
                        return await cls._replay(receipt, user, settle=True)
                    if isinstance(body.change, MediaBatch):
                        from app.services.media_batches import MediaBatchService

                        return await MediaBatchService.apply(
                            body, body.change, key, user, receipt
                        )
                    if isinstance(body.change, LibraryScan):
                        return await cls._apply_scan(body, body.change, key, user)
                    if isinstance(body.change, LibraryConfig):
                        # different reviews of one library must recheck in sequence
                        with submission_lock(f"media-library:{body.change.library_id}"):
                            return await cls._apply_config(body, body.change, key, user)
                    # different reviews of the same item must recheck in sequence
                    with submission_lock(f"media-item:{body.change.item_id}"):
                        return await cls._apply_rescrape(
                            body, body.change, key, user, receipt
                        )
            except LockTimeout:
                return await cls._pending(key, user)

    @classmethod
    async def _replay(
        cls, receipt: MediaChangeReceipt, user: UserInfo, *, settle: bool = False
    ) -> dict[str, Any]:
        """Read a recorded outcome under current permissions without applying.

        Args:
            receipt: Claimed request.
            user: Current account.
            settle: Whether the caller holds the request's locks, so a batch
                without an outcome was interrupted rather than still running.

        Returns:
            The recorded outcome, or `uncertain` for a claim without one, with
            the library's scan state or settings or the item's title now, or `None`
            once removed. Later edits are reported, never overwritten.

        Raises:
            ForbiddenException: If the account can no longer read the library.
        """
        _require_admin(user)
        if receipt.action == MediaAction.BATCH:
            from app.services.media_batches import MediaBatchService

            return await MediaBatchService.replay(receipt, user, settle=settle)
        with suppress(NotFoundException):
            await MediaLibService.get_accessible(receipt.lib_id, user)
        current = None
        if receipt.action == MediaAction.SCAN:
            lib = await MediaLib.get_or_none(id=receipt.lib_id)
            if lib is not None:
                current = {
                    "scanning": Sanic.get_app().ctx.lib_watcher.is_scanning(lib.dir)
                }
        elif receipt.action == MediaAction.CONFIGURE:
            if lib := await MediaLib.get_or_none(id=receipt.lib_id):
                current = await cls._library_state(lib)
        elif item := await MediaItem.get_or_none(id=receipt.item_id):
            current = {"title": item.title, "year": item.year}
        target = (
            {"item_id": receipt.item_id, "library_id": receipt.lib_id}
            if receipt.action == MediaAction.RESCRAPE
            else {"library_id": receipt.lib_id}
        )
        return loads(
            dumps(
                {
                    **(
                        receipt.result
                        or {
                            "applied": None,
                            "outcome": "uncertain",
                            "action": receipt.action,
                            **target,
                            "message": MEDIA_UNCERTAIN,
                        }
                    ),
                    "replayed": True,
                    "current": current,
                }
            )
        )

    @classmethod
    async def _apply_scan(
        cls, body: MediaChangeApply, change: LibraryScan, key: str, user: UserInfo
    ) -> dict[str, Any]:
        """Recheck, claim and submit one library scan.

        Args:
            body: Confirmed request.
            change: Reviewed scan.
            key: Account-scoped request identity.
            user: Current administrator.

        Returns:
            Shared outcome described by `apply_change`.
        """
        base: dict[str, Any] = {
            "action": MediaAction.SCAN,
            "library_id": change.library_id,
        }
        try:
            review, lib = await cls._review_scan(change, user)
        except NotFoundException:
            if not await MediaLib.exists(id=change.library_id):
                return {
                    "applied": False,
                    "outcome": "absent",
                    **base,
                    "message": "The library no longer exists; nothing was scanned.",
                }
            raise
        base["library_name"] = lib.name
        stale = await cls._checked(body, review, base)
        if stale is not None:
            return stale
        try:
            await MediaChangeReceipt.create(
                key=key, action=MediaAction.SCAN, lib_id=lib.id, claimed=True
            )
        except Exception:
            logger.exception("Failed to claim a library scan")
            return cls._failed(base)
        watcher = Sanic.get_app().ctx.lib_watcher
        path = f"/medialibs/{lib.id}"
        try:
            await watcher.scan_directory(lib, validate_request=True)
        except KaloscopeException as error:
            if error.message != ErrorCode.SCAN_IN_PROGRESS:
                logger.exception("Failed to scan a media library")
                return await cls._record(
                    key,
                    {
                        "applied": None,
                        "outcome": "uncertain",
                        **base,
                        "message": MEDIA_UNCERTAIN,
                    },
                )
            result = {
                "applied": False,
                "outcome": "already_scanning",
                **base,
                "path": path,
                "message": "A scan of this library was already running; nothing new "
                "started. Read scanning later.",
            }
        except Exception:
            logger.exception("Failed to scan a media library")
            result = {
                "applied": None,
                "outcome": "uncertain",
                **base,
                "message": "The scan stopped early; files found before the failure "
                "may already be queued. Read the library before scanning again.",
            }
        else:
            delegated = watcher.is_scanning(lib.dir)
            result = {
                "applied": True,
                "outcome": "submitted" if delegated else "scanned",
                **base,
                "path": path,
                "message": "Another worker runs the scan; read scanning later."
                if delegated
                else "Files were compared and changes queued; ingestion and "
                "organization continue in the background. This is not completion.",
            }
        return await cls._record(key, result)

    @classmethod
    async def _apply_rescrape(
        cls,
        body: MediaChangeApply,
        change: MediaRescrape,
        key: str,
        user: UserInfo,
        receipt: MediaChangeReceipt | None,
    ) -> dict[str, Any]:
        """Recheck, claim and save one reviewed candidate.

        Args:
            body: Confirmed request.
            change: Reviewed rescrape.
            key: Account-scoped request identity.
            user: Current administrator.
            receipt: Unclaimed review keeping the candidate, if any.

        Returns:
            Shared outcome described by `apply_change`.
        """
        base: dict[str, Any] = {
            "action": MediaAction.RESCRAPE,
            "item_id": change.item_id,
        }
        if not await MediaItem.exists(id=change.item_id):
            return {
                "applied": False,
                "outcome": "absent",
                **base,
                "message": "The item no longer exists; nothing was saved.",
            }
        target, item, graph = await cls._target(change, user)
        base.update(library_id=item.lib_id, title=item.title)
        if (
            receipt is None
            or receipt.item_id != item.id
            or receipt.candidate is None
            or graph is None
            or receipt.graph_id != graph.id
        ):
            return {
                "applied": False,
                **base,
                "stale": True,
                "message": "The reviewed candidate or workflow is no longer available; "
                "review the rescrape again.",
            }
        preview = await cls._preview(item, graph, receipt.candidate)
        review = {
            "valid": not (target["issues"] or preview["issues"]),
            "issues": target["issues"] + preview["issues"],
            **preview,
        }
        stale = await cls._checked(body, review, base)
        if stale is not None:
            return stale
        claimed = await MediaChangeReceipt.filter(key=key, claimed=False).update(
            claimed=True
        )
        if not claimed:
            return await cls._replay(await MediaChangeReceipt.get(key=key), user)
        nfo_path = item.nfo_path or get_nfo_path(item.path)
        before = await asyncio.to_thread(_file_digest, nfo_path)
        progress: dict[str, Any] = {}
        path = f"/medialibs/{item.lib_id}/{item.id}"
        try:
            written = await MediaItemService.save_video_metadata(
                item,
                MediaMetadata(graph_id=graph.id, metadata=receipt.candidate),
                progress,
            )
        except Exception as error:
            logger.exception("Failed to rescrape a media item")
            code = error.code if isinstance(error, ContentError) else None
            if not progress.get("written"):
                after = await asyncio.to_thread(_file_digest, nfo_path)
                if after == before:
                    # identical bytes cannot prove metadata or events stayed unchanged
                    return await cls._record(
                        key,
                        {
                            "applied": None,
                            "outcome": "uncertain",
                            **base,
                            "error": code,
                            "path": path,
                            "message": MEDIA_UNCERTAIN,
                        },
                    )
                result = {
                    "applied": True,
                    "outcome": "partial",
                    **base,
                    "step": "metadata",
                    "message": "The NFO was written but refreshing stored metadata "
                    "failed; the library rereads it later. Do not repeat.",
                }
            else:
                done = len(progress.get("episodes", []))
                result = {
                    "applied": True,
                    "outcome": "partial",
                    **base,
                    "step": "episodes",
                    "episodes": {
                        "attempted": done + 1,
                        "failed_episode_id": progress.get("current"),
                        "not_attempted": max(
                            0, (preview["episodes"] or {}).get("total", 0) - done - 1
                        ),
                    },
                    "error": code,
                    "path": path,
                    "message": "Show metadata was saved; one episode failed and later "
                    "ones were not attempted. Do not repeat; review again if needed.",
                }
        else:
            if not written:
                return await cls._unclaim(key, base)
            refreshed = await MediaItem.get(id=item.id)
            result = {
                "applied": True,
                "outcome": "rescraped",
                **base,
                "title": refreshed.title,
                "year": refreshed.year,
                "episodes": {"attempted": len(progress.get("episodes", []))}
                if preview["episodes"]
                else None,
                "organization": "queued" if item.lib.rename_template else "none",
                "path": path,
                "message": "Saved; organization runs in the background when queued.",
            }
        return await cls._record(key, result)

    @classmethod
    async def _checked(
        cls, body: MediaChangeApply, review: dict[str, Any], base: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Refuse a request whose inputs changed, are invalid or lack approval.

        Args:
            body: Confirmed request.
            review: Review recomputed from current state.
            base: Facts shared by every outcome.

        Returns:
            A refusal, or `None` when the request may be applied.
        """
        if review["revision"] != body.revision:
            return {
                "applied": False,
                **base,
                "stale": True,
                "message": "The target, workflow or metadata changed after the "
                "review; review it again.",
            }
        if not review["valid"]:
            return {
                "applied": False,
                **base,
                "valid": False,
                "issues": review["issues"],
            }
        if not body.confirmed:
            return {
                **review,
                **base,
                "request_id": str(body.request_id),
                "applied": False,
                "confirmation_required": True,
            }
        return None

    @staticmethod
    def _failed(base: dict[str, Any], error: str | None = None) -> dict[str, Any]:
        """Describe a change known not to have taken effect.

        Args:
            base: Facts shared by every outcome.
            error: Controlled error code, if known.

        Returns:
            A `failed` outcome that may be repeated.
        """
        return {
            "applied": False,
            **base,
            "failed": True,
            "error": error,
            "message": "Nothing was changed; the same request may be repeated.",
        }

    @classmethod
    async def _unclaim(
        cls, key: str, base: dict[str, Any], *, error: str | None = None
    ) -> dict[str, Any]:
        """Release a claim whose change definitely did not take effect.

        Args:
            key: Account-scoped request identity.
            base: Facts shared by every outcome.
            error: Controlled error code, if known.

        Returns:
            The `failed` outcome, or a recorded one if the claim cannot be released.
        """
        try:
            receipt = await MediaChangeReceipt.get(key=key)
            if receipt.action == MediaAction.SCAN:
                await receipt.delete()
            else:
                # the rescrape review keeps its candidate for a repeat
                await MediaChangeReceipt.filter(key=key).update(claimed=False)
            return cls._failed(base, error)
        except Exception:
            # a kept claim replays this failure instead of applying again
            logger.exception("Failed to release a media change claim")
            return await cls._record(
                key, {**cls._failed(base, error), "message": "Nothing was changed."}
            )

    @staticmethod
    async def _record(key: str, result: dict[str, Any]) -> dict[str, Any]:
        """Store an outcome so repeats read it instead of applying again.

        Args:
            key: Account-scoped request identity.
            result: Outcome to store.

        Returns:
            The outcome for this first call.
        """
        result = loads(dumps(result))
        try:
            await MediaChangeReceipt.filter(key=key).update(result=result)
        except Exception:
            # an unrecorded claim replays as uncertain, never as a new change
            logger.exception("Failed to record a media change")
        return {**result, "replayed": False}
