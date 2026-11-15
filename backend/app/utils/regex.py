"""Run untrusted regular expressions where a runaway match cannot stall a worker."""

import re
import subprocess
import sys

from app.utils.json import dumps, loads

_CHILD = """
import json, re, sys
request = json.load(sys.stdin)
try:
    pattern = re.compile(request["pattern"])
    result = [pattern.sub(repl, text) for repl, text in request["pairs"]]
    json.dump({"result": result}, sys.stdout)
except re.error as error:
    json.dump({"error": str(error)}, sys.stdout)
"""


def substitute(
    pattern: str, pairs: list[tuple[str, str]], *, timeout: float = 3
) -> list[str]:
    """Replace a pattern in many short texts inside a disposable interpreter.

    The standard engine has no time limit, and a pattern with catastrophic
    backtracking would occupy a worker thread until the process ends. A child
    process can be killed instead.

    Args:
        pattern: Regular expression supplied by a user or a model.
        pairs: Replacement and text for each substitution.
        timeout: Seconds allowed for every substitution together.

    Returns:
        The substituted texts in input order.

    Raises:
        re.error: If the pattern or a replacement is invalid.
        TimeoutError: If the substitutions do not finish in time.
    """
    if not pairs:
        re.compile(pattern)
        return []
    try:
        done = subprocess.run(
            [sys.executable, "-I", "-c", _CHILD],
            input=dumps({"pattern": pattern, "pairs": pairs}),
            capture_output=True,
            timeout=timeout,
            check=True,
        )
    except subprocess.TimeoutExpired as error:
        raise TimeoutError("regular expression took too long") from error
    except subprocess.CalledProcessError as error:
        raise re.error("regular expression could not be applied") from error
    answer = loads(done.stdout)
    if "error" in answer:
        raise re.error(answer["error"])
    return answer["result"]
