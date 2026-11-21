"""Present release choices and the recorded outcome of assistant subscriptions."""

import re
import unicodedata
from typing import Literal

from app.core.renderer import t2s

_TAG = re.compile(r"^[\[【]([^\]】\r\n]{1,48})[\]】]")
_STAR_GROUP = re.compile(r"^([^★☆]{1,40}(?:字幕组|字幕組|字幕社))[★☆]")
_METADATA = re.compile(
    r"\d+|(?:s\d+)?ep?\d+|\d{3,4}[pi].*|\d{1,2}月新番|(?:web-?dl|b[dr]rip|bluray|hevc|avc|"
    r"x26[45]|uhd|flac|hi-res|gb|big5|简中|繁中|简繁|繁簡|mkv|mp4)",
    re.I,
)
_EDITION = re.compile(
    r"[\[【]([简簡繁]{1,2}(?:体|體|中|日)?(?:内嵌|內嵌|内封|內封|双语|雙語)|"
    r"(?:CHS|CHT)(?:[&+](?:CHS|CHT))?)[\]】]",
    re.I,
)
_RESOLUTION = re.compile(r"(?<!\w)(\d{3,4}[pi]|4K)(?!\w)", re.I)


def compact(value: str) -> str:
    """Normalize a title or option label for a literal comparison.

    Args:
        value: Search keyword, option text or user-provided title.

    Returns:
        Case-folded text with punctuation and spaces omitted.
    """
    return "".join(
        c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum()
    )


def contains_title(name: str, title: str) -> bool:
    """Match a requested media title across simplified and traditional Chinese.

    Args:
        name: Indexer text that may contain the title.
        title: Requested title in its original spelling.

    Returns:
        Whether the normalized text contains the nonempty normalized title.
    """
    key = compact(t2s(title))
    return bool(key) and key in compact(t2s(name))


def release_group(item: dict, title: str) -> str:
    """Read the credited group from release tags or the indexer's uploader field.

    Args:
        item: A search result with title and optional uploader.
        title: Requested media title, which may also appear in brackets.

    Returns:
        The observed release group, or an empty string when unknown.
    """
    name = str(item.get("title") or "").strip()
    tag = _TAG.match(name) or _STAR_GROUP.match(name)
    group = tag[1].strip() if tag else ""
    if (
        not group
        or _METADATA.fullmatch(group)
        or _EDITION.fullmatch(tag[0])
        or contains_title(group, title)
    ):
        group = str(item.get("uploader") or "").strip()
    return group if len(group) <= 48 and any(c.isalpha() for c in group) else ""


def release_matches(item: dict, title: str, terms: list[str]) -> bool:
    """Check a returned release against the exact group and edition shown to the user.

    Args:
        item: Result of the confirmed search.
        title: Requested media title.
        terms: Observed group, followed by any edition and resolution restrictions.

    Returns:
        Whether the credited group and each selected edition token match.
    """
    name = str(item.get("title") or "")
    group = release_group(item, title)
    labels = {
        e.casefold() for e in (*_EDITION.findall(name), *_RESOLUTION.findall(name))
    }
    return group.casefold() == terms[0].casefold() and all(
        term.casefold() in labels for term in terms[1:]
    )


def release_mismatch(item: dict, rule: dict) -> Literal["title", "release"] | None:
    """Identify the first confirmed restriction a search result fails.

    Args:
        item: Metadata returned alongside the candidate link.
        rule: Verified title, credited release tags and literal restrictions.

    Returns:
        The mismatched field, or `None` when all restrictions match.
    """
    if rule["confirmed"] and rule["terms"]:
        name = str(item.get("title") or "")
        if not contains_title(name, rule["title"]):
            return "title"
        if rule["observed"]:
            return (
                None
                if release_matches(item, rule["title"], rule["observed"])
                else "release"
            )
        # custom values must identify the credited group or an actual edition tag
        labels = {compact(release_group(item, rule["title"]))}
        labels.update(
            compact(tag)
            for tag in (*_EDITION.findall(name), *_RESOLUTION.findall(name))
        )
        matched = all(compact(term) in labels for term in rule["terms"])
    else:
        metadata = compact(
            " ".join(str(item.get(k) or "") for k in ("title", "uploader"))
        )
        matched = all(compact(term) in metadata for term in rule["terms"])
    return None if matched else "release"


def matches_release_rule(item: dict, rule: dict) -> bool:
    """Apply the stored release restrictions to a background download candidate.

    Args:
        item: Indexer metadata for a candidate download.
        rule: Release criteria stored in the plan receipt.

    Returns:
        Whether the result satisfies every restriction.
    """
    return release_mismatch(item, rule) is None


def release_choices(items: list[dict], title: str) -> list[list[str]]:
    """List observed release groups and distinguish editions within a group.

    Args:
        items: Metadata from matching indexer searches.
        title: Requested title, which may itself appear as a bracketed tag.

    Returns:
        Up to seven choices, with one representative per group before extra editions.
    """
    groups: dict[str, list[tuple[str, str]]] = {}
    names: dict[str, str] = {}
    for item in items:
        name = str(item.get("title") or "").strip()
        group = release_group(item, title)
        if not group:
            continue
        group = names.setdefault(group.casefold(), group)
        editions = _EDITION.findall(name)
        language = next(
            (e for e in editions if not e.isascii()), editions[0] if editions else ""
        )
        resolution = _RESOLUTION.search(name)
        groups.setdefault(group, []).append(
            (language, resolution[1] if resolution else "")
        )

    first, extra = [], []
    for group, rows in groups.items():
        languages = {lang for lang, _ in rows}
        sizes = {size for _, size in rows}
        variants = []
        for language, size in rows:
            terms = [group]
            if language and len(languages) > 1:
                terms.append(language)
            if size and len(sizes) > 1:
                terms.append(size)
            if terms not in variants:
                variants.append(terms)
        # a missing edition tag must not turn into an option covering known editions
        variants = [
            v for v in variants if not any(set(v) < set(other) for other in variants)
        ]
        first.append(variants[0])
        extra.extend(variants[1:])
    return (first + extra)[:7]


def confirmation_question(
    title: str, choices: list[list[str]], items: list[dict], *, chinese: bool
) -> dict:
    """Describe the single decision that authorizes the subscription.

    Args:
        title: Requested media title.
        choices: Literal search restrictions per option, empty for all releases.
        items: Actual search preview used to describe the matching releases.
        chinese: Whether the originating order was in Chinese.

    Returns:
        Required, single-select `ask_user` question in the user's language.
    """
    options = []
    for terms in choices:
        if terms:
            label = " · ".join(terms)
            count = sum(release_matches(item, title, terms) for item in items)
            if count:
                detail = (
                    f"预览命中 {count} 条" if chinese else f"{count} in this preview"
                )
            else:
                detail = (
                    "按指定条件重搜" if chinese else "Search again with these terms."
                )
        else:
            label = "不限字幕组或版本" if chinese else "Any release group or edition"
            detail = (
                "同一集的多个版本都可能下载"
                if chinese
                else "Different releases of the same episode may download."
            )
        options.append({"label": label, "detail": detail})
    return {
        "title": f"《{title[:120]}》的字幕组或版本"
        if chinese
        else f"Subscribe to {title[:120]} · Choose a release",
        "options": options,
        "required": True,
    }


def first_check_pending(first: dict) -> bool:
    """Check whether a first-attempt receipt can still acquire a final result.

    Args:
        first: Stored or permission-checked first execution receipt.

    Returns:
        Whether the assistant should read the receipt again.
    """
    return (
        first.get("status") in {"queued", "waiting", "scheduled"}
        or first.get("stage") == "checking"
    )


def first_check_summary(first: dict, *, chinese: bool) -> str:
    """Describe the confirmed first submission count or its recorded uncertainty.

    Args:
        first: The first check's own receipt, independent of later runs.
        chinese: Whether to write the result in Chinese.

    Returns:
        The factual follow-up sentence for the assistant reply.
    """
    added = first.get("added")
    stage = first.get("stage") or first.get("status")
    if stage == "unconfirmed" and added:
        return (
            f"首轮已记录 {added} 个下载任务，下载器实际接收数尚无法确认。"
            if chinese
            else (
                f"The first check recorded {added} local tasks. "
                "The downloader has not confirmed the count."
            )
        )
    if stage in {"unconfirmed", "unavailable"} or added is None:
        return (
            "首轮实际提交数尚无法确认。"
            if chinese
            else "The first submission count is unknown."
        )
    detail = {
        "no_search_results": ("没有搜到资源", "the search returned no results"),
        "no_new_tasks": (
            "匹配资源已在计划历史或下载任务中",
            "matching resources were already recorded",
        ),
        "release_filtered": (
            "没有符合发布条件的新资源",
            "no new resources met the release conditions",
        ),
        "driver_offline": ("无法连接下载器", "the downloader was unavailable"),
        "search_failed": ("索引器搜索失败", "the indexer search failed"),
        "invalid_link": (
            "资源缺少有效下载链接",
            "the results had no valid download links",
        ),
        "submit_failed": ("向下载器提交失败", "submission to the downloader failed"),
        "failed": ("检查发生错误", "the check encountered errors"),
        "partial": (
            f"另有 {first.get('errors', 0)} 项错误",
            f"{first.get('errors', 0)} errors were recorded",
        ),
    }.get(stage)
    if added == 0 and not detail:
        detail = ("记录中没有更具体的原因", "the receipt has no more specific reason")
    if chinese:
        return f"首轮已提交 {added} 个下载" + (f"，{detail[0]}。" if detail else "。")
    noun = "download" if added == 1 else "downloads"
    return f"The first check submitted {added} {noun}." + (
        f" {detail[1].capitalize()}." if detail else ""
    )


def confirmation_outcome(tool: str, result: dict, *, chinese: bool) -> str:
    """Report the final tool receipt after the user confirms a subscription.

    Args:
        tool: Last action performed with the confirmed settings.
        result: Persisted result of that action.
        chinese: Whether the order was placed in Chinese.

    Returns:
        A summary distinguishing an applied plan, an unresolved write and a
        stopped preview.
    """
    data = result.get("data") or {}
    if plan_id := data.get("duplicate_plan_id"):
        return (
            f"已有完全相同的下载计划 #{plan_id}，未重复创建。"
            if chinese
            else f"Identical plan #{plan_id} already exists. No new plan was created."
        )
    if tool == "create_download_plan":
        if result["ok"] and data.get("created"):
            plan_id = data["plan_id"]
            message = (
                f"已创建下载计划 #{plan_id}。"
                if chinese
                else f"Created download plan #{plan_id}."
            )
            if data.get("already_exists"):
                message = (
                    f"已存在相同的下载计划 #{plan_id}。"
                    if chinese
                    else f"Matching download plan #{plan_id} already exists."
                )
            if data.get("already_exists") or data.get("follow_first_check"):
                return message
            summary = first_check_summary(
                data.get("first_execution") or {}, chinese=chinese
            )
            return message + ("" if chinese else " ") + summary
        if data.get("valid") is False:
            return (
                "已确认的计划参数已失效，未创建下载计划。"
                if chinese
                else "The confirmed settings are no longer valid. No plan was created."
            )
        if data.get("outcome") == "cancelled":
            return (
                "此订阅已取消，未创建下载计划。"
                if chinese
                else "The subscription was cancelled. No plan was created."
            )
        return (
            "尚未收到创建成功的回执，请查看此操作的状态。"
            if chinese
            else "Creation has not been confirmed. Check the status of this action."
        )
    submission = data.get("assistant_submission") or {}
    check = submission.get("status")
    if check == "unverified_term" and submission.get("mismatch") == "title":
        reason = (
            "部分搜索结果的片名与订阅片名不一致。"
            if chinese
            else "At least one search result did not match the requested title."
        )
    elif check == "unverified_term":
        reason = (
            "搜索结果与所选字幕组或版本不一致。"
            if chinese
            else "The search results disagree with your release choice."
        )
    elif check == "no_matches":
        reason = (
            "没有搜到符合所选条件的资源。"
            if chinese
            else "No releases matched your selection."
        )
    else:
        reason = (
            "未能验证所选条件。"
            if chinese
            else "Your selected settings could not be verified."
        )
    return reason + (
        "未创建下载计划。" if chinese else " No download plan was created."
    )
