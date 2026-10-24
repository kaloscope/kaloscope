from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.core.flow.fields.base import Field

# define the language type for the code field
type Language = Literal["yaml", "json", "jsonc", "jinja2", "python", "javascript"]


@dataclass(kw_only=True)
class CodeField(Field[str]):
    """A control for code editing."""

    default: str = ""
    template: str | None = None
    placeholder: str | None = None
    language: Language | None = None
    tabsize: int = 2
    lineLength: int = 80
    collapse: bool = False
    readonly: bool = False
    darkmode: bool = False
    copier: bool = True
    resetter: bool = True
    formatter: bool = True
    width: str | None = "24rem"

    def __post_init__(self):
        """Load the declared example into the shared field definition."""
        super().__post_init__()
        if self.template:
            self.default = (
                Path(__file__).parents[4] / "static" / "flow" / self.template
            ).read_text(encoding="utf-8")

    def _field_type(self) -> str:
        return "code"
