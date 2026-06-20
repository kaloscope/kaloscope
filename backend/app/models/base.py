from dataclasses import dataclass
from typing import Annotated, Any, Self

from pydantic import BaseModel, Field, PositiveInt, model_validator
from sanic import Request, SanicException
from sanic.request.form import File
from tortoise import timezone
from tortoise.contrib.pydantic.base import PydanticListModel, PydanticModel
from tortoise.contrib.pydantic.creator import (
    pydantic_model_creator,
    pydantic_queryset_creator,
)
from tortoise.expressions import Q
from tortoise.fields import DatetimeField, IntField
from tortoise.models import Model
from tortoise.queryset import QuerySet, UpdateQuery


def patched_update(self, **kwargs: Any) -> UpdateQuery:
    """Update all objects in QuerySet with given kwargs."""
    if "updated_at" not in kwargs:
        kwargs["updated_at"] = timezone.now()
    return original_update(self, **kwargs)


# patch the update method of QuerySet to set updated_at automatically
# https://github.com/tortoise/tortoise-orm/issues/1574
original_update = QuerySet.update
QuerySet.update = patched_update


class IDs(BaseModel):
    """A list of IDs."""

    ids: list[PositiveInt | Annotated[str, Field(min_length=1)]] = Field(
        min_length=1, max_length=999
    )


class KVPair(BaseModel):
    """A key-value pair."""

    key: str
    value: str | bool | int


@dataclass
class Page[M: Model]:
    """A paginated list wrapper."""

    total: int
    items: list[M]


class Pageable(BaseModel):
    """The base class for pageable request parameters."""

    page_num: int = Field(ge=0, default=1)
    page_size: int = Field(ge=1, default=10)
    ordering: str | None = None

    @property
    def page_params(self) -> dict[str, Any]:
        """Returns the page parameters."""
        return {
            "page_num": self.page_num,
            "page_size": self.page_size,
            "ordering": self.ordering,
        }


class Range:
    """A range object for HTTP Range requests."""

    __slots__ = ("start", "end", "size", "total")

    def __init__(
        self,
        start: int | None,
        end: int | None,
        size: int | None,
        total: int | None,
    ):
        self.start = start
        self.end = end
        self.size = size
        self.total = total


class TortoiseModel(Model):
    """The base class for all Tortoise ORM models."""

    id = IntField(primary_key=True)
    created_at = DatetimeField(null=True, auto_now_add=True)
    updated_at = DatetimeField(null=True, auto_now=True)

    class Meta:
        abstract = True

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # cache for Pydantic models
        cls._pydantic_models = {}
        cls._pydantic_querysets = {}

    @classmethod
    def pydantic(
        cls, *, include: tuple[str, ...] = (), exclude: tuple[str, ...] = ()
    ) -> type[PydanticModel]:
        """Creates PydanticModel for the class.

        Args:
            include: The fields to include from the model.
            exclude: The fields to exclude from the model.

        Returns:
            The PydanticModel for the class.
        """
        key = (include, exclude)
        model = cls._pydantic_models.get(key)
        if model is None:
            model = pydantic_model_creator(cls, include=include, exclude=exclude)
            cls._pydantic_models[key] = model
        return model

    @classmethod
    def pydantic_list(
        cls, *, include: tuple[str, ...] = (), exclude: tuple[str, ...] = ()
    ) -> type[PydanticListModel]:
        """Creates PydanticListModel for the class.

        Args:
            include: The fields to include from the model.
            exclude: The fields to exclude from the model.

        Returns:
            The PydanticListModel for the class.
        """
        key = (include, exclude)
        model = cls._pydantic_querysets.get(key)
        if model is None:
            model = pydantic_queryset_creator(cls, include=include, exclude=exclude)
            cls._pydantic_querysets[key] = model
        return model

    @classmethod
    async def page(
        cls,
        *args: Q,
        page_num: int = 1,
        page_size: int = 10,
        ordering: str | None = None,
        annotations: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Page[Self]:
        """Generates a paginated list with the filter applied.

        Args:
            page_num: The page number. Defaults to 1.
            page_size: The page size. Defaults to 10.
            ordering: The ordering. Defaults to None.
            annotations: The annotations to apply to the queryset.

        Returns:
            The paginated list.
        """
        queryset = cls.filter(*args, **kwargs)
        if ordering:
            queryset = queryset.order_by(ordering)
        if annotations:
            queryset = queryset.annotate(**annotations)
        # return all if page_num is 0
        if page_num == 0:
            items = await queryset
            return Page(len(items), items)
        # return empty result if total is 0
        total = await queryset.count()
        if total == 0:
            return Page(0, [])
        # return the paginated list
        return Page(
            total, await queryset.offset((page_num - 1) * page_size).limit(page_size)
        )


class RequestFilesMixin:
    """The mixin class for request files."""

    @model_validator(mode="before")
    @classmethod
    def set_request_files(cls, data: Any) -> Any:
        """Set the request files to the model data.

        Args:
            data: The model data.

        Returns:
            The updated model data.
        """
        if not isinstance(data, dict):
            return data

        # https://sanic.dev/en/guide/basics/request.html#current-request-getter
        try:
            request = Request.get_current()
        except SanicException:
            pass
        else:
            if not request.files:
                return data
            for var, var_type in cls.__annotations__.items():
                if var_type == File or var_type == (File | None):
                    file: File | None = request.files.get(var)
                    if file and (file.name or file.body):
                        data[var] = file
        return data
