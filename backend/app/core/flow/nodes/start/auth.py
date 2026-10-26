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
    """Start an indexer password login.

    Input: `username`/`password` from the search-page login form, as in `example`.
    `config.login` (a mapping) shows that form; `required: true` blocks search until
    login. `config.cookie` (`domain`/`path`/`name`) names the stored session cookie
    for login status and expiry. Output: unchanged context; connect `http`, then
    `auth_end`. It does not authenticate."""

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
