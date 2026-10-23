"""Unit tests for the flow control nodes."""

import asyncio
from typing import Any

import pytest

from app.core.flow.context import Context
from app.core.flow.edge import Source
from app.core.flow.handles import STOP
from app.core.flow.nodes.control.condition import ConditionNode
from app.core.flow.nodes.control.loop import LoopNode
from app.core.flow.nodes.control.merge import MergeNode


def _context(values: dict[str, Any]) -> Context:
    context = Context.__new__(Context)
    context._context = values
    return context


def _condition(expression, values):
    result = asyncio.run(
        ConditionNode.execute(
            node_data={"expression": expression},
            context=_context(values),
        )
    )
    assert result is not None
    return result.id


@pytest.mark.parametrize(
    ("expression", "values", "expected"),
    [
        ("{{ calendar }}", {}, "is_false"),
        ("{{ calendar }}", {"calendar": []}, "is_false"),
        ("{{ nfo_type == 'movie' }}", {"nfo_type": "episode"}, "is_false"),
        ("{{ nfo_type == 'movie' }}", {"nfo_type": "movie"}, "is_true"),
    ],
)
def test_condition(expression, values, expected):
    assert _condition(expression, values) == expected


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ('<div id="content"></div>', "is_false"),
        ('<div id="pagelink"></div>', "is_true"),
        ('<div id="pagelink">1</div>', "is_true"),
        ('<div id="pagelink"><a>2</a></div>', "is_true"),
    ],
)
def test_xpath(html, expected):
    assert (
        _condition(
            "{{ response|xpath('//div[@id=\"pagelink\"]') }}", {"response": html}
        )
        == expected
    )


@pytest.mark.parametrize("values", [[], [1, 2], [{"id": 1}, {"id": 2}]])
def test_loop(values):
    async def run():
        data: dict[str, Any] = {"expression": "{{ data }}", "varname": "item"}
        context = _context({"data": values})
        handle = LoopNode.Handles.input
        items = []
        while True:
            result = await LoopNode.execute(
                node_id="loop", node_data=data, context=context, input_handle=handle
            )
            assert result is not None
            if result.id == "output":
                return items
            items.append(data["$loop"]["item"])
            handle = LoopNode.Handles.loop_continue

    assert asyncio.run(run()) == values


@pytest.mark.parametrize("maximum", [1, 2, 3])
def test_merge(maximum):
    async def run():
        sources = [Source(name, handle_id="output") for name in ("one", "two")]
        data = {"maximum": maximum, "timeout": 0}
        results = [
            await MergeNode.execute(node_data=data, source=source, sources=sources)
            for source in sources
        ]
        repeated = await MergeNode.execute(
            node_data=data, source=sources[0], sources=sources
        )

        expected = (
            [MergeNode.Handles.output, STOP]
            if maximum == 1
            else [STOP, MergeNode.Handles.output]
        )
        assert results == expected
        assert repeated is STOP

    asyncio.run(run())


@pytest.mark.parametrize("field", ["maximum", "timeout"])
def test_merge_empty_field(field):
    with pytest.raises(ValueError, match="must not be empty"):
        asyncio.run(MergeNode.execute(node_data={field: None}))
