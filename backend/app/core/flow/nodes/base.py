import re
from abc import ABCMeta, abstractmethod
from dataclasses import dataclass
from enum import StrEnum, auto
from inspect import getdoc
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
    def order(self) -> int:
        return list(NodeGroup).index(self)


@dataclass(frozen=True)
class NodeSchema:
    """The schema of a node."""

    node_type: str
    name: str
    icon: str
    group: NodeGroup
    order: int
    width: str | None
    fields: tuple[Field, ...]
    handles: tuple[Handle, ...]
    categories: tuple[GraphCategory, ...]
    description: str

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
            name: Input name to convert.

        Returns:
            Converted node type.
        """
        name = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", name)
        name = re.sub("([a-z0-9])([A-Z])", r"\1_\2", name).lower()
        return name[:-5] if name.endswith("_node") else name


class NodeMeta(ABCMeta):
    """Collect fields and handles during node class creation."""

    def __new__(metacls, name: str, bases: tuple[type, ...], namespace: dict[str, Any]):
        """Collect declared fields and handles for a node class.

        Args:
            metacls: Node class builder.
            name: Node class name.
            bases: Parent classes.
            namespace: Declared fields, handles and methods.

        Returns:
            Node class with its field and handle definitions.
        """
        if name == "Node":
            return super().__new__(metacls, name, bases, namespace)

        # collect the node fields
        fields: list[Field] = []
        # documentation must not shift persisted divider field IDs
        members = ((key, value) for key, value in namespace.items() if key != "__doc__")
        for index, (attr_name, field) in enumerate(members):
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
        width: str | None = None,
        icon: str | None = None,
        categories: tuple[GraphCategory, ...] = (),
    ):
        """Register a node class into the node schemas container.

        Args:
            group: Group of the node.
            order: Order of the node in the group. Defaults to `0`.
            width: Width of the node content area. `None` uses the default node
                width.
            icon: Icon name of the node for the frontend UI. `None` uses the app
                icon.
            categories: Available graph categories for the node. An empty tuple
                permits all graph categories.

        Returns:
            Decorator that registers the node's schema and executor.
        """

        def decorator(node_cls: type[N]) -> type[N]:
            """Register a node schema and executor without changing its class.

            Args:
                node_cls: Node class to register.

            Returns:
                Registered node class.

            Raises:
                ValueError: If another node uses the same schema type.
            """
            schema = NodeSchema(
                # convert the node class name to node type
                NodeSchema.get_type(node_cls.__name__),
                node_cls.__name__,
                icon or "app",
                group,
                order,
                width,
                tuple(node_cls._fields),
                tuple(node_cls._handles),
                categories,
                getdoc(node_cls) or "",
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
            definition: Definition to compress.

        Returns:
            Compressed definition.
        """
        for node in definition["nodes"]:
            node["data"]["$schema"] = node["data"]["$schema"]["node_type"]
        return definition

    @classmethod
    def decompress(cls, definition: dict) -> dict:
        """Decompress the node schema in the flow definition.

        Args:
            definition: Definition to decompress.

        Returns:
            Decompressed definition.
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
            graph_id: Flow graph ID.
            node_id: Node ID.
            node_data: Node data.
            context: Context to store the intermediate data.
            input_handle: Input handle. `None` supplies no explicit input handle.
            source: Edge source for this execution. `None` means no incoming edge.
            sources: Incoming edge sources for this execution. `None` supplies no
                connected source list.

        Returns:
            Output handle.
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
        group: Default group of the node.
        icon: Default icon name of the node.
        categories: Default available graph categories.
    """
    _icon = icon
    _categories = categories

    def register(
        *,
        order: int = 0,
        width: str | None = None,
        icon: str | None = None,
        categories: tuple[GraphCategory, ...] = (),
    ):
        """Apply the group defaults to a node registration decorator.

        Args:
            order: Display order within the group; defaults to zero.
            width: Content width; `None` uses the frontend default.
            icon: Icon name; `None` uses the group default.
            categories: Allowed graph categories; empty uses the group defaults.

        Returns:
            Decorator that registers the node class.
        """
        return Node.register(
            group,
            order=order,
            width=width,
            icon=icon or _icon,
            categories=categories or _categories,
        )

    return register


async def start_config(graph: int | FlowGraph, key: str, *keys: str) -> dict[str, Any]:
    """Get start node configuration of the flow graph.

    Args:
        graph: Flow graph ID or instance.
        key: First required key.
        *keys: Keys of the configuration to retrieve.

    Returns:
        Configuration dictionary.
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
        """Read a start node configuration with string mapping keys.

        Args:
            node_type: Start node schema type.

        Returns:
            Parsed mapping, an empty mapping for invalid content, or `None` if the node
            is absent.
        """
        data = next(
            (node["data"] for node in nodes if node["data"]["$schema"] == node_type),
            None,
        )
        # return `None` if the node data is not found
        if data is None:
            return None
        return parse_start_config(data.get("config"))

    return {k: load_config(f"{k}_start") for k in (key, *keys)}


def parse_start_config(config: Any) -> dict[str, Any]:
    """Parse a start node's YAML configuration as runtime callers read it.

    Args:
        config: Raw `config` field value from the start node data.

    Returns:
        Mapping with string keys. Missing, blank, invalid or non-mapping YAML returns
        an empty mapping, matching how saved workflows are read.
    """
    # return an empty dictionary if the configuration is not provided
    if not isinstance(config, str) or config.strip() == "":
        return {}
    # parse the YAML configuration
    try:
        yaml_config = yaml.load(config, Loader=yaml.SafeLoader)
        if isinstance(yaml_config, dict):
            # dict key must be str
            def stringify_keys(obj: Any) -> Any:
                """Convert nested mapping keys to strings for configuration use.

                Args:
                    obj: Parsed YAML value.

                Returns:
                    Value with nested mapping keys converted to strings.
                """
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


# create the node register decorators
start_node = node_register(NodeGroup.START, icon="arrowRouting")
end_node = node_register(NodeGroup.END, icon="record")
general_node = node_register(NodeGroup.GENERAL)
control_node = node_register(NodeGroup.CONTROL)
