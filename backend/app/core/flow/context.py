import copy
import time
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from app.core.decorators import after
from app.models.flow import FlowVariable
from app.models.general import GlobalVariable
from app.utils.crypto import xor_decrypt
from app.utils.deep import deep_update
from app.utils.dict import TrackableDict

with open(Path(__file__).parents[3] / "pyproject.toml", "rb") as _f:
    _KS_VERSION: str = tomllib.load(_f)["project"]["version"]

AUTH_KEY = "$auth"
"""The key to the indexer authentication in the local variables."""

MANUAL_KEY = "$manual"
"""The key to the manual execution flag in the boot parameters."""

START_KEY = "$start"
"""The key to the start node type in the boot parameters."""

RETVAL_KEY = "$retval"
"""The key to the execution result in the flow context."""

OUTPUT_KEY = "$output"
"""The key to the output handle in the node data."""

LOOP_KEY = "$loop"
"""The key to the loop variable in the node data."""

IDX_KEY = "$index"
"""The key to the loop index in the loop variable."""


@dataclass(init=False)
class Context:
    _context: dict[str, Any]
    _deleted: set[str]
    # the global variables
    globalvars: Mapping[str, str]
    # the local variables
    localvars: Mapping[str, Any]
    # the boot parameters
    bootparams: Mapping[str, Any]
    # the storage dictionary
    storage: TrackableDict[str, Any]
    # the loop variable
    loopvar: Mapping[str, Any] | None

    def __new__(cls, *args, **kwargs):
        context = super().__new__(cls)
        context._deleted = set()
        context.loopvar = None
        return context

    @classmethod
    async def create(
        cls,
        graph_id: int,
        bootparams: Mapping[str, Any],
        storage: dict[str, Any] | None = None,
    ) -> Self:
        """Create a new context object.

        Args:
            graph_id: The flow graph ID.
            bootparams: The boot parameters.
            storage: The storage dictionary.

        Returns:
            A new context object.
        """
        # load global variables
        globalvars = await GlobalVariable.all()
        globalvars = {
            g.key: (xor_decrypt(g.value) if g.encrypted else g.value)
            for g in globalvars
        }
        # load local variables
        now = time.time()
        await FlowVariable.filter(graph_id=graph_id, expires__lt=now).delete()
        localvars = await FlowVariable.filter(graph_id=graph_id)
        localvars = {f.key: f.value for f in localvars}
        # create a new instance
        context = cls.__new__(cls)
        context.globalvars = globalvars
        context.localvars = localvars
        context.bootparams = bootparams
        context.storage = TrackableDict(storage or {})
        context.union()
        return context

    def union(self):
        """Merge the context variables into a single dictionary."""
        merged = {"ks_version": _KS_VERSION}
        merged.update(self.globalvars)
        merged.update(self.localvars)
        merged.update(self.bootparams)
        merged.update(self.storage)
        if self.loopvar is not None:
            merged.update(self.loopvar)
        self._context = merged

    def __getitem__(self, key: str):
        return self._context[key]

    def get(self, key: str, default: Any = None):
        return self._context.get(key, default)

    def keys(self):
        return self._context.keys()

    def values(self):
        return self._context.values()

    def items(self):
        return self._context.items()

    @after("union")
    def __setitem__(self, key: str, value: Any):
        self._deleted.discard(key)
        self.storage[key] = value

    @after("union")
    def setdefault(self, key: str, default: Any):
        self._deleted.discard(key)
        return self.storage.setdefault(key, default)

    @after("union")
    def __delitem__(self, key: str):
        self._deleted.add(key)
        del self.storage[key]

    @after("union")
    def pop(self, key: str, default: Any = None):
        if key in self.storage:
            self._deleted.add(key)
        return self.storage.pop(key, default)

    @after("union")
    def clear(self):
        self._deleted.update(self.storage.keys())
        self.storage.clear()

    @after("union")
    def update(self, other: Self | dict[str, Any]) -> Self:
        """Merge the context with another context.

        Args:
            other: The other context to merge with.

        Returns:
            The merged context.
        """
        if isinstance(other, Context):
            # remove keys that are marked as deleted in the other context
            for key in other._deleted:
                self.storage.pop(key, None)
                self._deleted.add(key)
            self._deleted.difference_update(other.storage.keys())
            deep_update(self.storage, other.storage)
        else:
            self._deleted.difference_update(other.keys())
            deep_update(self.storage, other)
        return self

    @after("union")
    def bind_loop(self, node_data: dict[str, Any]) -> Self:
        """Bind the loop variable to the context.

        Args:
            node_data: The loop node data.

        Returns:
            The context object.
        """
        self.loopvar = node_data.get(LOOP_KEY)
        return self

    def is_modified(self) -> bool:
        """Check if the context has been modified.

        Returns:
            True if the context has been modified, False otherwise.
        """
        return self.storage.is_modified()

    def copy(self) -> Self:
        """Create a copy of the context.

        Returns:
            A copy of the context.
        """
        context = self.__class__.__new__(self.__class__)
        context.globalvars = self.globalvars
        context.localvars = self.localvars
        context.bootparams = self.bootparams
        context.storage = TrackableDict(copy.deepcopy(self.storage))
        context.union()
        return context
