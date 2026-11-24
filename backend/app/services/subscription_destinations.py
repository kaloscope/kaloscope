"""Bind ordinary subscription questions to the downloader and library choices shown."""

import re
import sys
from pathlib import PurePosixPath, PureWindowsPath

from tortoise.expressions import Q
from tortoise.queryset import QuerySet

from app.models.assistant import media_resource
from app.models.download import DEFAULT_TRANSFER_NAMING, DownloadDir
from app.models.media import MediaLib
from app.models.user import UserInfo
from app.services.media import accessible_library_ids
from app.utils.json import json_digest


def libraries(user: UserInfo) -> QuerySet[MediaLib]:
    """Query libraries within the current account and token grants.

    Args:
        user: Authenticated assistant user.

    Returns:
        Permission-filtered library query.
    """
    allowed = accessible_library_ids(user)
    return MediaLib.all() if allowed is None else MediaLib.filter(id__in=allowed)


async def destination_questions(
    plan: dict, prompt: str, user: UserInfo, *, chinese: bool
) -> list[dict]:
    """Offer the unresolved destinations using the ordinary question vocabulary.

    Args:
        plan: Validated settings, including the proposed downloader directory.
        prompt: User order and prior preferences.
        user: Account whose libraries may appear on the card.
        chinese: Whether to present Chinese labels.

    Returns:
        Questions with server-side option values for the subscription adapter.
    """
    directory = plan["dir"]
    paths = list(
        dict.fromkeys(
            [
                directory,
                *await DownloadDir.all()
                .order_by("-last_used")
                .limit(3)
                .values_list("path", flat=True),
            ]
        )
    )
    directory_known = bool(
        re.search(
            r"""(?:^|[\s,，:：=“「《"'（(]|保存到|下载到|存到|存入)"""
            + re.escape(directory)
            + r"""(?=$|[\s，,。；;！!、」》”"'）)]|\.(?:\s|$))""",
            prompt,
        )
    )
    lib_id = plan.get("transfer_lib_id")
    available = (
        await libraries(user)
        .order_by("priority", "id")
        .limit(7)
        .values("id", "name", "dir")
    )
    fixed_library = await libraries(user).filter(id=lib_id).first() if lib_id else None
    no_transfer = bool(
        re.search(
            r"(?:无需|不必|不用|不要|不)(?:转存|入库)|仅下载"
            r"|\b(?:no (?:transfer|library)|download only)\b",
            prompt,
            re.I,
        )
    )
    library_known = bool(fixed_library or no_transfer or not available)
    only = "仅下载，不入库" if chinese else "Download only"
    target = fixed_library.name if fixed_library else only
    method = plan.get("transfer_method") or (
        "symlink" if sys.platform == "win32" else "hardlink"
    )
    method_name = (
        {
            "hardlink": "硬链接",
            "symlink": "符号链接",
            "copy": "复制",
            "move": "移动",
        }.get(method, method)
        if chinese
        else method
    )
    if fixed_library:
        target = f"{target} · {method_name}"

    questions = []
    combined = not directory_known and not library_known and len(paths) == 1
    # a sole candidate needs no question; the confirmation still shows it
    if not directory_known and len(paths) > 1:
        questions.append(
            {
                "field": "directory",
                "title": "下载到哪个目录？"
                if chinese
                else "Which downloader directory?",
                "options": [
                    {
                        "label": path,
                        "detail": target
                        if library_known
                        else ("下载器上的目录" if chinese else "On the downloader"),
                        "value": path,
                    }
                    for path in paths
                ],
            }
        )
    if not library_known:
        # before a destination is chosen, nulls describe the download-only draft
        use_default_naming = all(
            plan.get(key) is None for key in DEFAULT_TRANSFER_NAMING
        )
        naming_hint = (
            (" · 按标题建目录" if chinese else " · title folder")
            if use_default_naming
            else ""
        )
        hint = (
            (
                "自填：下载路径 | 库名或 #编号"
                if chinese
                else "Custom: directory | library name or #ID"
            )
            if combined
            else ("可自填库名或 #编号" if chinese else "Or enter a library name or #ID")
        )
        prefix = directory + " · " if directory_known or combined else ""
        questions.append(
            {
                "field": "destination" if combined else "library",
                "title": ("文件放在哪里？" if chinese else "Where should the files go?")
                if combined
                else ("转存到哪个媒体库？" if chinese else "Which transfer library?"),
                "method": method,
                **(
                    {"naming": DEFAULT_TRANSFER_NAMING.copy()}
                    if use_default_naming
                    else {}
                ),
                "options": [
                    {"label": only, "detail": f"{prefix}{hint}", "value": None},
                    *[
                        {
                            "label": f"{lib['name']} · #{lib['id']}",
                            "detail": f"{prefix}{method_name} → {lib['dir']}"
                            + naming_hint,
                            "value": lib["id"],
                        }
                        for lib in available
                    ],
                ],
            }
        )
    return questions


async def resolve_destination(
    q: dict, answer: dict, plan: dict, user: UserInfo
) -> dict | None:
    """Resolve a displayed choice or custom input to persistent destination fields.

    Args:
        q: Server-issued destination question with immutable option values.
        answer: Structurally validated answer for this question.
        plan: Original validated settings.
        user: Account submitting the answer.

    Returns:
        Exact plan changes, or `None` for an invalid or ambiguous destination.
    """
    field = q["field"]
    custom = answer["custom"].strip()
    if "\x00" in custom:
        return None
    chosen = next((o for o in q["options"] if o["id"] in answer["selected"]), {})
    changes = {}
    if field == "directory":
        path = custom or chosen["value"]
        if custom and not (
            PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()
        ):
            return None
        return {"dir": path}
    library_id = chosen.get("value")
    if custom and field == "destination":
        path, separator, target = custom.partition("|")
        path = path.strip()
        if PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute():
            changes["dir"] = path
            custom = target.strip()
            if separator and not custom:
                return None
        elif separator:
            return None
    if custom:
        if custom.casefold() in {"仅下载", "不入库", "仅下载，不入库", "download only"}:
            library_id = None
        else:
            query = Q(name__iexact=custom) | Q(dir=custom)
            if re.fullmatch(r"#?[1-9][0-9]{0,17}", custom):
                query = Q(id=int(custom.lstrip("#")))
            matches = (
                await libraries(user)
                .filter(query)
                .limit(2)
                .values_list("id", flat=True)
            )
            if len(matches) != 1:
                return None
            library_id = matches[0]
    if library_id and not await libraries(user).filter(id=library_id).exists():
        return None
    changes.update(
        transfer_lib_id=library_id, transfer_method=q["method"] if library_id else None
    )
    if library_id != plan.get("transfer_lib_id"):
        changes.update(
            (q.get("naming") if library_id else None)
            or {"sub_pattern": None, "sub_repl": None}
        )
    return changes


def bind_questions(card: dict, specs: list[dict], review: dict) -> None:
    """Attach trusted plan values to the IDs issued by the generic card builder.

    Args:
        card: Newly generated generic question card.
        specs: Questions actually sent to the builder with their domain bindings.
        review: Frozen validated subscription settings.
    """
    card["subscription"] = review
    for q, spec in zip(card["questions"], specs, strict=True):
        q.update(title=spec["title"], field=spec["field"])
        for key in ("method", "naming"):
            if key in spec:
                q[key] = spec[key]
        for option, source in zip(q["options"], spec["options"], strict=True):
            option.update(
                label=source["label"], detail=source["detail"], value=source["value"]
            )
            if spec["field"] in {"library", "destination"} and source["value"]:
                card["resources"].append(media_resource(source["value"]))
    card["version"] = json_digest(card["questions"])[:24]
