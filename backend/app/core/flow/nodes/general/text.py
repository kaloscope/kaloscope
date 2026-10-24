from pathlib import Path
from typing import Any

from app.core.constants import ENCODING
from app.core.flow.context import Context
from app.core.flow.fields.code import CodeField
from app.core.flow.fields.select import SelectField
from app.core.flow.fields.text import TextField
from app.core.flow.nodes.base import Node, general_node
from app.models.media import MediaLib


@general_node(order=2, icon="documentSignature")
class TextNode(Node):
    """Write rendered text to a file inside a registered media library.

    Input: `path`, `content`, and mode `x` (create), `w` (replace), or `a` (append).
    Example: path `/media/note.txt`, content `{{ title }}` writes the title if that
    path belongs to a library. Parent directories are created. Outside paths are
    skipped. Output: no new context variables. Execution changes files."""

    path = TextField("path", required=True, maxlength=1024)
    mode = SelectField(
        required=True,
        options={
            "exclusive": "x",
            "overwrite": "w",
            "append": "a",
        },
        default="x",
    )
    content = CodeField(required=True, language="jinja2", darkmode=True)

    @classmethod
    async def execute(cls, *, node_data: dict[str, Any], context: Context, **kwargs):
        """Write rendered text only within a registered media library.

        Args:
            node_data: Configured fields and mutable node state.
            context: Shared variables and storage for this flow run.
            **kwargs: Unused execution arguments supplied by the flow engine.
        """
        path = Path(cls.path.extract(node_data, context=context))

        # security: path must be under one of the MediaLib dirs
        lib_dirs: list = await MediaLib.all().values_list("dir", flat=True)
        if not any(path.is_relative_to(Path(dir)) for dir in lib_dirs):
            return

        # create parent directory if it doesn't exist
        path.parent.mkdir(parents=True, exist_ok=True)

        # render the content and write to the file
        mode = str(cls.mode.extract(node_data))
        content = cls.content.extract(node_data, context=context)
        with open(path, mode=mode, encoding=ENCODING) as f:
            f.write(content)
