from dataclasses import dataclass

from app.core.flow.fields.base import Field


@dataclass(kw_only=True)
class NumberField(Field[float | None]):
    """Define a numeric input that can use `None` as its empty default."""

    default: float | None = 0
    placeholder: str | None = None
    min: float | None = None
    max: float | None = None
    step: float = 1
    precision: int = 0

    def _field_type(self) -> str:
        return "number"
