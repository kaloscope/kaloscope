import traceback

import autopep8
from pydantic import BaseModel, Field
from sanic import Blueprint, HTTPResponse, text
from sanic_ext import validate

from app.core.decorators import authorize
from app.core.renderer import render
from app.models.user import UserRole
from app.utils import json

code = Blueprint("code", url_prefix="/code")


class FormatOptions(BaseModel):
    """Options for formatting Python code."""

    indent_size: int = Field(ge=1, le=4, default=4)
    max_line_length: int = Field(ge=79, default=88)


class FormatRequest(BaseModel):
    """Request model for formatting Python code."""

    source: str
    options: FormatOptions | None = None


@code.post("/format")
@validate(json=FormatRequest)
async def format(_, body: FormatRequest) -> HTTPResponse:
    """Format given Python code using autopep8.

    Args:
        _: Unused request.
        body: Python source and formatting options.

    Returns:
        Response containing the requested data.
    """
    source = body.source
    options = body.options.model_dump() if body.options else {}
    return text(autopep8.fix_code(source, options))


class EvalRequest(BaseModel):
    """Request model for evaluating Jinja2 templates."""

    template: str
    document: str
    doc_name: str | None = None


@code.post("/evaluate")
@authorize(role=UserRole.ADMIN)
@validate(json=EvalRequest)
async def evaluate(_, body: EvalRequest) -> HTTPResponse:
    """Evaluate the given Jinja2 template with the provided document.

    The preview uses the unsandboxed workflow environment, so only administrators,
    who can already author workflow scripts, may call it.

    Args:
        _: Unused request.
        body: Template, source document and context name.

    Returns:
        Response containing the requested data.
    """
    try:
        document = body.document
        if doc_name := body.doc_name:
            # if a document name is provided, use it as the key in the context
            context = {}
            for key in reversed(doc_name.split(".")):
                context = {key: context if context else document}
        else:
            # otherwise, parse the document as JSON and use it as the context
            context = json.loads(document)
            if not isinstance(context, dict):
                raise ValueError("document must be a valid JSON object")

        # render the template with the context
        result = render(body.template, context)
        if json_result := json.try_loads(result, with_comments=True):
            return text(json.pretty(json_result))
        return text(result)
    except Exception:
        # if an error occurs during evaluation, return the traceback
        return text(traceback.format_exc())
