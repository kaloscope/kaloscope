from dataclasses import dataclass
from typing import TypedDict

from app.core.flow.fields.base import Field


class AIModel(TypedDict):
    """The type for the AI provider and model selection."""

    provider_id: int
    model_id: str


@dataclass(kw_only=True)
class AIModelField(Field[AIModel | None]):
    """A control for selecting an AI provider and model."""

    default: AIModel | None = None

    def _field_type(self) -> str:
        return "ai_model"
