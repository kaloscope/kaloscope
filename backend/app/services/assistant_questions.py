"""Issue immutable assistant questions and validate their answers."""

import hashlib
from datetime import datetime

from tortoise.transactions import in_transaction

from app.core.exceptions import (
    BadRequestException,
    ForbiddenException,
    NotFoundException,
)
from app.models.assistant import AssistantToolCall, AssistantTurn, media_resource
from app.models.assistant_questions import AnswerGroup, AskUser, QuestionAnswer
from app.models.download import DownloadPlan
from app.models.user import UserInfo
from app.services.assistant_sessions import AssistantSessionService
from app.services.download import DownloadPlanService
from app.services.flow import FlowGraphService
from app.services.media import MediaLibService
from app.utils.json import dumps


async def build_card(body: AskUser, user: UserInfo) -> dict:
    """Build choices with stable identifiers and current authorized plan facts.

    Args:
        body: Requested questions and optional plan candidates.
        user: Current account.

    Returns:
        Immutable card with its revision and resource provenance.

    Raises:
        NotFoundException: If a requested plan no longer exists.
    """
    questions = []
    resources = []
    if body.plan_ids:
        options = []
        for plan_id in body.plan_ids:
            plan = await DownloadPlan.get_or_none(id=plan_id)
            if plan is None:
                raise NotFoundException
            graph = await FlowGraphService.get_accessible(plan.graph_id, user)
            resources.append(graph.id)
            if plan.transfer_lib_id:
                await MediaLibService.get_accessible(plan.transfer_lib_id, user)
                resources.append(media_resource(plan.transfer_lib_id))
            options.append(
                {
                    "id": str(plan.id),
                    "label": f"#{plan.id} · {plan.keyword}",
                    "detail": graph.name,
                    "revision": DownloadPlanService.revision(plan),
                }
            )
        questions.append(
            {
                "id": "plan",
                "title": "",
                "options": options,
                "multiple": False,
                "required": True,
                "format": "plan",
            }
        )

    for i, q in enumerate(body.questions):
        questions.append(
            {
                **q.model_dump(exclude={"options"}),
                "id": f"q{i + 1}",
                "options": [
                    {"id": f"o{j + 1}", **option.model_dump()}
                    for j, option in enumerate(q.options)
                ],
            }
        )

    return {
        "questions": questions,
        "version": hashlib.sha256(dumps(questions)).hexdigest()[:24],
        "state": "pending",
        "resources": resources,
    }


def validate_answer(q: dict, answer: QuestionAnswer) -> tuple[list[str], str | None]:
    """Validate a selected or custom answer against the displayed question.

    Args:
        q: Immutable server-issued question.
        answer: Submitted answer.

    Returns:
        Readable answer values and any localized validation issue key.
    """
    values = {o["id"]: o for o in q["options"]}
    selected = answer.selected.copy()
    custom = answer.custom.strip()
    if answer.skipped:
        return ([], "invalid") if selected or custom else ([], None)
    if len(set(selected)) != len(selected) or any(v not in values for v in selected):
        return [], "invalid"
    if not q["multiple"] and len(selected) + bool(custom) > 1:
        return [], "invalid"
    if q["format"] == "plan" and custom:
        matches = [
            k
            for k, v in values.items()
            if custom in (k, f"#{k}", v["label"], v["label"].partition(" · ")[2])
        ]
        if len(matches) != 1:
            return [], "plan"
        selected = matches
        custom = ""
    if q["format"] == "datetime" and custom:
        try:
            value = datetime.fromisoformat(custom)
        except ValueError:
            return [], "datetime"
        if value.tzinfo is None:
            return [], "datetime"
    parts = [values[k]["label"] for k in selected] + ([custom] if custom else [])
    return (parts, None) if parts else ([], "empty")


async def answer_card(
    conversation_id: int, call_id: str, body: AnswerGroup, user: UserInfo
) -> dict:
    """Record the owner's answers once and return field errors without running tools.

    Args:
        conversation_id: Conversation that issued the questions.
        call_id: Server-generated interaction identity.
        body: Answer values and displayed card version.
        user: Account submitting the answers.

    Returns:
        Card outcome with the normalized selection summary or a located issue.

    Raises:
        NotFoundException: If the owned question no longer exists.
        ForbiddenException: If access to its resources has been revoked.
    """
    async with in_transaction():
        record, turn = await AssistantSessionService.locked_tool(
            conversation_id, call_id, "ask_user", user
        )
        data = record.result["data"]
        if body.version != data["version"]:
            return {"issue": "expired"}
        if data["state"] == "answered":
            if [item.model_dump() for item in body.items] != data["answers"]:
                return {"issue": "expired"}
            return {"card": data}
        latest = (
            await AssistantTurn.filter(conversation_id=conversation_id)
            .order_by("-id")
            .first()
        )
        if latest.id != turn.id or turn.status != "complete":
            return {"issue": "expired"}
        answers = {item.id: item for item in body.items}
        if len(answers) != len(body.items) or set(answers) != {
            q["id"] for q in data["questions"]
        }:
            return {"issue": "invalid"}
        pending = False
        summary = []
        for q in data["questions"]:
            answer = answers[q["id"]]
            parts, issue = validate_answer(q, answer)
            if issue:
                return {"issue": issue, "id": q["id"]}
            if q["format"] == "plan" and parts:
                target = next(o for o in q["options"] if o["label"] == parts[0])
                plan = await DownloadPlan.get_or_none(id=int(target["id"]))
                if not plan or DownloadPlanService.revision(plan) != target["revision"]:
                    return {"issue": "expired", "id": q["id"]}
            pending |= answer.skipped and q["required"]
            summary.append(
                {
                    "id": q["id"],
                    "title": q["title"],
                    "values": parts,
                    "skipped": answer.skipped,
                }
            )
        card = {
            **data,
            "state": "deferred" if pending else "answered",
            "summary": summary,
            "answers": [item.model_dump() for item in body.items],
        }
        await AssistantToolCall.filter(id=record.id).update(
            result={**record.result, "data": card}
        )
        return {"card": card}


async def continuation_prompt(
    conversation_id: int, call_id: str, user: UserInfo
) -> tuple[str, list]:
    """Resolve a durable card answer for a single continuation turn.

    Args:
        conversation_id: Owned conversation receiving the continuation.
        call_id: Previously answered question identity.
        user: Account starting the continuation.

    Returns:
        Selection facts and resource provenance.

    Raises:
        BadRequestException: If the card is stale or unresolved.
        ForbiddenException: If resource access has changed.
    """
    latest = (
        await AssistantTurn.filter(conversation_id=conversation_id)
        .order_by("-id")
        .first()
    )
    record = await AssistantToolCall.get_or_none(
        call_id=call_id,
        name="ask_user",
        status="complete",
        turn_id=latest.id if latest else 0,
    )
    result = record.result if record and isinstance(record.result, dict) else {}
    card = result.get("data")
    if (
        not latest
        or latest.status != "complete"
        or result.get("ok") is not True
        or not isinstance(card, dict)
        or card.get("state") != "answered"
        or not isinstance(card.get("summary"), list)
    ):
        raise BadRequestException
    if not await AssistantSessionService.allowed(latest.resource_ids, user):
        raise ForbiddenException
    return dumps(card["summary"]).decode(), latest.resource_ids
