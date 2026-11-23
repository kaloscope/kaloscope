"""Submit confirmed subscription answers collected through assistant question cards."""

from uuid import uuid4

from app.models.assistant import AssistantToolCall, AssistantTurn, media_resource
from app.models.assistant_questions import QuestionAnswer
from app.models.download import DownloadPlanCheck, DownloadPlanCreate
from app.models.user import UserInfo
from app.services import download
from app.services.assistant_sessions import resource_order
from app.services.download import DownloadPlanService
from app.services.subscription_destinations import resolve_destination
from app.services.subscription_intent import (
    confirmed_submission,
    confirmed_subscription_plan,
)


async def is_superseded_subscription_card(turn_id: int, record_id: int) -> bool:
    """Check whether a later question or validation in the turn replaced this card.

    Args:
        turn_id: Originating assistant turn identifier.
        record_id: Primary key of the `ask_user` tool call being answered.

    Returns:
        Whether a newer completed subscription interaction exists in the turn.
    """
    return await AssistantToolCall.filter(
        turn_id=turn_id,
        id__gt=record_id,
        name__in=["ask_user", "validate_download_plan"],
        status="complete",
    ).exists()


async def validate_subscription_answer(
    question: dict,
    answer: QuestionAnswer,
    subscription: dict,
    user: UserInfo,
) -> tuple[dict, str | None]:
    """Validate and resolve a non-skipped answer to a subscription question.

    Args:
        question: Server-issued subscription question metadata.
        answer: Validated user answer for `question`.
        subscription: Frozen subscription payload stored on the card.
        user: Signed-in administrator submitting the answers.

    Returns:
        Resolved destination patch fields and any localized validation issue key.
    """
    field = question.get("field")
    if field in {"directory", "library", "destination"}:
        patch = await resolve_destination(
            question, answer.model_dump(), subscription["plan"], user
        )
        if patch is None:
            return {}, field
        return patch, None
    if (
        field == "settings"
        and answer.custom
        and answer.custom.strip() != question["options"][0]["label"]
    ):
        return {}, "invalid"
    return {}, None


async def submit_subscription(card: dict, turn: AssistantTurn, user: UserInfo) -> dict:
    """Apply an answered subscription and record its final local parameter check.

    Preserves compatibility with legacy single-question release cards whose
    release question omits an explicit `field` key.

    Args:
        card: Server-issued card with all required choices resolved.
        turn: Locked question turn in the owner's conversation.
        user: Signed-in administrator submitting the answers.

    Returns:
        The domain outcome or the field that needs a corrected answer.
    """
    selected = await confirmed_subscription_plan(card, user)
    if not selected:
        release = next(
            (
                q["id"]
                for q in card["questions"]
                if q.get("field", "release") == "release"
            ),
            None,
        )
        return {"issue": "invalid", **({"id": release} if release else {})}
    check = DownloadPlanCheck(
        plan=selected["plan"], ingest_workflow_id=selected["ingest_workflow_id"]
    )
    preview = await DownloadPlanService.validate_creation(
        check, user, check_duplicate=False
    )
    consent = confirmed_submission(card, selected, preview)
    preview["assistant_submission"] = consent
    review_id = uuid4().hex
    await AssistantToolCall.create(
        turn_id=turn.id,
        name="validate_download_plan",
        call_id=review_id,
        arguments=check.model_dump_json(),
        offset=len(turn.content or ""),
        status="complete",
        result={
            "id": review_id,
            "name": "validate_download_plan",
            "ok": True,
            "data": preview,
        },
    )
    if not preview["valid"] or consent["status"] != "ready":
        return {
            "created": False,
            "valid": False,
            "issues": preview["issues"],
            "review_id": review_id,
            "request_id": preview["request_id"],
        }
    binding = preview.get("ingestion")
    result = await DownloadPlanService.create_once(
        DownloadPlanCreate(
            request_id=preview["request_id"],
            plan=check.plan,
            ingestion={key: binding[key] for key in ("workflow_id", "revision")}
            if binding
            else None,
            confirmed=True,
        ),
        user,
        active_review_call_id=review_id,
        notify=False,
    )
    return {
        **{
            key: result[key]
            for key in (
                "created",
                "replayed",
                "plan_id",
                "duplicate_plan_id",
                "already_exists",
                "outcome",
                "valid",
                "issues",
            )
            if key in result
        },
        "request_id": preview["request_id"],
        "review_id": review_id,
    }


async def finalize_subscription_card(
    card: dict,
    destination: dict,
    turn: AssistantTurn,
    user: UserInfo,
    *,
    pending: bool,
) -> tuple[dict | None, dict | None]:
    """Attach resolved destinations, commit ready subscriptions, and record provenance.

    Args:
        card: Mutable answered card payload being saved in the transaction.
        destination: Destination fields resolved from the submitted answers.
        turn: Locked originating turn whose resource provenance is updated.
        user: Signed-in administrator submitting the answers.
        pending: Whether a required question was skipped, deferring creation.

    Returns:
        Tuple of `(submission, issue)` where `issue` is populated when the card
        must reject the submission without persisting answers.
    """
    if destination:
        card["subscription"] = {**card["subscription"], "destination": destination}
    submission = None
    if not pending:
        submission = await submit_subscription(card, turn, user)
        if "issue" in submission:
            return None, submission
        card["subscription"] = {**card["subscription"], "submission": submission}
    resources = {"admin:downloads", *card["resources"]}
    if lib_id := destination.get("transfer_lib_id"):
        resources.add(media_resource(lib_id))
    card["resources"] = sorted(resources, key=resource_order)
    await AssistantTurn.filter(id=turn.id).update(
        resource_ids=sorted(set(turn.resource_ids) | resources, key=resource_order)
    )
    return submission, None


def notify_subscription_created(submission: dict | None) -> None:
    """Wake download synchronization after committing a newly created subscription.

    Args:
        submission: Submission receipt returned inside the answer transaction.
    """
    if submission and submission.get("created") and not submission.get("replayed"):
        download.notify_download_changes()
