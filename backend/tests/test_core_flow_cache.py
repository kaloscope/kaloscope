"""Unit tests for the flow cache node."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.core.flow.context import Context
from app.core.flow.nodes.general import cache


@pytest.mark.parametrize(
    ("value", "ttl", "values", "expected", "expires"),
    [
        ('{"count": 1}', 10, {}, '{"count": 1}', 110),
        ("{{ payload | tojson }}", 0, {"payload": "123"}, '"123"', None),
    ],
    ids=["json", "template"],
)
def test_value(monkeypatch, value, ttl, values, expected, expires):
    context = Context.__new__(Context)
    context._context = values
    update = AsyncMock()
    monkeypatch.setattr(cache.FlowVariable, "update_or_create", update)
    monkeypatch.setattr(cache.time, "time", lambda: 100)

    asyncio.run(
        cache.CacheNode.execute(
            graph_id=2,
            node_data={"key": "item", "ttl": ttl, "value": value},
            context=context,
        )
    )

    update.assert_awaited_once_with(
        graph_id=2, key="item", defaults={"value": expected, "expires": expires}
    )
