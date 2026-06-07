import re
from abc import ABCMeta, abstractmethod
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import Any, Protocol, TypeVar

import yaml
from sanic.log import Colors, logger

from app.core.flow.context import OUTPUT_KEY, Context
from app.core.flow.edge import Source
from app.core.flow.fields import DividerField, Field
from app.core.flow.handles import (
    STOP,
    DefaultHandles,
    Handle,
    InputHandle,
    OutputHandle,
)
from app.models.flow import FlowGraph, GraphCategory

N = TypeVar("N", bound="Node")


class CancellationSignal(Exception):
    """Exception raised to cancel flow task execution."""

    def __init__(self, message: str = "Execution cancelled."):
        super().__init__(message)
        self.message = message

    def __str__(self):
        return self.message


class NodeGroup(StrEnum):
    """The group to categorize the node."""

    START = auto()
    GENERAL = auto()
    CONTROL = auto()
    END = auto()

    @property
    def index(self) -> int:
        return list(NodeGroup).index(self)


@dataclass(frozen=True)
class NodeSchema:
    """The schema of a node."""

    node_type: str
    name: str
    icon: str
    group: NodeGroup
    order: int
    fields: tuple[Field, ...]
    handles: tuple[Handle, ...]
    categories: tuple[GraphCategory, ...]

    def __eq__(self, other):
        if isinstance(other, NodeSchema):
            return self.node_type == other.node_type
        return False

    def __hash__(self):
        return hash(self.node_type)

    @staticmethod
    def get_type(name: str) -> str:
        """Convert the input name to node type.

        Args:
            name: The input name to convert.

        Returns:
            The converted node type.
        """
        name = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", name)
        name = re.sub("([a-z0-9])([A-Z])", r"\1_\2", name).lower()
        return name[:-5] if name.endswith("_node") else name


class NodeMeta(ABCMeta):
    """The metaclass for the node class."""

    def __new__(metacls, name: str, bases: tuple[type, ...], namespace: dict[str, Any]):
        if name == "Node":
            return super().__new__(metacls, name, bases, namespace)

        # collect the node fields
        fields: list[Field] = []
        for index, (attr_name, field) in enumerate(namespace.items()):
            if isinstance(field, DividerField):
                field.id = f"divider_{index}"
                fields.append(field)
            elif not attr_name.startswith("__") and isinstance(field, Field):
                field.id = attr_name
                if field.label is None:
                    field.label = f"{NodeSchema.get_type(name)}_{attr_name}"
                fields.append(field)
        namespace["_fields"] = fields

        # collect the node handles
        handles: list[Handle] = []
        handles_cls = namespace.get("Handles", DefaultHandles)
        for attr_name in dir(handles_cls):
            if not attr_name.startswith("__"):
                handle = getattr(handles_cls, attr_name)
                if isinstance(handle, Handle):
                    handle.id = attr_name
                    handles.append(handle)
        namespace["_handles"] = handles

        return super().__new__(metacls, name, bases, namespace)


class NodeExecutor(Protocol):
    """The type of the node executor."""

    async def __call__(
        self,
        *,
        graph_id: int,
        node_id: str,
        node_data: dict[str, Any],
        context: Context,
        input_handle: InputHandle | None = None,
        source: Source | None = None,
        sources: list[Source] | None = None,
    ) -> OutputHandle | None: ...


class Node(metaclass=NodeMeta):
    """The base class for all nodes."""

    schemas: set[NodeSchema] = set()
    executors: dict[str, NodeExecutor] = {}
    _fields: list[Field]
    _handles: list[Handle]

    @classmethod
    def register(
        cls,
        group: NodeGroup,
        *,
        order: int = 0,
        icon: str | None = None,
        categories: tuple[GraphCategory, ...] = (),
    ):
        """Register a node class into the node schemas container.

        Args:
            group: The group of the node.
            order: The order of the node in the group.
            icon: The icon name of the node for the frontend UI.
            categories: The available graph categories for the node.
        """

        def decorator(node_cls: type[N]) -> type[N]:
            schema = NodeSchema(
                # convert the node class name to node type
                NodeSchema.get_type(node_cls.__name__),
                node_cls.__name__,
                icon or "app",
                group,
                order,
                tuple(node_cls._fields),
                tuple(node_cls._handles),
                categories,
            )
            # check if the node type is duplicated
            if schema in cls.schemas:
                raise ValueError(f"duplicate node schema type: {schema.node_type}")
            cls.schemas.add(schema)
            cls.executors[schema.node_type] = node_cls._execute
            return node_cls

        return decorator

    @classmethod
    def compress(cls, definition: dict) -> dict:
        """Compress the node schema in the flow definition.

        Args:
            definition: The definition to compress.

        Returns:
            The compressed definition.
        """
        for node in definition["nodes"]:
            node["data"]["$schema"] = node["data"]["$schema"]["node_type"]
        return definition

    @classmethod
    def decompress(cls, definition: dict) -> dict:
        """Decompress the node schema in the flow definition.

        Args:
            definition: The definition to decompress.

        Returns:
            The decompressed definition.
        """
        schemas = {schema.node_type: schema for schema in cls.schemas}
        for node in definition["nodes"]:
            node["data"]["$schema"] = schemas[node["data"]["$schema"]]
        return definition

    @classmethod
    async def _execute(
        cls,
        *,
        graph_id: int,
        node_id: str,
        node_data: dict[str, Any],
        context: Context,
        input_handle: InputHandle | None = None,
        source: Source | None = None,
        sources: list[Source] | None = None,
    ) -> OutputHandle | None:
        """Execute the node with the given data.

        Args:
            graph_id: The flow graph ID.
            node_id: The node ID.
            node_data: The node data.
            context: The context to store the intermediate data.
            input_handle: The input handle.
            source: The edge source for this execution.
            sources: The incoming edge sources for this execution.

        Returns:
            The output handle.
        """
        # log the execution
        logger.debug(
            f"{Colors.BOLD}{Colors.SANIC}<%s>"
            f"{Colors.BLUE} %s{Colors.YELLOW} %s{Colors.END}",
            cls.__name__,
            node_id,
            f"{input_handle.id} =>" if input_handle else "",
        )
        # skip the execution if the output handle is restored from the snapshot
        output_handle = OutputHandle.from_snapshot(node_data.get(OUTPUT_KEY))
        if output_handle is None:
            # call the execute method implemented by the subclass
            output_handle = await cls.execute(
                graph_id=graph_id,
                node_id=node_id,
                node_data=node_data,
                context=context,
                input_handle=input_handle,
                source=source,
                sources=sources,
            )
            if output_handle is STOP:
                # explicitly abort flow, skip default handle fallback
                output_handle = None
            elif output_handle is None:
                # get the default output handle if not provided
                output_handles = [
                    h for h in cls._handles if isinstance(h, OutputHandle)
                ]
                if len(output_handles) == 1:
                    handle_id = output_handles[0].id
                    loop_id = input_handle.loop_id if input_handle else None
                    output_handle = OutputHandle.create(handle_id, loop_id)
        if output_handle is not None:
            node_data[OUTPUT_KEY] = output_handle.snapshot
        # log the execution
        logger.debug(
            f"{Colors.BOLD}{Colors.SANIC}</%s>"
            f"{Colors.BLUE} %s{Colors.YELLOW} %s{Colors.END}",
            cls.__name__,
            node_id,
            f"=> {output_handle.id}" if output_handle else "",
        )
        return output_handle

    @classmethod
    @abstractmethod
    async def execute(cls, **kwargs) -> OutputHandle | None: ...


def node_register(
    group: NodeGroup,
    *,
    icon: str | None = None,
    categories: tuple[GraphCategory, ...] = (),
):
    """Create a node register decorator.

    Args:
        group: The default group of the node.
        icon: The default icon name of the node.
        categories: The default available graph categories.
    """
    _icon = icon
    _categories = categories

    def register(
        *,
        order: int = 0,
        icon: str | None = None,
        categories: tuple[GraphCategory, ...] = (),
    ):
        return Node.register(
            group, order=order, icon=icon or _icon, categories=categories or _categories
        )

    return register


async def start_config(graph: int | FlowGraph, key: str, *keys: str) -> dict[str, Any]:
    """Get the start node configuration of the flow graph.

    Args:
        graph: The flow graph ID or instance.
        key: The first required key.
        keys: The keys of the configuration to retrieve.

    Returns:
        The configuration dictionary.
    """
    # get the flow graph definition and extract the nodes
    if isinstance(graph, int):
        _graph = await FlowGraph.get_or_none(id=graph)
    else:
        _graph = graph

    nodes = None
    if _graph and _graph.definition:
        nodes = _graph.definition.get("nodes")
    if not isinstance(nodes, list):
        return {k: None for k in (key, *keys)}

    # load the configuration of the specified node type
    def load_config(node_type: str) -> dict[str, Any] | None:
        data = next(
            (node["data"] for node in nodes if node["data"]["$schema"] == node_type),
            None,
        )
        # return `None` if the node data is not found
        if data is None:
            return None
        # return an empty dictionary if the configuration is not provided
        config = data.get("config")
        if not isinstance(config, str) or config.strip() == "":
            return {}
        # parse the YAML configuration
        try:
            yaml_config = yaml.load(config, Loader=yaml.SafeLoader)
            if isinstance(yaml_config, dict):
                # dict key must be str
                def stringify_keys(obj: Any) -> Any:
                    if isinstance(obj, dict):
                        return {str(k): stringify_keys(v) for k, v in obj.items()}
                    if isinstance(obj, list):
                        return [stringify_keys(v) for v in obj]
                    return obj

                return stringify_keys(yaml_config)
        except yaml.YAMLError:
            logger.debug(
                f"Failed to parse the YAML configuration:\n{Colors.RED}%s{Colors.END}",
                config,
            )
        return {}

    return {k: load_config(f"{k}_start") for k in (key, *keys)}


# create the node register decorators
start_node = node_register(NodeGroup.START, icon="arrowRouting")
end_node = node_register(NodeGroup.END, icon="record")
general_node = node_register(NodeGroup.GENERAL)
control_node = node_register(NodeGroup.CONTROL)
