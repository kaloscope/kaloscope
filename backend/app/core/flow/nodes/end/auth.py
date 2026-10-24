import time
from typing import Any

from sanic import Sanic

from app.core.cookiejar import SQLiteCookieJar
from app.core.flow.context import AUTH_KEY, RETVAL_KEY, Context
from app.core.flow.fields import CodeField, ToggleField
from app.core.flow.handles import InputHandle
from app.core.flow.nodes.base import (
    CancellationSignal,
    Node,
    end_node,
    start_config,
)
from app.models.flow import FlowVariable, GraphCategory
from app.models.general import GlobalCookie
from app.utils import json


@end_node(order=1, icon="keyFilled", categories=(GraphCategory.INDEXER,))
class AuthEndNode(Node):
    """Return and persist indexer authentication.

    Input: render `response` to the object documented in `example`.
    Output: `$retval`; a nonempty `name` also persists workflow-local `$auth`,
    consuming `expires_in` from both results to compute expiry. Empty `name`
    leaves cached authentication unchanged. Configured cookies are saved.
    `force_end=true` ends the whole run; otherwise only this branch ends."""

    example = CodeField(
        "response_example",
        language="jsonc",
        collapse=True,
        readonly=True,
        template="resp/auth.jsonc",
    )
    response = CodeField(
        "response",
        required=True,
        language="jinja2",
        darkmode=True,
        default=json.pretty({"name": ""}),
    )
    force_end = ToggleField("force_end", tooltip="force_end", required=True)

    class Handles:
        input = InputHandle(tag="auth")

    @classmethod
    async def execute(
        cls, *, graph_id: int, node_data: dict[str, Any], context: Context, **kwargs
    ):
        # extract the response
        """Store the authentication result and persist its expiry.

        Args:
            graph_id: Flow graph ID.
            node_data: Configured fields and mutable node state.
            context: Shared variables and storage for this flow run.
            **kwargs: Unused execution arguments supplied by the flow engine.

        Raises:
            CancellationSignal: If the flow should end without an error.
        """
        retval = json.try_loads(
            cls.response.extract(node_data, context=context), with_comments=True
        )
        context[RETVAL_KEY] = retval

        # handle the cookie
        expires = None
        config = await start_config(graph_id, "auth")
        cookie = config.get("auth", {}).get("cookie")
        if isinstance(cookie, dict) and (domain := cookie.get("domain")):
            # persist the cookies
            cookies: SQLiteCookieJar = Sanic.get_app().ctx.cookies
            await cookies.save()
            # get the cookie expiration time
            if (path := cookie.get("path")) and (name := cookie.get("name")):
                _cookie = await GlobalCookie.get_or_none(
                    domain=domain, path=path, name=name
                )
                expires = _cookie.expires if _cookie else None

        # handle the response
        if isinstance(retval, dict) and retval.get("name"):
            # delete the old auth variable
            await FlowVariable.filter(graph_id=graph_id, key=AUTH_KEY).delete()
            # calculate the expiration time
            expires_in = retval.pop("expires_in", None)
            expires = (
                int(time.time() + expires_in)
                if isinstance(expires_in, int)
                else expires
            )
            # persist the auth variable
            await FlowVariable.create(
                graph_id=graph_id, key=AUTH_KEY, value=retval, expires=expires
            )

        # force end the flow if required
        if cls.force_end.extract(node_data):
            raise CancellationSignal
