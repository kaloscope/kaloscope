"""Review and apply batches of library files and items for the assistant and MCP."""

import asyncio
import re
from collections import Counter
from collections.abc import Iterable
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from typing import Any

from filelock import Timeout as LockTimeout
from jinja2 import TemplateError
from sanic.log import logger
from tortoise import timezone
from tortoise.exceptions import DoesNotExist
from tortoise.functions import Count

from app.core.config import KaloscopeConfig
from app.core.dl.syncer import submission_lock
from app.core.exceptions import NotFoundException
from app.core.media.batch import (
    BATCH_LIMIT,
    SAMPLE_ROWS,
    FileScan,
    LibraryIndex,
    PlanEntry,
    build_index,
    delete_files,
    item_entries,
    item_matches,
    plan_deletion,
    plan_renames,
    prune_directories,
    relative,
    rename_files,
    scan_files,
    shown,
)
from app.core.media.common import ContentError
from app.core.media.coordination import library_lock
from app.core.media.organizer import (
    OrganizeDeferredError,
    OrganizePendingError,
    _fingerprint,
    organize_items,
    preview_organization,
    recover_organizing,
    rename_item,
    shared_validation,
)
from app.models.base import Pageable
from app.models.download import DownloadTask
from app.models.media import (
    FileOwner,
    MediaAction,
    MediaBatch,
    MediaBatchEntry,
    MediaChangeApply,
    MediaChangeReceipt,
    MediaItem,
    MediaLib,
)
from app.models.user import UserInfo
from app.services.base import BaseService
from app.services.media import MediaItemService, MediaLibService, _running_ingests
from app.utils.asyncio import write_in_thread
from app.utils.json import dumps, loads

# an unconfirmed plan is worthless once the library has moved on
PLAN_TTL = timedelta(hours=24)
PLAN_KEEP = 10
# loose files are removed in short lock holds so the watcher and scans interleave
FILE_CHUNK = 50
# organization previews stop here so a review answers within the tool timeout
PLAN_SECONDS = 10
BATCH_UNCERTAIN = (
    "The server stopped while this batch was running. Counts show what is known; "
    "review again to continue with what remains."
)


def _tally(values: Iterable[Any]) -> Counter[str]:
    """Count values under plain text keys, which stored JSON requires.

    Args:
        values: Enumeration members, reason codes or `None`.

    Returns:
        Counts keyed by each value's text; `None` counts as `unknown`.
    """
    return Counter("unknown" if value is None else str(value) for value in values)


class MediaBatchService(BaseService[MediaBatchEntry], model=MediaBatchEntry):
    """Freeze a selection into a plan, then apply exactly that plan once."""

    @staticmethod
    def _library_facts(lib: MediaLib) -> list[Any]:
        """Read the library settings a plan depends on.

        Args:
            lib: Library a plan was built for.

        Returns:
            Facts whose change makes every stored path meaningless.
        """
        return [lib.id, lib.dir, lib.lib_type, lib.rename_template]

    @staticmethod
    async def _index(lib: MediaLib) -> tuple[LibraryIndex, list[MediaItem]]:
        """Snapshot a library's items for classification.

        Args:
            lib: Library to read.

        Returns:
            Path lookups and the items they were built from.
        """
        items = await MediaItem.filter(lib_id=lib.id)
        return build_index(lib, items), items

    @staticmethod
    async def _busy(lib: MediaLib) -> tuple[set[int], set[str]]:
        """Find items and files that other work is still using.

        Args:
            lib: Library being planned.

        Returns:
            Item IDs with a queued or running ingest workflow, and files of
            downloads that are still downloading into or transferring to it.
        """
        _, items = await _running_ingests()
        paths: set[str] = set()
        root = Path(lib.dir)
        for task in await DownloadTask.all():
            if task.transfer_lib_id == lib.id and task.transfer_pending:
                paths.update((task.transfer_targets or {}).values())
            if task.completed_at is None and Path(task.dir).is_relative_to(root):
                paths.update(str(Path(task.dir) / name) for name in task.files or [])
        return items, paths

    @classmethod
    async def review(cls, change: MediaBatch, user: UserInfo) -> dict[str, Any]:
        """List matching files, or freeze a deletion plan for confirmation.

        Nothing is deleted. A review with an operation stores its plan so the
        confirmed batch is exactly what was shown, however the library changes.

        Args:
            change: Library, selector and optional operation.
            user: Current administrator.

        Returns:
            Without an operation: match counts by class and extension and a
            bounded sample. With one: planned and blocked units, the deletion
            mode, notes, located issues, the `revision` and, when valid, a
            `request_id` and `batch_id` for this exact plan.

        Raises:
            ForbiddenException: If the account cannot manage library files.
            NotFoundException: If the library does not exist.
        """
        from app.services.media_changes import (
            MediaChangeService,
            _digest,
            _require_admin,
        )

        _require_admin(user)
        lib = await MediaLibService.get_accessible(change.library_id, user)
        index, items = await cls._index(lib)
        base: dict[str, Any] = {
            "library_id": lib.id,
            "library_name": lib.name,
            "lib_type": lib.lib_type,
        }
        if change.operation is not None:
            base = {"action": MediaAction.BATCH, "operation": change.operation, **base}
        issues: list[dict[str, str]] = []
        scan = FileScan()
        entries: list[PlanEntry] = []
        unchanged = 0
        cut = False
        if change.operation == "organize":
            entries, issues, unchanged, cut = await cls._organize_plan(
                index, items, change, lib
            )
        elif change.item_ids and change.operation != "rename":
            entries, issues = await cls._selected(index, items, change, lib)
        else:
            if change.item_ids:
                chosen = set(change.item_ids)
                scan = await asyncio.to_thread(
                    item_matches,
                    index,
                    [
                        item
                        for item in items
                        if item.id in chosen or item.parent_id in chosen
                    ],
                )
                if not scan.matches:
                    issues.append(
                        {
                            "path": "item_ids",
                            "message": "No video file of this library is selected.",
                        }
                    )
            else:
                try:
                    scan = await asyncio.to_thread(
                        scan_files,
                        index,
                        directory=change.directory,
                        extensions=change.extensions,
                        name=change.name,
                        min_size=change.min_size,
                        max_size=change.max_size,
                        whole=change.operation is not None,
                    )
                except (OSError, ValueError):
                    issues.append(
                        {
                            "path": "directory",
                            "message": "This directory is not readable inside the "
                            "library.",
                        }
                    )
                if change.operation is None:
                    return cls._listing(base, index, scan, issues)
                if scan.truncated:
                    issues.append(
                        {
                            "path": "directory",
                            "message": "The library is too large to plan in one "
                            "pass; choose a narrower directory.",
                        }
                    )
            busy_items, busy_paths = await cls._busy(lib)
            if change.operation == "rename":
                try:
                    entries, unchanged = await asyncio.to_thread(
                        plan_renames,
                        index,
                        scan,
                        [FileOwner.INDEXED] if change.item_ids else change.owners,
                        change.pattern or "",
                        change.replacement,
                        templated=bool(lib.rename_template),
                        busy_items=busy_items,
                        busy_paths=busy_paths,
                    )
                except (re.error, TemplateError, ValueError, TypeError):
                    issues.append(
                        {
                            "path": "pattern",
                            "message": "The pattern or replacement is not a valid "
                            "rule for these names.",
                        }
                    )
            else:
                entries = plan_deletion(
                    index,
                    scan,
                    change.owners,
                    busy_items=busy_items,
                    busy_paths=busy_paths,
                )
        if change.operation is None:
            return cls._selection(base, entries, issues)

        planned = [entry for entry in entries if entry.status == "planned"]
        blocked = [entry for entry in entries if entry.status == "blocked"]
        # one item may be matched through several of its files
        seen: set[int] = set()
        unique = []
        for entry in planned:
            if entry.kind == "item":
                if entry.item_id in seen:
                    continue
                seen.add(entry.item_id or 0)
            unique.append(entry)
        more = cut or len(unique) > BATCH_LIMIT
        planned = unique[:BATCH_LIMIT]
        if not planned and not issues:
            issues.append(
                {
                    "path": "owners" if blocked else "library_id",
                    "message": "Nothing would change: every match is blocked."
                    if blocked
                    else "Every selected name already matches."
                    if unchanged
                    else "Nothing matches this selection.",
                }
            )
        deleting = change.operation == "delete"
        trash = KaloscopeConfig.get().filesystem_trash_mode
        reasons = _tally(entry.reason for entry in blocked)
        owners = _tally(entry.owner for entry in planned)
        notes = []
        if deleting:
            notes.append(
                "Files go to the system trash."
                if trash
                else "Files are deleted permanently; this cannot be undone."
            )
        elif change.operation == "rename":
            notes.append(
                "Each video's NFO, subtitles, artwork and danmaku cache take its "
                "new name; nothing is moved to another folder."
            )
        else:
            notes.append(
                "Works are renamed and moved as the naming template and their "
                "saved metadata say; no metadata is fetched."
            )
        if unchanged:
            notes.append(
                f"{unchanged} already have the resulting name and are left out."
            )
        if cut:
            notes.append(
                "Planning stopped at its time limit; review again after applying "
                "for the remaining works."
            )
        if deleting and owners[FileOwner.INDEXED]:
            notes.append(
                f"{owners[FileOwner.INDEXED]} indexed items are removed from the "
                "library with their watch or reading history and their own "
                "metadata, subtitles and artwork."
            )
        if deleting and owners[FileOwner.COMPANION]:
            notes.append(
                f"{owners[FileOwner.COMPANION]} files are metadata, subtitles or "
                "artwork of items that stay; those items lose them, and a later "
                "scan may fetch metadata again."
            )
        if deleting and owners[FileOwner.PAGE]:
            notes.append(
                f"{owners[FileOwner.PAGE]} files are comic pages; their comics are "
                "indexed again afterwards."
            )
        if deleting and owners[FileOwner.UNINDEXED]:
            notes.append(
                f"{owners[FileOwner.UNINDEXED]} files are content the library "
                "has not indexed."
            )
        if reasons["owner_excluded"]:
            notes.append(
                f"{reasons['owner_excluded']} matches belong to other file classes "
                "and are not included; name them in owners to include them."
            )
        if len(unique) > BATCH_LIMIT:
            notes.append(
                f"Only the first {BATCH_LIMIT} of {len(unique)} units are in this "
                "batch; review the same selection again afterwards for the rest."
            )
        revision = _digest(
            [
                cls._library_facts(lib),
                change.model_dump(mode="json"),
                [
                    [entry.kind, entry.src, entry.dst, entry.identity]
                    for entry in planned
                ],
            ]
        )
        result: dict[str, Any] = {
            "valid": not issues,
            "issues": issues,
            **base,
            "matched": {"units": len(unique) + len(blocked)},
            "planned": {
                "units": len(planned),
                "files": sum(entry.files for entry in planned),
                "bytes": sum(entry.size or 0 for entry in planned),
                "by_owner": dict(owners),
            },
            "blocked": {"units": len(blocked), "reasons": dict(reasons)},
            "more": more,
            "unchanged": unchanged,
            "deletion": ("trash" if trash else "permanent") if deleting else None,
            "sample": cls._sample([*blocked, *planned]),
            "omitted": max(0, len(blocked) + len(planned) - SAMPLE_ROWS),
            "notes": notes,
            "revision": revision,
        }
        if not issues:
            request_id = MediaChangeService.change_request(user.id, change, revision)
            receipt = await cls._store(
                MediaChangeService.request_key(user.id, request_id),
                lib,
                change,
                revision,
                result,
                user,
                [*planned, *blocked[:BATCH_LIMIT]],
            )
            result.update(request_id=str(request_id), batch_id=receipt.id)
        return loads(dumps(result))

    @staticmethod
    def _sample(entries: list[PlanEntry]) -> list[dict[str, Any]]:
        """Show the first entries of a plan in a bounded form.

        Args:
            entries: Entries in display order.

        Returns:
            At most `SAMPLE_ROWS` rows with shortened paths.
        """
        rows = []
        for entry in entries[:SAMPLE_ROWS]:
            row: dict[str, Any] = {
                "src": shown(entry.src),
                "size": entry.size,
                "owner": entry.owner,
            }
            if entry.dst is not None:
                row["dst"] = shown(entry.dst)
            if entry.item_id is not None:
                row["item_id"] = entry.item_id
            if entry.reason:
                row["blocked"] = entry.reason
            rows.append(row)
        return rows

    @classmethod
    def _listing(
        cls,
        base: dict[str, Any],
        index: LibraryIndex,
        scan: FileScan,
        issues: list[dict[str, str]],
    ) -> dict[str, Any]:
        """Summarize matched files without planning anything.

        Args:
            base: Library facts shared by every result.
            index: Library lookups used for the walk.
            scan: Walk result.
            issues: Problems reading the selection.

        Returns:
            Counts by class and by the ten commonest extensions, and a sample.
        """
        extensions = Counter(
            match.path.suffix.casefold() or "(none)" for match in scan.matches
        )
        common = dict(extensions.most_common(10))
        if len(extensions) > 10:
            common["(other)"] = len(scan.matches) - sum(common.values())
        sample = []
        for match in scan.matches[:SAMPLE_ROWS]:
            row: dict[str, Any] = {
                "path": shown(relative(index.root, match.path)),
                "size": match.size,
                "owner": match.owner,
            }
            if match.item_id is not None:
                row["item_id"] = match.item_id
            sample.append(row)
        return loads(
            dumps(
                {
                    **base,
                    "issues": issues,
                    "matched": {
                        "files": len(scan.matches),
                        "bytes": sum(match.size for match in scan.matches),
                    },
                    "by_owner": dict(_tally(match.owner for match in scan.matches)),
                    "by_extension": common,
                    "sample": sample,
                    "omitted": max(0, len(scan.matches) - SAMPLE_ROWS),
                    "truncated": scan.truncated,
                }
            )
        )

    @classmethod
    def _selection(
        cls,
        base: dict[str, Any],
        entries: list[PlanEntry],
        issues: list[dict[str, str]],
    ) -> dict[str, Any]:
        """Summarize explicitly selected items without planning anything.

        Args:
            base: Library facts shared by every result.
            entries: Units the selected items would form.
            issues: Problems resolving the selection.

        Returns:
            The item count and a sample.
        """
        return loads(
            dumps(
                {
                    **base,
                    "issues": issues,
                    "matched": {
                        "items": len(entries),
                        "files": sum(entry.files for entry in entries),
                        "bytes": sum(entry.size or 0 for entry in entries),
                    },
                    "sample": cls._sample(entries),
                    "omitted": max(0, len(entries) - SAMPLE_ROWS),
                    "truncated": False,
                }
            )
        )

    @classmethod
    async def _selected(
        cls,
        index: LibraryIndex,
        items: list[MediaItem],
        change: MediaBatch,
        lib: MediaLib,
    ) -> tuple[list[PlanEntry], list[dict[str, str]]]:
        """Resolve explicitly selected items into units.

        Args:
            index: Library lookups.
            items: Every item of the library.
            change: Selection naming item IDs.
            lib: Library the items must belong to.

        Returns:
            Units for the selected items, and an issue naming IDs that are not
            items of this library. A child whose parent is also selected is
            covered by the parent.
        """
        rows = {item.id: item for item in items}
        missing = [item_id for item_id in change.item_ids if item_id not in rows]
        issues = (
            [
                {
                    "path": "item_ids",
                    "message": "Not items of this library: "
                    + ", ".join(map(str, missing[:10])),
                }
            ]
            if missing
            else []
        )
        chosen = set(change.item_ids) - set(missing)
        children: dict[int, list[MediaItem]] = {}
        for item in items:
            if item.parent_id is not None:
                children.setdefault(item.parent_id, []).append(item)
        top = [
            rows[item_id] for item_id in chosen if rows[item_id].parent_id not in chosen
        ]
        busy_items, _ = await cls._busy(lib)
        entries = await asyncio.to_thread(
            item_entries, index, top, children, busy_items=busy_items
        )
        return entries, issues

    @classmethod
    async def _organize_plan(
        cls,
        index: LibraryIndex,
        items: list[MediaItem],
        change: MediaBatch,
        lib: MediaLib,
    ) -> tuple[list[PlanEntry], list[dict[str, str]], int, bool]:
        """Preview organizing the selected works under the saved naming template.

        Args:
            index: Library lookups.
            items: Every item of the library.
            change: Selection naming works by ID or directory, or the whole library.
            lib: Library whose template is applied.

        Returns:
            One `group` unit per work that would move, with its expected paths
            as identity; works the organizer would defer, skip or refuse as
            blocked matches; located issues; the number of works already in
            place; and whether planning stopped at its time limit.
        """
        issues: list[dict[str, str]] = []
        if not lib.rename_template:
            return (
                [],
                [
                    {
                        "path": "library_id",
                        "message": "This library has no naming template; set one in "
                        "its settings first.",
                    }
                ],
                0,
                False,
            )
        works = [item for item in items if item.parent_id is None]
        if change.item_ids:
            chosen = set(change.item_ids)
            known = {item.id for item in works}
            if chosen - known:
                issues.append(
                    {
                        "path": "item_ids",
                        "message": "Select top-level works of this library: "
                        + ", ".join(map(str, sorted(chosen - known)[:10])),
                    }
                )
            works = [item for item in works if item.id in chosen]
        elif change.directory:
            scope = index.root / change.directory
            works = [
                item
                for item in works
                if Path(item.path) == scope or Path(item.path).is_relative_to(scope)
            ]
        entries: list[PlanEntry] = []
        unchanged = 0
        cut = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + PLAN_SECONDS
        try:
            async with shared_validation(lib):
                for work in sorted(works, key=lambda item: item.path):
                    if loop.time() > deadline or len(entries) > 2 * BATCH_LIMIT:
                        cut = True
                        break
                    preview = await preview_organization(lib, lib.rename_template, work)
                    if preview["status"] == "unchanged":
                        unchanged += 1
                        continue
                    pairs = [
                        [
                            relative(index.root, pair["current"]),
                            relative(index.root, pair["proposed"]),
                        ]
                        for pair in preview["paths"]
                    ]
                    entry = PlanEntry(
                        "group",
                        FileOwner.INDEXED,
                        relative(index.root, work.path),
                        work.size,
                        pairs,
                        work.id,
                        files=max(1, preview["moved_files"]),
                    )
                    if pairs:
                        target = Path(pairs[0][1])
                        entry.dst = (
                            target.parent.as_posix()
                            if work.path == work.dir
                            else target.as_posix()
                        )
                    if preview["status"] != "moves":
                        entry.status = "blocked"
                        entry.reason = f"organize_{preview['status']}"
                    entries.append(entry)
        except OSError:
            issues.append(
                {
                    "path": "library_id",
                    "message": "The library directory could not be read.",
                }
            )
        return entries, issues, unchanged, cut

    @classmethod
    async def _store(
        cls,
        key: str,
        lib: MediaLib,
        change: MediaBatch,
        revision: str,
        result: dict[str, Any],
        user: UserInfo,
        entries: list[PlanEntry],
    ) -> MediaChangeReceipt:
        """Persist a reviewed plan and drop plans nobody confirmed.

        Args:
            key: Account-scoped request identity.
            lib: Library the plan belongs to.
            change: Reviewed selection and operation.
            revision: Reviewed revision.
            result: Review shown to the user.
            user: Reviewing administrator.
            entries: Planned units followed by blocked matches.

        Returns:
            The unclaimed receipt holding the plan header.
        """
        stale = await MediaChangeReceipt.filter(
            action=MediaAction.BATCH, claimed=False
        ).order_by("-id")
        cutoff = timezone.now() - PLAN_TTL
        expired = [
            row.id
            for position, row in enumerate(stale)
            if position >= PLAN_KEEP - 1 or (row.created_at and row.created_at < cutoff)
        ]
        if expired:
            await MediaChangeReceipt.filter(id__in=expired, claimed=False).delete()
        receipt = await MediaChangeReceipt.create(
            key=key,
            action=MediaAction.BATCH,
            lib_id=lib.id,
            plan={
                "operation": change.operation,
                "change": change.model_dump(mode="json"),
                "revision": revision,
                "library": cls._library_facts(lib),
                "library_name": lib.name,
                "user_id": user.id,
                "deletion": result["deletion"],
                "planned": result["planned"],
                "blocked": result["blocked"],
                "more": result["more"],
                "stop": False,
            },
        )
        await MediaBatchEntry.bulk_create(
            [
                MediaBatchEntry(
                    receipt_id=receipt.id,
                    seq=seq,
                    kind=entry.kind,
                    owner=entry.owner,
                    item_id=entry.item_id,
                    src=entry.src,
                    dst=entry.dst,
                    files=entry.files,
                    size=entry.size,
                    identity=entry.identity,
                    status=entry.status,
                    reason=entry.reason,
                )
                for seq, entry in enumerate(entries, 1)
            ],
            batch_size=500,
        )
        return receipt

    @classmethod
    async def counts(cls, receipt_id: int) -> dict[str, int]:
        """Count a batch's entries by status.

        Args:
            receipt_id: Batch identity.

        Returns:
            `done`, `skipped`, `failures`, `uncertain` and `remaining` unit
            counts. Entries still marked as started count as remaining.
        """
        rows = (
            await MediaBatchEntry.filter(receipt_id=receipt_id)
            .annotate(total=Count("id"))
            .group_by("status")
            .values("status", "total")
        )
        found = {row["status"]: row["total"] for row in rows}
        return {
            "done": found.get("done", 0),
            "skipped": found.get("skipped", 0),
            "failures": found.get("failed", 0),
            "uncertain": found.get("uncertain", 0),
            "remaining": found.get("planned", 0) + found.get("started", 0),
        }

    @classmethod
    async def _summary(cls, receipt: MediaChangeReceipt) -> dict[str, Any]:
        """Describe a batch's progress with grouped reasons and a sample.

        Args:
            receipt: Batch header.

        Returns:
            Shared identity, unit counts, reasons of units that were not applied
            and a few of their paths.
        """
        plan = receipt.plan or {}
        unfinished = await MediaBatchEntry.filter(
            receipt_id=receipt.id, status__in=("skipped", "failed", "uncertain")
        ).order_by("seq")
        return {
            "action": MediaAction.BATCH,
            "operation": plan.get("operation"),
            "library_id": receipt.lib_id,
            "library_name": plan.get("library_name"),
            "batch_id": receipt.id,
            **await cls.counts(receipt.id),
            "reasons": dict(_tally(entry.reason for entry in unfinished)),
            "sample": [
                {
                    "src": shown(entry.src),
                    "status": entry.status,
                    "reason": entry.reason,
                }
                for entry in unfinished[:10]
            ],
            "path": f"/medialibs/{receipt.lib_id}",
        }

    @classmethod
    async def running(cls, receipt: MediaChangeReceipt | None) -> dict[str, Any]:
        """Report a batch that another call is still applying.

        Args:
            receipt: Claimed batch, if its claim is already visible.

        Returns:
            A `running` outcome with the counts so far.
        """
        summary = await cls._summary(receipt) if receipt is not None else {}
        return loads(
            dumps(
                {
                    "applied": None,
                    "outcome": "running",
                    "action": MediaAction.BATCH,
                    **summary,
                    "message": "Still running in the background; repeat this request "
                    "later to read its outcome. Do not review it again.",
                }
            )
        )

    @classmethod
    async def apply(
        cls,
        body: MediaChangeApply,
        change: MediaBatch,
        key: str,
        user: UserInfo,
        receipt: MediaChangeReceipt | None,
    ) -> dict[str, Any]:
        """Recheck, claim and run one reviewed batch.

        The caller holds this request's locks and has already replayed a claimed
        receipt. Only the stored plan runs; the selector is never evaluated again.

        Args:
            body: Confirmed request.
            change: Reviewed selection and operation.
            key: Account-scoped request identity.
            user: Current administrator.
            receipt: Unclaimed review holding the plan, if it still exists.

        Returns:
            A `completed`, `partial` or `cancelled` outcome with unit counts, or
            a refusal: `stale`, `absent`, `confirmation_required` or `pending`
            while another batch runs on the library.
        """
        from app.services.media_changes import MediaChangeService

        base: dict[str, Any] = {
            "action": MediaAction.BATCH,
            "operation": change.operation,
            "library_id": change.library_id,
        }
        lib = await MediaLib.get_or_none(id=change.library_id)
        if lib is None:
            return {
                "applied": False,
                "outcome": "absent",
                **base,
                "message": "The library no longer exists; nothing was changed.",
            }
        await MediaLibService.get_accessible(lib.id, user)
        plan = (receipt.plan if receipt else None) or {}
        if (
            receipt is None
            or plan.get("revision") != body.revision
            or plan.get("library") != cls._library_facts(lib)
        ):
            return {
                "applied": False,
                **base,
                "stale": True,
                "message": "The reviewed plan expired or the library changed; "
                "review it again.",
            }
        base.update(library_name=lib.name, batch_id=receipt.id)
        if not body.confirmed:
            return {
                **base,
                "planned": plan["planned"],
                "blocked": plan["blocked"],
                "deletion": plan["deletion"],
                "request_id": str(body.request_id),
                "revision": body.revision,
                "applied": False,
                "confirmation_required": True,
            }
        try:
            with submission_lock(f"media-batch:{lib.id}"):
                claimed = await MediaChangeReceipt.filter(
                    key=key, claimed=False
                ).update(claimed=True)
                if not claimed:
                    return await MediaChangeService._replay(
                        await MediaChangeReceipt.get(key=key), user
                    )
                stopped = await cls._run(receipt, lib)
        except LockTimeout:
            return {
                "applied": False,
                **base,
                "pending": True,
                "message": "Another batch is running on this library; repeat this "
                "request when it has finished.",
            }
        summary = await cls._summary(receipt)
        done, left = summary["done"], summary["remaining"]
        problems = summary["skipped"] + summary["failures"] + summary["uncertain"]
        outcome = (
            "cancelled" if stopped else "partial" if problems or left else "completed"
        )
        return await MediaChangeService._record(
            key,
            {
                "applied": bool(done),
                "outcome": outcome,
                **summary,
                "deletion": plan["deletion"],
                "message": {
                    "completed": "Every planned unit was applied.",
                    "partial": "Some units were skipped or failed; their reasons "
                    "are listed. Review again to retry what remains.",
                    "cancelled": "Stopped on request; the remaining units were "
                    "not touched.",
                }[outcome],
            },
        )

    @classmethod
    async def _stopped(cls, receipt_id: int) -> bool:
        """Read a running batch's stop flag.

        Args:
            receipt_id: Batch identity.

        Returns:
            Whether a stop was requested.
        """
        receipt = await MediaChangeReceipt.get_or_none(id=receipt_id)
        return receipt is None or bool((receipt.plan or {}).get("stop"))

    @classmethod
    async def _run(cls, receipt: MediaChangeReceipt, lib: MediaLib) -> bool:
        """Apply the planned units in order, recording each outcome.

        Args:
            receipt: Claimed batch.
            lib: Library as reviewed.

        Returns:
            Whether the batch stopped early on request.
        """
        root = Path(lib.dir).absolute()
        entries = await MediaBatchEntry.filter(
            receipt_id=receipt.id, status="planned"
        ).order_by("seq")
        emptied: set[Path] = set()
        position = 0
        stopped = False
        while position < len(entries):
            if await cls._stopped(receipt.id):
                stopped = True
                break
            if entries[position].kind != "file":
                chunk = [entries[position]]
            else:
                chunk = []
                for entry in entries[position : position + FILE_CHUNK]:
                    if entry.kind != "file":
                        break
                    chunk.append(entry)
            position += len(chunk)
            ids = [entry.id for entry in chunk]
            await MediaBatchEntry.filter(id__in=ids).update(status="started")
            operation = (receipt.plan or {}).get("operation")
            try:
                if chunk[0].kind == "group":
                    outcomes = {chunk[0].id: await cls._organize_work(chunk[0], lib)}
                elif chunk[0].kind == "item":
                    outcomes = {
                        chunk[0].id: await (
                            cls._rename_item(chunk[0], lib, root)
                            if operation == "rename"
                            else cls._delete_item(chunk[0], root)
                        )
                    }
                elif operation == "rename":
                    outcomes = await cls._rename_files(chunk, lib, root)
                else:
                    outcomes = await cls._delete_files(chunk, lib, root)
            except Exception:
                logger.exception("Failed to apply a media batch chunk")
                outcomes = dict.fromkeys(ids, "error")
            for outcome in set(outcomes.values()):
                matching = [key for key, value in outcomes.items() if value == outcome]
                await MediaBatchEntry.filter(id__in=matching).update(
                    status="done"
                    if outcome == "done"
                    else "failed"
                    if outcome
                    in ("io_error", "error", "organize_pending", "organize_blocked")
                    else "skipped",
                    reason=None if outcome == "done" else outcome,
                )
            if operation == "delete":
                emptied.update(
                    (root / entry.src).parent
                    for entry in chunk
                    if entry.kind == "file" and outcomes.get(entry.id) == "done"
                )
        if emptied:
            try:
                async with library_lock(lib.dir):
                    kept = set()
                    for item in await MediaItem.filter(lib_id=lib.id).only(
                        "dir", "path"
                    ):
                        kept.update((item.dir, item.path))
                    await write_in_thread(prune_directories, root, emptied, kept)
            except Exception:
                logger.exception("Failed to prune directories emptied by a batch")
        return stopped

    @staticmethod
    async def _delete_files(
        chunk: list[MediaBatchEntry], lib: MediaLib, root: Path
    ) -> dict[int, str]:
        """Delete a chunk of reviewed files under the library lock.

        Args:
            chunk: File entries marked as started.
            lib: Library as reviewed.
            root: Its absolute root.

        Returns:
            Each entry's outcome: `done` or the reason it was not deleted.
        """
        async with library_lock(lib.dir):
            current = await MediaLib.get_or_none(id=lib.id)
            if current is None or (current.dir, current.lib_type) != (
                lib.dir,
                lib.lib_type,
            ):
                return {entry.id: "library_changed" for entry in chunk}
            await recover_organizing(current)
            # a file that became an item's body since the review is left alone
            indexed = set(
                await MediaItem.filter(
                    lib_id=lib.id,
                    path__in=[
                        str(root / entry.src)
                        for entry in chunk
                        if entry.owner != FileOwner.INDEXED
                    ],
                ).values_list("path", flat=True)
            )
            outcomes = {
                entry.id: "item_changed"
                for entry in chunk
                if str(root / entry.src) in indexed
            }
            outcomes.update(
                await write_in_thread(
                    delete_files,
                    root,
                    [
                        (entry.id, entry.src, entry.identity or [])
                        for entry in chunk
                        if entry.id not in outcomes
                    ],
                )
            )
            return outcomes

    @staticmethod
    async def _rename_files(
        chunk: list[MediaBatchEntry], lib: MediaLib, root: Path
    ) -> dict[int, str]:
        """Rename a chunk of reviewed loose files under the library lock.

        Args:
            chunk: File entries marked as started.
            lib: Library as reviewed.
            root: Its absolute root.

        Returns:
            Each entry's outcome: `done` or the reason it was not renamed.
        """
        async with library_lock(lib.dir):
            current = await MediaLib.get_or_none(id=lib.id)
            if current is None or (current.dir, current.lib_type) != (
                lib.dir,
                lib.lib_type,
            ):
                return {entry.id: "library_changed" for entry in chunk}
            await recover_organizing(current)
            # a file that became an item's body since the review is left alone
            indexed = set(
                await MediaItem.filter(
                    lib_id=lib.id, path__in=[str(root / entry.src) for entry in chunk]
                ).values_list("path", flat=True)
            )
            outcomes = {
                entry.id: "item_changed"
                for entry in chunk
                if str(root / entry.src) in indexed
            }
            outcomes.update(
                await write_in_thread(
                    rename_files,
                    root,
                    [
                        (entry.id, entry.src, entry.dst or "", entry.identity or [])
                        for entry in chunk
                        if entry.id not in outcomes
                    ],
                )
            )
            return outcomes

    @staticmethod
    async def _rename_item(entry: MediaBatchEntry, lib: MediaLib, root: Path) -> str:
        """Rename one reviewed video and its companions through the organizer.

        Args:
            entry: Item entry marked as started.
            lib: Library as reviewed.
            root: Its absolute root.

        Returns:
            `done`, or the reason the video was not renamed.
        """
        async with library_lock(lib.dir):
            current = await MediaLib.get_or_none(id=lib.id)
            if current is None or (
                current.dir,
                current.lib_type,
                current.rename_template,
            ) != (lib.dir, lib.lib_type, lib.rename_template):
                return "library_changed"
            await recover_organizing(current)
            item = await MediaItem.get_or_none(id=entry.item_id, lib_id=lib.id)
            if item is None:
                return "missing"
            if item.path != str(root / entry.src):
                return "item_changed"
            try:
                if await asyncio.to_thread(_fingerprint, Path(item.path)) != (
                    entry.identity or []
                ):
                    return "content_changed"
                await rename_item(current, item, Path(entry.dst or "").stem)
            except FileNotFoundError:
                return "missing"
            except OrganizeDeferredError:
                return "organize_deferred"
            except OrganizePendingError:
                return "organize_pending"
            except ValueError:
                return "organize_blocked"
            except OSError:
                return "io_error"
        return "done"

    @staticmethod
    async def _organize_work(entry: MediaBatchEntry, lib: MediaLib) -> str:
        """Organize one reviewed work if it would still move as it was shown.

        Args:
            entry: Group entry marked as started.
            lib: Library as reviewed.

        Returns:
            `done`, or the reason the work was not organized.
        """
        root = Path(lib.dir).absolute()
        async with library_lock(lib.dir):
            current = await MediaLib.get_or_none(id=lib.id)
            if (
                current is None
                or not current.rename_template
                or (current.dir, current.lib_type, current.rename_template)
                != (lib.dir, lib.lib_type, lib.rename_template)
            ):
                return "library_changed"
            try:
                await recover_organizing(current)
                item = await MediaItem.get_or_none(id=entry.item_id, lib_id=lib.id)
                if item is None:
                    return "missing"
                preview = await preview_organization(
                    current, current.rename_template, item
                )
                pairs = [
                    [relative(root, pair["current"]), relative(root, pair["proposed"])]
                    for pair in preview["paths"]
                ]
                if preview["status"] != "moves" or pairs != entry.identity:
                    return "content_changed"
                await organize_items(current, [item.id])
                item = await MediaItem.get_or_none(id=entry.item_id, lib_id=lib.id)
                if item is not None:
                    after = await preview_organization(
                        current, current.rename_template, item
                    )
                    if after["status"] != "unchanged":
                        return f"organize_{after['status']}"
            except OrganizeDeferredError:
                return "organize_deferred"
            except OrganizePendingError:
                return "organize_pending"
            except OSError:
                return "io_error"
        return "done"

    @staticmethod
    async def _delete_item(entry: MediaBatchEntry, root: Path) -> str:
        """Delete one reviewed item with its files through the library's own rules.

        Args:
            entry: Item entry marked as started.
            root: Absolute library root.

        Returns:
            `done`, or the reason the item was not deleted.
        """
        if entry.item_id is None:
            return "missing"
        _, busy = await _running_ingests()
        if entry.item_id in busy:
            return "ingesting"
        try:
            await MediaItemService.delete(
                entry.item_id,
                local=True,
                expected=(str(root / entry.src), entry.identity or []),
            )
        except DoesNotExist:
            return "missing"
        except ContentError as error:
            return error.code
        except OrganizePendingError:
            return "organize_pending"
        except OSError:
            return "io_error"
        return "done"

    @classmethod
    async def replay(
        cls, receipt: MediaChangeReceipt, user: UserInfo, *, settle: bool = False
    ) -> dict[str, Any]:
        """Read a claimed batch's outcome without applying anything.

        Args:
            receipt: Claimed batch.
            user: Current administrator.
            settle: Whether the caller holds this request's locks, which proves
                no runner is alive. A batch without a recorded outcome is then
                recorded as `interrupted`; otherwise it is reported as running.

        Returns:
            The recorded outcome with current counts, a `running` report, or
            an `interrupted` outcome after a server stop.
        """
        from app.services.media_changes import MediaChangeService

        with suppress(NotFoundException):
            await MediaLibService.get_accessible(receipt.lib_id, user)
        if receipt.result is None:
            if not settle:
                return await cls.running(receipt)
            await cls._settle(receipt)
            summary = await cls._summary(receipt)
            await MediaChangeService._record(
                receipt.key,
                {
                    "applied": True if summary["done"] else None,
                    "outcome": "interrupted",
                    **summary,
                    "message": BATCH_UNCERTAIN,
                },
            )
            receipt = await MediaChangeReceipt.get(id=receipt.id)
        return loads(
            dumps(
                {
                    **(receipt.result or {}),
                    "replayed": True,
                    "current": await cls.counts(receipt.id),
                }
            )
        )

    @staticmethod
    async def _settle(receipt: MediaChangeReceipt):
        """Resolve entries a stopped server left marked as started.

        Args:
            receipt: Claimed batch with no runner alive.
        """
        lib = await MediaLib.get_or_none(id=receipt.lib_id)
        for entry in await MediaBatchEntry.filter(
            receipt_id=receipt.id, status="started"
        ):
            status = "uncertain"
            if entry.kind == "item" and entry.dst is None:
                if not await MediaItem.exists(id=entry.item_id):
                    status = "done"
            elif entry.kind == "item" and lib is not None:
                # a journaled rename is finished by organizer recovery
                item = await MediaItem.get_or_none(id=entry.item_id)
                if item is not None and item.path == str(Path(lib.dir) / entry.dst):
                    status = "done"
                elif item is not None and item.path == str(Path(lib.dir) / entry.src):
                    status = "planned"
            elif entry.kind == "file" and lib is not None:
                path = Path(lib.dir) / entry.src
                present = await asyncio.to_thread(
                    lambda path=path: path.exists() or path.is_symlink()
                )
                status = "planned" if present else "done"
            await MediaBatchEntry.filter(id=entry.id).update(
                status=status, reason="interrupted" if status == "uncertain" else None
            )

    @classmethod
    async def details(cls, id: int) -> dict[str, Any]:
        """Read one batch's header, progress and outcome for the plan dialog.

        Args:
            id: Batch identity.

        Returns:
            The reviewed plan header, unit counts, whether it is `reviewed`,
            `running` or `finished`, and its recorded outcome.

        Raises:
            NotFoundException: If no batch has this identity.
        """
        receipt = await MediaChangeReceipt.get_or_none(id=id, action=MediaAction.BATCH)
        if receipt is None or receipt.plan is None:
            raise NotFoundException
        plan = receipt.plan
        return loads(
            dumps(
                {
                    "id": receipt.id,
                    "library_id": receipt.lib_id,
                    "library_name": plan.get("library_name"),
                    "operation": plan.get("operation"),
                    "deletion": plan.get("deletion"),
                    "planned": plan.get("planned"),
                    "blocked": plan.get("blocked"),
                    "more": plan.get("more"),
                    "state": "reviewed"
                    if not receipt.claimed
                    else "finished"
                    if receipt.result is not None
                    else "running",
                    "stop_requested": bool(plan.get("stop")),
                    "counts": await cls.counts(receipt.id),
                    "outcome": (receipt.result or {}).get("outcome"),
                    "created_at": receipt.created_at,
                }
            )
        )

    @classmethod
    async def entries(
        cls, id: int, query: Pageable, status: str | None = None
    ) -> dict[str, Any]:
        """Page through one batch's entries.

        Args:
            id: Batch identity.
            query: Page number and size.
            status: Only entries with this status, if given.

        Returns:
            The total and one page in plan order, blocked entries after planned.

        Raises:
            NotFoundException: If no batch has this identity.
        """
        if not await MediaChangeReceipt.exists(id=id, action=MediaAction.BATCH):
            raise NotFoundException
        filters: dict[str, Any] = {"receipt_id": id}
        if status:
            filters["status"] = status
        page = await MediaBatchEntry.page(
            page_num=query.page_num,
            page_size=query.page_size,
            ordering="seq",
            **filters,
        )
        return {
            "total": page.total,
            "items": [
                {
                    "seq": entry.seq,
                    "kind": entry.kind,
                    "owner": entry.owner,
                    "item_id": entry.item_id,
                    "src": entry.src,
                    "dst": entry.dst,
                    "files": entry.files,
                    "size": entry.size,
                    "status": entry.status,
                    "reason": entry.reason,
                }
                for entry in page.items
            ],
        }

    @classmethod
    async def cancel(cls, id: int) -> dict[str, Any]:
        """Ask a running batch to stop before its next unit.

        Args:
            id: Batch identity.

        Returns:
            The batch's details after the request. A batch that is not running
            is unchanged.

        Raises:
            NotFoundException: If no batch has this identity.
        """
        receipt = await MediaChangeReceipt.get_or_none(id=id, action=MediaAction.BATCH)
        if receipt is None or receipt.plan is None:
            raise NotFoundException
        if receipt.claimed and receipt.result is None:
            await MediaChangeReceipt.filter(id=id).update(
                plan={**receipt.plan, "stop": True}
            )
        return await cls.details(id)
