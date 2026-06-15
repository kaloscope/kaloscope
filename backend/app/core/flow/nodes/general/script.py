import builtins as _builtins
import copy
from typing import Any

from quickjs_rs import Runtime

from app.core.config import KaloscopeConfig
from app.core.flow.context import Context
from app.core.flow.fields import ScriptField
from app.core.flow.nodes.base import Node, general_node

# builtins removed from exec() namespace in strict mode
_RESTRICTED_BUILTINS = frozenset(
    {
        # dynamic code execution
        "__import__",
        "compile",
        "eval",
        "exec",
        # namespace introspection
        "globals",
        "locals",
        "vars",
        # I/O & interaction
        "breakpoint",
        "input",
        "open",
    }
)

# pre-computed filtered builtins for strict mode
_STRICT_BUILTINS: dict[str, Any] | None = None


def _get_strict_builtins() -> dict[str, Any]:
    """Return a filtered copy of builtins with dangerous entries removed.

    The result is computed once and cached for the process lifetime.

    Returns:
        The filtered builtins dictionary.
    """
    global _STRICT_BUILTINS
    if _STRICT_BUILTINS is None:
        _STRICT_BUILTINS = {
            k: v for k, v in _builtins.__dict__.items() if k not in _RESTRICTED_BUILTINS
        }
    return _STRICT_BUILTINS


@general_node(order=4, icon="code")
class ScriptNode(Node):
    script = ScriptField("code", required=True)

    @classmethod
    async def execute(
        cls, *, node_id: str, node_data: dict[str, Any], context: Context, **kwargs
    ):
        script = cls.script.extract(node_data)
        if not (script_code := script["code"]):
            return

        language = script["language"]
        if language == "python":
            # https://docs.python.org/3/library/functions.html#exec
            namespace: dict[str, Any] = {
                "node_id": node_id,
                "node_data": node_data,
                "context": context,
            }

            # apply strict mode if enabled
            if KaloscopeConfig.get().script_strict_mode:
                namespace["__builtins__"] = _get_strict_builtins()

            # execute the script
            exec(f"{script_code}\n\nexecute(node_id, node_data, context)", namespace)

        elif language == "javascript":
            # https://github.com/langchain-ai/quickjs-rs
            node_data_copy = copy.deepcopy(node_data)
            context_copy = copy.deepcopy(dict(context.items()))
            context_keys = set(context.storage.keys())

            def _set_handle_items(handle, data: dict[str, Any]):
                for key, value in data.items():
                    handle.set(key, value)

            with Runtime() as runtime, runtime.new_context() as js_ctx:
                js_ctx.eval(script_code)
                with (
                    js_ctx.eval_handle("({})") as node_data_handle,
                    js_ctx.eval_handle("({})") as context_handle,
                ):
                    # set the node data and context onto the QuickJS handles
                    _set_handle_items(node_data_handle, node_data_copy)
                    _set_handle_items(context_handle, context_copy)

                    # execute the "execute" function defined in the JavaScript code
                    with js_ctx.eval_handle("execute") as execute:
                        execute.call(node_id, node_data_handle, context_handle)

                    # read back the node data and context from the QuickJS handles
                    _node_data = node_data_handle.to_python(allow_opaque=True)
                    _context = context_handle.to_python(allow_opaque=True)

            # apply mutations to the original node data
            node_data.clear()
            node_data.update(_node_data)

            # apply mutations to the original context
            for key in context_keys - _context.keys():
                context.pop(key, None)
            context.update(
                {k: v for k, v in _context.items() if context_copy.get(k) != v}
            )
