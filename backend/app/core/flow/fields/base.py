from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from app.core.flow.context import Context
from app.core.renderer import render


@dataclass(kw_only=True)
class Field[V](ABC):
    """The base dataclass for all node fields."""

    id: str = field(init=False, repr=False)
    field_type: str = field(init=False, repr=False)
    span: int | None = None
    label: str | None = field(kw_only=False, default=None)
    tooltip: str | None = None
    required: bool = False
    default: V

    def __post_init__(self):
        if self.span is not None and not 1 <= self.span <= 100:
            raise ValueError("span must be between 1 and 100")
        self.field_type = self._field_type()

    @abstractmethod
    def _field_type(self) -> str:
        """The method to be implemented by the subclasses to return the field type.

        Returns:
            The field type.
        """
        raise NotImplementedError

    def extract(
        self, data: dict, *, context: Context | None = None, raw: bool = False
    ) -> V:
        """Extract the field value from the node data.

        Args:
            data: The node data to extract the field value.
            context: The context to render the field value.
            raw: Whether to render the value as raw object.

        Returns:
            The field value.
        """
        value = data.get(self.id, self.default)
        if context is not None:
            value = render(value, context._context, raw=raw)
        return value
