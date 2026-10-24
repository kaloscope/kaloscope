from app.core.flow.fields import CodeField
from app.core.flow.handles import OutputHandle
from app.core.flow.nodes.base import Node, start_node
from app.models.flow import GraphCategory

AUTH_CONFIG = """
login:
  mode: password # password | captcha | qrcode
  required: false

cookie:
  domain:
  path: /
  name:
""".lstrip()


@start_node(order=1, icon="key", categories=(GraphCategory.INDEXER,))
class AuthStartNode(Node):
    """Start an indexer login request.

    Input: caller credentials shown in `example` become top-level context variables.
    `config.login` selects the login UI; `config.cookie` selects saved cookies.
    Output: unchanged context through `output`; connect to an `auth_end` path.
    This node does not authenticate by itself."""

    example = CodeField(
        "request_example",
        language="jsonc",
        collapse=True,
        readonly=True,
        template="req/auth.jsonc",
    )
    config = CodeField(
        "config",
        language="yaml",
        darkmode=True,
        default=AUTH_CONFIG,
    )

    class Handles:
        output = OutputHandle(tag="auth")
