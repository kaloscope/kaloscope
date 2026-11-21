"""Define bounded question cards and the answers submitted by their owner."""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

type Text = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)
]
type Key = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


class QuestionModel(BaseModel):
    """Reject unexpected fields on question and answer messages."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class Choice(QuestionModel):
    """Describe one proposed answer and its effect."""

    label: Text
    detail: Text


class Question(QuestionModel):
    """Describe one question in the user's language."""

    title: Text
    options: list[Choice] = Field(min_length=1, max_length=8)
    multiple: bool = False
    required: bool = True
    format: Literal["text", "datetime"] = "text"


class AskUser(QuestionModel):
    """Request missing information before proceeding."""

    questions: list[Question] = Field(default_factory=list, max_length=3)
    plan_ids: list[Annotated[int, Field(gt=0)]] = Field(
        default_factory=list, max_length=8
    )

    @model_validator(mode="after")
    def check_size(self) -> Self:
        """Require one to three questions and distinct plan candidates.

        Returns:
            The validated group.

        Raises:
            ValueError: If the group is empty, too large or ambiguous.
        """
        if not 1 <= len(self.questions) + bool(self.plan_ids) <= 3 or (
            self.plan_ids
            and (
                len(self.plan_ids) < 2 or len(set(self.plan_ids)) != len(self.plan_ids)
            )
        ):
            raise ValueError(
                "Use 1–3 questions, with at least two distinct IDs for a plan choice."
            )
        return self


class QuestionAnswer(QuestionModel):
    """Describe the answer to a single server-issued question."""

    id: Key
    selected: list[Key] = Field(default_factory=list, max_length=8)
    custom: str = Field(default="", max_length=512)
    skipped: bool = False


class AnswerGroup(QuestionModel):
    """Submit a complete set of answers against an immutable card."""

    version: Key
    items: list[QuestionAnswer] = Field(min_length=1, max_length=3)
