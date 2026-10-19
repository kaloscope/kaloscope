"""Validate stateless assistant conversations and public stream events."""

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.ai import AIModelSelection

MAX_HISTORY_BYTES = 128 * 1024


class AssistantMessage(BaseModel):
    """Accept text history without client-supplied system or tool messages."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=32768)


class AssistantChat(BaseModel):
    """Request one assistant turn using an explicitly configured model."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    model: AIModelSelection
    messages: list[AssistantMessage] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_history(self) -> Self:
        """Require alternating, nonblank turns within the conversation byte limit.

        Returns:
            Validated conversation ending in the current user request.

        Raises:
            ValueError: If roles, text, or the total UTF-8 size are invalid.
        """
        for index, message in enumerate(self.messages):
            expected = "user" if index % 2 == 0 else "assistant"
            if message.role != expected or not message.content.strip():
                raise ValueError("Use alternating nonblank user and assistant turns")
        if self.messages[-1].role != "user":
            raise ValueError("End the conversation with a user request")
        if (
            sum(len(item.content.encode()) for item in self.messages)
            > MAX_HISTORY_BYTES
        ):
            raise ValueError("Conversation exceeds the context limit")
        return self


class AssistantEvent(BaseModel):
    """Carry a named SSE event without the ordinary API response envelope."""

    event: Literal["start", "text_delta", "tool_start", "tool_result", "done", "error"]
    data: dict[str, Any]
