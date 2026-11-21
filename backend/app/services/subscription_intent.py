"""Tie assistant subscription execution to the user's order and actual search facts."""

import re
import unicodedata
from dataclasses import dataclass
from uuid import UUID

from tortoise import timezone
from tortoise.expressions import F, Q

from app.core.flow.nodes.base import start_config
from app.models.assistant import AssistantToolCall, AssistantTurn
from app.models.download import DownloadPlan
from app.models.user import UserInfo, UserRole
from app.services.flow import FlowGraphService
from app.services.subscription_choices import (
    compact,
    confirmation_question,
    contains_title,
    release_choices,
    release_mismatch,
)
from app.services.subscription_destinations import (
    destination_questions,
    libraries,
    resolve_destination,
)

_CREATE = re.compile(
    r"^(?:请\s*)?(?:(?:帮我|给我|我要|我想要?|麻烦你?)\s*)?"
    r"(?:订阅|追更|追番|(?:新建|创建|添加|建立)(?:一[个条份])?(?:下载|订阅)计划)"
    r"|^(?:please\s+)?(?:subscribe(?:\s+me)?\s+to|(?:create|add|set up)\s+"
    r"(?:a\s+)?(?:download plan|subscription)(?:\s+for)?\b)",
    re.I,
)
_TITLE_FIRST = re.compile(r"^[《「“]([^》」”]+)[》」”](.*)$", re.S)
_AUTO_DOWNLOAD = re.compile(r"(?:有更新|更新后|每次更新).{0,10}自动.{0,6}(?:下|追)")
_REVISION = re.compile(
    r"改主意|按新条件|(?:改|换)(?:为|成)|(?:只要|仅限)|(?:change|instead|switch to)\b",
    re.I,
)
_HOLD = re.compile(
    r"[?？]|吗[呀呢。！\s]*$|(?:咨询|了解|请问|能否|是否|可否|能不能|行不行|怎么|怎样|如何|为什么|是什么|有没有|多少[空个]|"
    r"假如|假设|如果|除非|先让我审核|给我确认一下|重建|替换|删除|取消|先别|暂不|暂缓|先等等|(?:仅|只|先)(?:预览|准备|看一下|看看|核对)|只想看|暂时不|"
    r"(?:不要|别|不必|先不|还不|不想|不)(?:立即|直接|马上|立刻|自动|实际)?(?:建|创建|执行|订阅|提交)|"
    r"(?:确认|批准|同意)(?:后|以后|再|之后)|等我(?:点|确认|同意|批准)|我(?:再|来)(?:点|确认|决定))"
    r"|\b(?:can i|can you explain|how (?:to|do|would)|preview|"
    r"do not(?: yet)? (?:create|subscribe)|don't (?:create|subscribe)|"
    r"before (?:creating|subscribing)|(?:after|until) (?:i|my|approval|confirmation)|"
    r"delete|rebuild|replace|cancel|stop)\b",
    re.I,
)
_ACCEPT = re.compile(
    r"(?:仍然)?确认(?:创建|订阅|执行|提交)?(?:这个计划)?|同意(?:创建|订阅)?|按(?:照)?(?:原|上述|以上|这个|默认)(?:设置|方案)?(?:创建|执行|订阅)?|"
    r"(?:i )?confirm(?: the (?:displayed )?(?:plan|subscription)(?: and defaults)?)?"
    r"(?:[.] create it)?|go ahead|create(?: it)?|use my choices",
    re.I,
)
_PREFERENCE = re.compile(
    r"^(?:选择.{1,24}(?:分类|字幕组|下载器)|只要|仅限|只收(?:录)?|只下载|(?:字幕组|发布组|目录|下载器|关键词|总上限|总计|每次上限)\s*(?:选|用|是|为|设为|[:：=])|"
    r"用(?:默认|推荐)(?:选项|设置)?|每\s*\d+\s*(?:小时|分钟)|"
    r"use the (?:default|recommended) (?:options|settings))",
    re.I,
)
_PREFERENCE_TITLE = re.compile(
    r"字幕|发布|制作组|版本|画质|分辨率|清晰度|关键[词字]|分类|限制|上限|数量|下载|间隔|频率|目录|整理|入库|索引|工作流|筛选|订阅|"
    r"subtitle|release|group|uploader|filter|indexer|schedule|interval|download|directory|limit|cap|quality|resolution",
    re.I,
)
_TERM = re.compile(
    r"(?:只要|仅限|只收(?:录)?|只(?:下载|订阅)|(?:发布组|字幕组|制作组)\s*[:：=为是]\s*)([^，,。；;、\n]{1,80})"
)
_TERM_TITLE = re.compile(
    r"字幕组|发布组|制作组|画质|清晰度|分辨率|release group|uploader|"
    r"quality|resolution",
    re.I,
)
_LIMIT = re.compile(
    r"(?:总(?:数|计|量|共)?\s*(?:上限|限制|最多)?|(?:下载|资源)上限|total(?:\s+(?:limit|cap))?)"
    r"\s*(?:of|[:：=为是])?\s*(\d+)",
    re.I,
)
_LIMIT_TITLE = re.compile(r"总数|总计|总量|总上限|总.*上限|total limit|total cap", re.I)
_GROUP_FILTER = re.compile(
    r"uploader|release.?group|sub.?group|字幕组|发布组|^(group|team)$", re.I
)
_UNRESTRICTED = re.compile(
    r"(?:不限|不限制|任意|全部|无偏好|不指定|都可以)(?:字幕组|发布组|版本|字幕组或版本)?"
    r"|(?:字幕组|发布组|版本)(?:不限|不限制)|any(?: release group(?: or edition)?)?"
    r"|no preference",
    re.I,
)


def explicit_subscription(prompt: str) -> bool:
    """Recognize a direct creation request that the user did not defer.

    Args:
        prompt: Original user text, never a model summary.

    Returns:
        Whether its imperative can authorize subscription creation.
    """
    text = prompt.strip()
    match = _CREATE.match(text)
    if not match:
        quoted = _TITLE_FIRST.match(text)
        return bool(
            quoted and _AUTO_DOWNLOAD.search(quoted[2]) and not _HOLD.search(quoted[2])
        )
    # punctuation and words inside the requested title do not alter the order
    rest = re.sub(
        r'^[\s]*[「《“"\']([^」》”"\']+)[」》”"\']', "", text[match.end() :], count=1
    )
    return not _HOLD.search(rest)


def deferred_subscription(prompt: str) -> bool:
    """Recognize subscription proposals whose writer must wait for manual approval.

    Args:
        prompt: Original user message.

    Returns:
        Whether to stop at the requested preview once it has validated.
    """
    return bool(_CREATE.match(prompt.strip())) and not explicit_subscription(prompt)


def accepts_offer(prompt: str, *, preferences: bool = False) -> bool:
    """Recognize explicit acceptance or an invited subscription preference.

    Args:
        prompt: User message immediately following the prior offer.
        preferences: Whether an earlier explicit order already established intent.

    Returns:
        Whether the reply can advance the offered subscription.
    """
    value = prompt.strip().rstrip("。.!！")
    return not _HOLD.search(value) and bool(
        _ACCEPT.fullmatch(value) or (preferences and _PREFERENCE.match(value))
    )


@dataclass
class SubscriptionIntent:
    """Carry the originating order and the choices supplied for this order."""

    prompt: str
    turns: list[int]
    choices: list[tuple[str, str]]
    answered: bool
    confirmation: dict | None = None
    revision: dict | None = None


async def superseded_retry(current: AssistantTurn, ancestor: int) -> bool:
    """Detect another user request between this retry and its original turn.

    Args:
        current: Server-owned retry attempt.
        ancestor: ID of the earlier logical turn.

    Returns:
        Whether the retry would revive an order after an intervening message.
    """
    return (
        current.id != ancestor
        and await AssistantTurn.filter(
            Q(retry_of=None) | ~Q(retry_of=ancestor),
            conversation_id=current.conversation_id,
            id__gt=ancestor,
            id__lt=current.id,
        ).exists()
    )


async def has_release_question(turn_id: int) -> bool:
    """Check whether the turn requires an answer tied to its release-choice card.

    Args:
        turn_id: Originating turn in the current conversation.

    Returns:
        Whether a successful question carries a server-issued subscription review.
    """
    results = await AssistantToolCall.filter(
        turn_id=turn_id, name="ask_user", status="complete"
    ).values_list("result", flat=True)
    return any(
        (r or {}).get("ok") and (r.get("data") or {}).get("subscription")
        for r in results
    )


async def subscription_intent(turn: AssistantTurn) -> SubscriptionIntent | None:
    """Resolve an uninterrupted subscription order across clarification and retries.

    Args:
        turn: User turn whose validated plan is being considered.

    Returns:
        User authorization and preferences, or no authority to create a plan.
    """
    current = turn
    ids = [turn.id]
    choices = []
    answered = False
    confirmation = None
    revision = None
    while True:
        if current.retry_of:
            previous = await AssistantTurn.get_or_none(
                id=current.retry_of, conversation_id=turn.conversation_id
            )
            if (
                previous is None
                or previous.id in ids
                or await superseded_retry(current, previous.id)
            ):
                return None
        elif not current.request_id.startswith("answer-") and explicit_subscription(
            current.prompt
        ):
            return SubscriptionIntent(
                current.prompt, ids, choices, answered, confirmation, revision
            )
        else:
            is_card = current.request_id.startswith("answer-")
            revises = (
                not is_card
                and not _HOLD.search(current.prompt)
                and _REVISION.search(current.prompt)
            )
            if (
                not is_card
                and not revises
                and (
                    (answered and confirmation is None)
                    or not accepts_offer(current.prompt, preferences=True)
                )
            ):
                return None
            previous = (
                await AssistantTurn.filter(
                    conversation_id=turn.conversation_id, id__lt=current.id
                )
                .order_by("-id")
                .first()
            )
            if not previous or previous.status != "complete":
                return None
            if is_card:
                card = await AssistantToolCall.get_or_none(
                    turn_id=previous.id,
                    call_id=current.request_id.removeprefix("answer-"),
                    name="ask_user",
                    status="complete",
                )
                result = card.result if card else None
                data = (result or {}).get("data") or {}
                confirmed_card = data.get("subscription") is not None
                if (
                    not (result or {}).get("ok")
                    or data.get("state") != "answered"
                    or any(q.get("format") == "plan" for q in data.get("questions", []))
                ):
                    return None
                values = [
                    (q["title"], v)
                    for q in data.get("summary", [])
                    if not q["skipped"]
                    for v in q["values"]
                ]
                check_values = [v for _, v in values]
                if confirmed_card:
                    release_ids = {
                        q["id"]
                        for q in data["questions"]
                        if q.get("field", "release") == "release"
                    }
                    check_values = [
                        a["custom"]
                        for a in data["answers"]
                        if a["id"] in release_ids and a["custom"]
                    ]
                if (
                    not values
                    or all(
                        v.strip().casefold()
                        in {"是", "好", "好的", "继续", "确认", "yes", "ok"}
                        for _, v in values
                    )
                    or any(
                        _HOLD.search(v) or v.strip().casefold() in {"不", "否", "no"}
                        for v in check_values
                    )
                    or not confirmed_card
                    and not any(
                        _PREFERENCE_TITLE.search(q) or _PREFERENCE.match(v)
                        for q, v in values
                    )
                ):
                    return None
                if confirmed_card and confirmation is None:
                    confirmation = data
                choices.extend(values)
            else:
                if await has_release_question(previous.id):
                    if not revises:
                        return None
                    offer = (
                        await AssistantToolCall.filter(
                            turn_id=previous.id, name="ask_user", status="complete"
                        )
                        .order_by("-id")
                        .first()
                    )
                    card = (offer.result or {}).get("data") or {}
                    if card.get("state") not in {"pending", "deferred"} or not card.get(
                        "subscription"
                    ):
                        return None
                    revision = revision or card["subscription"]
                elif not accepts_offer(current.prompt, preferences=True):
                    return None
                choices.append(("", current.prompt))
            answered = True
        ids.append(previous.id)
        current = previous


async def revised_subscription_plan(
    intent: SubscriptionIntent, prompt: str, user: UserInfo
) -> dict | None:
    """Reopen a pending review when every correction identifies a supported field.

    Args:
        intent: Uninterrupted order carrying the previous server-issued review.
        prompt: Latest user instructions.
        user: Account resolving downloader and library destinations.

    Returns:
        A draft for fresh search and confirmation, or `None` for open-ended changes.
    """
    if not intent.revision or intent.confirmation:
        return None
    review = intent.revision
    plan = {**review["plan"], "keyword": review["title"]}
    changed = False
    workflow_id = review.get("ingest_workflow_id")
    for text in re.split(r"[，,。；;！!\n]", prompt):
        clause = text.strip()
        if not clause or re.fullmatch(
            r"(?:我)?改主意了|(?:请)?按新条件(?:帮我)?(?:订好|订阅|创建)", clause
        ):
            continue
        if _TERM.fullmatch(clause):
            # the renewed release choice will be bound to actual search metadata
            plan["filters"] = {
                k: v
                for k, v in (plan.get("filters") or {}).items()
                if not _GROUP_FILTER.search(k)
            }
        elif directory := re.fullmatch(
            r"(?:下载到|保存到|存到|download to)\s*(.+)", clause, re.I
        ):
            patch = await resolve_destination(
                {"field": "directory", "options": []},
                {"custom": directory[1].strip(), "selected": []},
                plan,
                user,
            )
            if not patch:
                return None
            plan.update(patch)
        elif re.fullmatch(
            r"(?:不用|不必|无需|不)(?:入库|转存)|仅下载|download only", clause, re.I
        ):
            plan.update(
                transfer_lib_id=None,
                transfer_method=None,
                sub_pattern=None,
                sub_repl=None,
            )
            workflow_id = None
        else:
            return None
        changed = True
    return {"plan": plan, "ingest_workflow_id": workflow_id} if changed else None


def requested_title(prompt: str) -> str:
    """Read the title immediately named by an explicit subscription order.

    Args:
        prompt: User's original imperative.

    Returns:
        The requested title or an empty string for a context-dependent target.
    """
    if quoted_first := _TITLE_FIRST.match(prompt.strip()):
        return quoted_first[1].strip()
    rest = _CREATE.sub("", prompt.strip(), count=1).strip()
    quoted = re.match(r'^[「《“"\']([^」》”"\']+)[」》”"\']', rest)
    if quoted:
        return quoted.group(1).strip()
    return re.split(
        r"[，,。；;！!\n]|[.]\s|\s+(?=只要|仅限|每隔?|每次|使用|总计|用.*下载器|"
        r"(?:with|using|every)\s)|(?:只要|仅限)",
        rest,
        maxsplit=1,
        flags=re.I,
    )[0].strip(" .。")


def required_preferences(intent: SubscriptionIntent) -> tuple[set[str], int | None]:
    """Collect concrete search restrictions and the latest user-specified total cap.

    Args:
        intent: Verified order and its newest-first clarification responses.

    Returns:
        Selected release or quality terms and total resource count.
    """
    terms: set[str] = set()
    cap = None
    for title, value in [("", intent.prompt), *reversed(intent.choices)]:
        number = _LIMIT.search(value)
        if number is None and _LIMIT_TITLE.search(title):
            number = re.search(r"(\d+)", value)
        if number:
            cap = int(number.group(1))
        match = _TERM.search(value)
        if match or _TERM_TITLE.search(title):
            if intent.revision and not title and value != intent.prompt:
                terms.clear()
            selected = match.group(1) if match else value
            term = re.sub(
                r"(?:发布|制作|翻译|压制|上传)的(?:资源|版本|作品|内容|字幕)$|的(?:资源|版本)$|[（(]推荐[）)]$",
                "",
                selected.strip(),
            ).strip(" 「」《》。.")
            if term and not _UNRESTRICTED.fullmatch(term):
                terms.add(term)
    return terms, cap


def requested_transfer(intent: SubscriptionIntent, library: str) -> bool:
    """Match the user's transfer destination to the library in the domain preview.

    Args:
        intent: Original order and the actual submitted clarification values.
        library: Library name from the validated transfer preview, when available.

    Returns:
        Whether the user explicitly selected this exact destination.
    """
    if not library:
        return False
    action = r"转存|入库|整理到|transfer|ingest|(?:copy|link|add) to"
    name = re.escape(unicodedata.normalize("NFKC", library).strip())
    target = (
        r"(?:到|至|to|into)?\s*(?:媒体库|the library|library)?\s*"
        rf"""[「《“"']?{name}[」》”"']?\s*(?:媒体库|库|library)?"""
    )
    for title, value in [*intent.choices, ("", intent.prompt)]:
        clauses = [
            clause[match.end() :].strip()
            for clause in re.split(r"[，,。；;！!\n]", value)
            if (match := re.search(action, clause, re.I))
        ]
        if not clauses:
            clauses = [
                m[1]
                for clause in re.split(r"[，,。；;！!\n]", value)
                if (
                    m := re.fullmatch(
                        r"\s*(?:媒体库|目标库|library)\s*"
                        r"(?:选用|选|用|为|是|[:：=])\s*(.+)",
                        clause,
                        re.I,
                    )
                )
            ]
        if not clauses:
            if not re.search(action, title, re.I):
                continue
            clauses = [value.strip()]
        if re.search(
            r"(?:无需|不用|不要|不必|不需要|别|不)(?:自动)?(?:转存|入库|整理)|"
            r"\b(?:no|without|skip|do not|don't)\s+(?:library|transfer|ingest|copy)",
            value,
            re.I,
        ):
            return False
        return all(
            re.fullmatch(target, unicodedata.normalize("NFKC", v), re.I)
            for v in clauses
        )
    return False


def _filter_option(filters: dict, term: str) -> tuple[str, str | list[str]] | None:
    """Find a declared filter value matching a release restriction.

    Args:
        filters: Published search filter definitions.
        term: Literal group or edition chosen by the user.

    Returns:
        The filter key and value, or `None` for a keyword restriction.
    """
    wanted = compact(term)
    return next(
        (
            (key, [value] if definition.get("type") == "checkbox" else value)
            for key, definition in filters.items()
            if isinstance(definition, dict)
            and isinstance(options := definition.get("options"), dict)
            for value, label in options.items()
            if wanted in {compact(str(value)), compact(str(label))}
        ),
        None,
    )


def _release_answer(
    question: dict, answer: dict, plan: dict
) -> tuple[list | None, list]:
    """Resolve the selected release, including older single-question cards.

    Args:
        question: Release choices issued from the search preview.
        answer: User selection or custom release restriction.
        plan: Settings to update if the user specified a resource cap.

    Returns:
        Required terms and observed release tags, or unresolved terms.
    """
    observed = []
    if answer["custom"].strip():
        value = answer["custom"].strip()
        if _HOLD.search(value) or value.casefold() in {"不", "否", "no", "取消"}:
            return None, []
        group = re.sub(
            _LIMIT.pattern + r"(?:\s*个(?:资源)?|\s+resources?)?", "", value, flags=re.I
        ).strip(" ，,；;、。.")
        terms, _ = required_preferences(
            SubscriptionIntent("", [], [("字幕组", group)], True)
        )
        restrictions = list(terms)
        for option in question["options"]:
            if option.get("value", option)["observed"] and any(
                compact(t) == compact(option["label"]) for t in restrictions
            ):
                restrictions = observed = option.get("value", option)["terms"]
                break
        if cap := _LIMIT.search(value):
            limit = int(cap[1])
            if (
                not 0
                < limit
                <= DownloadPlan._meta.fields_map["total_limit"].constraints["le"]
            ):
                return None, []
            plan["total_limit"] = limit
        if not restrictions and not _UNRESTRICTED.fullmatch(group):
            return None, []
    else:
        (selected,) = answer["selected"]
        option = next(o for o in question["options"] if o["id"] == selected)
        restrictions = option.get("value", option)["terms"]
        observed = restrictions if option.get("value", option)["observed"] else []
    return restrictions, observed


async def confirmed_subscription_plan(card: dict, user: UserInfo) -> dict | None:
    """Apply the answered questions to the exact settings shown in the card.

    Args:
        card: Server-owned answered question, including its frozen plan.
        user: Account whose indexer access must still be valid.

    Returns:
        The exact plan, keyword restrictions and ingestion workflow for revalidation.
        Returns `None` for an unresolved card or an unrelated question.
    """
    review = card.get("subscription")
    if not review or card.get("state") != "answered":
        return None
    answers = {answer["id"]: answer for answer in card["answers"]}
    if any(answer["skipped"] for answer in answers.values()):
        return None
    plan = {**review["plan"], **review.get("destination", {})}
    release = next(
        (q for q in card["questions"] if q.get("field", "release") == "release"), None
    )
    if release:
        restrictions, observed = _release_answer(release, answers[release["id"]], plan)
        if restrictions is None:
            return None
    else:
        restrictions = review["release"]["terms"]
        observed = restrictions if review["release"]["observed"] else []
    filters = dict(plan.get("filters") or {})
    graph = await FlowGraphService.get_accessible(plan["graph_id"], user)
    config = (await start_config(graph, "search"))["search"] or {}
    definitions = config.get("filters") or {}
    # a proposed group must not survive an explicit choice of all releases
    for key in tuple(filters):
        if _GROUP_FILTER.search(key):
            del filters[key]
    keywords = []
    for term in restrictions:
        matched = _filter_option(definitions, term)
        if matched:
            key, value = matched
            if isinstance(value, list):
                value = list(dict.fromkeys([*filters.get(key, []), *value]))
            filters[key] = value
        else:
            keywords.append(term)
    plan["filters"] = filters
    plan["keyword"] = " ".join([review["title"], *keywords])
    return {
        "plan": plan,
        "keywords": keywords,
        "observed": observed,
        "restrictions": restrictions,
        "ingest_workflow_id": review.get("ingest_workflow_id")
        if plan.get("transfer_lib_id") == review["plan"].get("transfer_lib_id")
        else None,
    }


def confirmed_submission(card: dict, selected: dict | None, validation: dict) -> dict:
    """Authorize the resolved card settings and freeze its background release rule.

    Args:
        card: Server-issued answered subscription card.
        selected: Settings resolved from its options and submitted custom values.
        validation: Local domain validation of the candidate plan.

    Returns:
        Creation readiness or the settings that still require approval.
    """
    plan = validation["plan"]
    if not selected:
        return {
            "status": "needs_preferences",
            "next": "Complete the required subscription choices.",
        }
    if {**plan, "filters": plan.get("filters") or {}} != selected["plan"] or (
        validation.get("ingestion") or {}
    ).get("workflow_id") != selected["ingest_workflow_id"]:
        return {
            "status": "needs_settings",
            **selected,
            "next": "Use the exact settings confirmed on the card.",
        }
    if (validation.get("ingestion") or {}).get("binding") == "add":
        return {
            "status": "needs_approval",
            "next": "Show the new library-wide binding for explicit approval.",
        }
    rule = {
        "title": card["subscription"]["title"],
        "observed": selected["observed"],
        "terms": selected["restrictions"],
        "confirmed": True,
    }
    return {
        "status": "ready",
        **({"release_rule": rule} if rule["terms"] else {}),
        "next": "Create with the confirmed settings.",
    }


async def submission_check(
    intent: SubscriptionIntent, validation: dict, user: UserInfo
) -> dict:
    """Check confirmed parameters or use recorded previews to prepare a question.

    Args:
        intent: Original order and the user's answered preferences.
        validation: Normalized editor plan returned by domain validation.
        user: Account authorized to read the published indexer's filter options.

    Returns:
        Readiness or the concrete missing settings for the next tool step.
    """
    plan = validation["plan"]
    if intent.confirmation:
        selected = await confirmed_subscription_plan(intent.confirmation, user)
        return confirmed_submission(intent.confirmation, selected, validation)
    if plan.get("transfer_lib_id"):
        library = await libraries(user).filter(id=plan["transfer_lib_id"]).first()
        if not library or not requested_transfer(intent, library.name):
            return {
                "status": "needs_approval",
                "next": "Remove the unrequested transfer or show it for approval.",
            }
    keyword = plan["keyword"]
    filters = plan.get("filters") or {}
    title = requested_title(intent.prompt)
    terms, cap = required_preferences(intent)
    if len(terms) > 1:
        return {
            "status": "needs_preferences",
            "next": (
                "Multiple release or quality selections need a combined filter. "
                "Show a review with every restriction before creating."
            ),
        }
    term = next(iter(terms), "")
    # a revised card starts from fresh title results so the new choice can be bound
    revise_release = intent.revision is not None
    if not term and any(_GROUP_FILTER.search(key) and filters[key] for key in filters):
        return {
            "status": "needs_settings",
            "next": "Remove the guessed group filter, then search and validate.",
        }
    if cap is not None and plan.get("total_limit") != cap:
        return {
            "status": "needs_settings",
            "total_limit": cap,
            "next": "Set this total_limit, then validate.",
        }
    expected = {compact(title)}
    keywords = []
    if term and not revise_release:
        graph = await FlowGraphService.get_accessible(plan["graph_id"], user)
        config = (await start_config(graph, "search"))["search"] or {}
        matched = _filter_option(config.get("filters") or {}, term)
        if matched:
            key, value = matched
            if filters.get(key) != value:
                return {
                    "status": "needs_settings",
                    "required_filter": {key: value},
                    "next": "Apply this filter. Search the exact inputs and validate.",
                }
        else:
            keywords = [term]
            expected = {compact(title + term), compact(term + title)}
    if not title or not compact(keyword) or compact(keyword) not in expected:
        return {
            "status": "needs_settings",
            "requested_title": title,
            "required_term": term,
            "next": "Search the exact title with the required term, then validate.",
        }
    searches = AssistantToolCall.filter(
        turn_id__in=intent.turns, name="search_indexer", status="complete"
    )
    rows = await searches.order_by("-id")
    for row in rows:
        result = row.result or {}
        data = result.get("data") or {}
        query = data.get("query") or {}
        if not (
            result.get("ok")
            and data.get("workflow_id") == plan["graph_id"]
            and not query.get("sources")
            and query.get("keyword") == keyword
            and (query.get("filters") or {}) == filters
        ):
            continue
        if query.get("page_num", 1) != 1 or (
            query.get("page_size", 5) < 5
            and (data.get("has_more") or data.get("omitted_count"))
        ):
            # a one-result probe can hide editions already present on the first page
            continue
        items = data.get("items") or []
        if data.get("status") != "ok" or not items:
            return {
                "status": "no_matches",
                "next": "No matches. Ask to revise or review a future-match plan.",
            }
        rule = {
            "title": title,
            "observed": [],
            "terms": keywords,
            "confirmed": False,
        }
        for item in items:
            if mismatch := release_mismatch(item, rule):
                return {
                    "status": "unverified_term",
                    "mismatch": mismatch,
                    "next": f"Check the {mismatch} before searching again.",
                }
        if (validation.get("ingestion") or {}).get("binding") == "add":
            return {
                "status": "needs_approval",
                "next": "Show the new library-wide binding for explicit approval.",
            }
        title_items = [
            item
            for item in items
            if contains_title(str(item.get("title") or ""), title)
        ]
        observed_releases = release_choices(title_items, title)
        releases = observed_releases
        if term:
            named = [
                r
                for r in releases
                if compact(r[0]) == compact(term)
                or compact("".join(r)) == compact(term.replace("的", ""))
            ]
            releases = named or (
                [list(dict.fromkeys([*r, term])) for r in releases]
                if releases
                else [[term]]
            )
        else:
            releases = [*releases, []]
        chinese = bool(re.search(r"[\u3400-\u9fff]", intent.prompt))
        release = confirmation_question(title, releases, title_items, chinese=chinese)
        for option, restriction in zip(release["options"], releases, strict=True):
            option["value"] = {
                "terms": restriction,
                "observed": restriction in observed_releases,
            }
        release["field"] = "release"
        prompt = " ".join([intent.prompt, *(v for _, v in intent.choices)])
        questions = await destination_questions(plan, prompt, user, chinese=chinese)
        fixed = release["options"][0]["value"] if term and len(releases) == 1 else None
        if fixed:
            for q in questions:
                q["title"] = f"{' · '.join(fixed['terms'])} · {q['title']}"
        if not questions:
            dest = plan["dir"] + (
                " → " + library.name + " · " + plan["transfer_method"]
                if plan.get("transfer_lib_id")
                else (" · 不入库" if chinese else " · download only")
            )
            if fixed:
                detail = " · ".join([*fixed["terms"], dest])
                questions.append(
                    {
                        "field": "settings",
                        "title": title,
                        "options": [
                            {
                                "label": "按这些设置订阅"
                                if chinese
                                else "Subscribe with these settings",
                                "detail": detail,
                                "value": True,
                            }
                        ],
                    }
                )
            else:
                release["title"] += " · " + dest
        if not fixed:
            questions.insert(0, release)
        existing = []
        if user.role == UserRole.ADMIN:
            existing = (
                await DownloadPlan.filter(
                    Q(keyword=title) | Q(keyword__istartswith=title + " "),
                    Q(interval_end=None) | Q(interval_end__gt=timezone.now()),
                    Q(total_limit=None) | Q(total_count__lt=F("total_limit")),
                    graph_id=plan["graph_id"],
                    downloader_id=plan["downloader_id"],
                )
                .order_by("id")
                .limit(5)
                .values_list("id", flat=True)
            )
        return {
            "status": "needs_confirmation",
            **({"related_plan_ids": existing} if existing else {}),
            "next": "Wait for the user to submit all subscription choices.",
            "questions": questions,
            "release": fixed,
            "title": title,
        }
    return {
        "status": "needs_search",
        "next": "Search these exact inputs, then validate and create.",
    }


async def prior_validations(call: AssistantToolCall) -> list[UUID]:
    """Collect older review identities for the same original subscription order.

    Args:
        call: Server-owned validation record with its originating turn loaded.

    Returns:
        Prior UUIDs that might already have been claimed in chat or the browser.
    """
    intent = await subscription_intent(call.turn)
    if intent is None:
        return []
    results = await AssistantToolCall.filter(
        turn_id__in=intent.turns,
        id__lt=call.id,
        name="validate_download_plan",
        status="complete",
    ).values_list("result", flat=True)
    return [
        UUID(data["request_id"])
        for result in results
        if (result or {}).get("ok")
        and (data := result.get("data"))
        and data.get("valid")
        and data.get("request_id")
    ]
