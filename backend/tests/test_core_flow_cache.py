"""Unit tests for the flow cache node."""

import asyncio
from types import SimpleNamespace
from typing import Any

from app.core.flow.context import Context
from app.core.flow.nodes.general import cache


def _context(values: dict[str, Any] | None = None) -> Context:
    context = Context.__new__(Context)
    context._context = values or {}
    return context


def test_json_value(monkeypatch):
    calls = {}

    class FakeFlowVariable:
        @classmethod
        async def update_or_create(cls, *, defaults, **filters):
            calls["defaults"] = defaults
            calls["filters"] = filters
            return SimpleNamespace(id=7), True

    monkeypatch.setattr(cache, "FlowVariable", FakeFlowVariable)
    monkeypatch.setattr(cache.time, "time", lambda: 100)

    asyncio.run(
        cache.CacheNode.execute(
            graph_id=2,
            node_data={"key": "item", "ttl": 10, "value": '{"count": 1}'},
            context=_context(),
        )
    )

    assert calls == {
        "defaults": {"value": '{"count": 1}', "expires": 110},
        "filters": {"graph_id": 2, "key": "item"},
    }


def test_template_value(monkeypatch):
    calls = {}

    class FakeFlowVariable:
        @classmethod
        async def update_or_create(cls, *, defaults, **filters):
            calls["defaults"] = defaults
            calls["filters"] = filters
            return SimpleNamespace(id=7), False

    monkeypatch.setattr(cache, "FlowVariable", FakeFlowVariable)
    monkeypatch.setattr(cache.time, "time", lambda: 100)

    asyncio.run(
        cache.CacheNode.execute(
            graph_id=2,
            node_data={
                "key": "item",
                "ttl": 0,
                "value": "{{ payload | tojson }}",
            },
            context=_context({"payload": "123"}),
        )
    )

    assert calls == {
        "defaults": {"value": '"123"', "expires": None},
        "filters": {"graph_id": 2, "key": "item"},
    }
