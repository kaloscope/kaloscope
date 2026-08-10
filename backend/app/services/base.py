from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from sanic import Config, Sanic
from sanic.types.shared_ctx import SharedContext
from tortoise.queryset import QuerySet

from app.models.base import Page, TortoiseModel

# https://docs.pydantic.dev/latest/concepts/serialization/#advanced-include-and-exclude
type IncExc = (
    set[int] | set[str] | Mapping[int, IncExc | bool] | Mapping[str, IncExc | bool]
)


def _inc_exc(fields: tuple[str, ...] | IncExc) -> tuple[tuple[str, ...], IncExc | None]:
    """Convert include/exclude fields to a tuple and an optional IncExc.

    Args:
        fields: The fields to convert.

    Returns:
        A tuple containing the fields as a tuple and an optional IncExc.
    """
    if isinstance(fields, tuple):
        return fields, None
    return (), fields


class BaseService[M: TortoiseModel]:
    """The base class for all services."""

    def __init_subclass__(cls, model: type[M], **kwargs):
        """Initialize the subclass.

        Args:
            model: The Tortoise ORM model class.
        """
        cls._modelcls = model
        super().__init_subclass__(**kwargs)

    def __new__(cls, *args, **kwargs):
        """Prevent instantiation of the class directly.

        Raises:
            ValueError: If the class is instantiated directly.
        """
        raise ValueError(f"cannot instantiate {cls.__name__} directly")

    @classmethod
    def app_config(cls) -> Config:
        """Get the application config.

        Returns:
            The application config.
        """
        return Sanic.get_app().config

    @classmethod
    def app_ctx(cls) -> SimpleNamespace:
        """Get the application context.

        Returns:
            The application context.
        """
        return Sanic.get_app().ctx

    @classmethod
    def shared_ctx(cls) -> SharedContext:
        """Get the shared context.

        Returns:
            The shared context.
        """
        return Sanic.get_app().shared_ctx

    @classmethod
    async def dump(
        cls,
        obj: M,
        *,
        include: tuple[str, ...] | IncExc = (),
        exclude: tuple[str, ...] | IncExc = (),
    ) -> dict[str, Any]:
        """Dumps a Tortoise ORM model object to a Python dict.

        Args:
            obj: The Tortoise ORM model object.
            include: The fields to include in the output.
            exclude: The fields to exclude from the output.

        Returns:
            The dumped dict.
        """
        # split include/exclude into different formats
        model_inc, dump_inc = _inc_exc(include)
        model_exc, dump_exc = _inc_exc(exclude)
        # create the pydantic model
        pydantic_model = cls._modelcls.pydantic(include=model_inc, exclude=model_exc)
        # dump the object using the pydantic model
        return (await pydantic_model.from_tortoise_orm(obj)).model_dump(
            include=dump_inc, exclude=dump_exc
        )

    @classmethod
    async def dump_list(
        cls,
        objects: list[M] | QuerySet[M],
        *,
        include: tuple[str, ...] | IncExc = (),
        exclude: tuple[str, ...] | IncExc = (),
    ) -> Any:
        """Dumps a list of Tortoise ORM model objects to a Python list.

        Args:
            objects: The list of Tortoise ORM model objects.
            include: The fields to include in the output.
            exclude: The fields to exclude from the output.

        Returns:
            The dumped list.
        """
        # split include/exclude into different formats
        model_inc, dump_inc = _inc_exc(include)
        model_exc, dump_exc = _inc_exc(exclude)

        if isinstance(objects, QuerySet):
            # create the pydantic list model
            list_model = cls._modelcls.pydantic_list(
                include=model_inc, exclude=model_exc
            )
            # dump the queryset using the pydantic list model
            return (await list_model.from_queryset(objects)).model_dump(
                include=dump_inc, exclude=dump_exc
            )

        # if `objects` is not a QuerySet, assume it's a list of objects
        return [
            await cls.dump(obj, include=include, exclude=exclude) for obj in objects
        ]

    @classmethod
    async def dump_page(
        cls,
        page: Page[M],
        *,
        include: tuple[str, ...] | IncExc = (),
        exclude: tuple[str, ...] | IncExc = (),
    ) -> dict[str, Any]:
        """Dumps a paginated list to a Python dict.

        Args:
            page: The paginated list.
            include: The fields to include in the output.
            exclude: The fields to exclude from the output.

        Returns:
            The dumped paginated list.
        """
        return {
            "total": page.total,
            "items": await cls.dump_list(page.items, include=include, exclude=exclude),
        }
