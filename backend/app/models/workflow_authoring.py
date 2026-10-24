"""Bound shared HTTP probing and template validation arguments and results."""

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.utils.json import dumps


class AuthoringInput(BaseModel):
    """Accept explicit fixtures without loading workflow or account secrets."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    @model_validator(mode="after")
    def check_size(self) -> Self:
        """Bound the complete input, including nested fixture values.

        Returns:
            Validated input.

        Raises:
            ValueError: If the serialized input exceeds 64 KiB.
        """
        if len(dumps(self.model_dump(mode="json"))) > 65536:
            raise ValueError("Authoring input exceeds 64 KiB")
        return self


class HTTPRequest(AuthoringInput):
    """Describe one request using the HTTP node's fields and encoding rules."""

    url: str = Field(min_length=1, max_length=1024)
    method: Literal["GET", "HEAD", "POST"] = "GET"
    headers: dict[str, str] = Field(default_factory=dict, max_length=32)
    body: str = Field(default="", max_length=16384)
    client: Literal["httpx", "curl_cffi"] = "httpx"
    timeout: int = Field(default=10, ge=1, le=10, strict=True)


class HTTPProbe(AuthoringInput):
    """Probe one explicit endpoint with disposable cookies and supplied fixtures."""

    request: HTTPRequest
    context: dict[str, Any] = Field(default_factory=dict)
    formatter: str | None = Field(default=None, max_length=8192)


class TemplateValidation(AuthoringInput):
    """Render supplied data using the target field's expected result form."""

    template: str = Field(max_length=8192)
    context: dict[str, Any] = Field(default_factory=dict)
    mode: Literal["text", "expression", "json", "object", "array"] = "text"
    strict: bool = False


class TemplateResult(BaseModel):
    """Describe fixture evaluation, without asserting workflow readiness."""

    valid: bool
    value_type: str | None = None
    rendered: Any = None
    value: Any = None
    error: str | None = None
    line: int | None = None


class HTTPProbeResult(BaseModel):
    """Report observed HTTP facts and explicit sample completeness."""

    status_code: int | None = None
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    text: str = ""
    bytes_read: int = Field(
        default=0, description="Decoded bytes observed; a lower bound when incomplete."
    )
    body_complete: bool = Field(
        default=False,
        description="Whether the response stream reached its end within the cap.",
    )
    text_truncated: bool = Field(
        default=False,
        description="Whether text is only a sample, even if the full body was read.",
    )
    formatted: TemplateResult | None = None
    error: str | None = None
