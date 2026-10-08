"""Workflow nodes for video and reading metadata output."""

from app.core.flow.nodes.base import Node
from app.utils.importer import import_subclasses

# import all node classes in the current directory
import_subclasses(Node, globals())
