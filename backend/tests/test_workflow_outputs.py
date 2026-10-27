"""Tests for indexer/movie output checks and isolated authoring recipes."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.core.flow.context import MANUAL_KEY, RETVAL_KEY
from app.core.flow.nodes.end.details import DetailsEndNode
from app.core.flow.nodes.end.search import SearchEndNode
from app.core.flow.nodes.general import http
from app.core.flow.nodes.metadata.movie import MovieNode
from app.core.flow.reference import get_workflow_reference
from app.core.media import shelver
from app.core.media.shelver import TEMPLATES_PATH, render_nfo
from app.models.workflow_authoring import HTTPProbe, TemplateValidation
from app.services import workflow_authoring as authoring
from app.utils.json import dumps, loads_jsonc
from tests.test_flow_reference import context

JSON_FORMATTER = '{"payload": {{ response.text|tojson }}}'
HTML_FORMATTER = '{"html": {{ response.text|tojson }}}'
SEARCH_TEMPLATE = """{% set page = payload|jsonpath_first('$') %}
{"total": {{ page.count|tojson }}, "items": [
{% for item in page.results %}
{"id": {{ item.id|string|tojson }}, "title": {{ item.name|tojson }},
 "link": {{ item.download|tojson }}}{% if not loop.last %},{% endif %}
{% endfor %}]}"""
DETAILS_TEMPLATE = """{% set item = payload|jsonpath_first('$.resource') %}
{% if item %}{"id": {{ item.id|string|tojson }}, "title": {{ item.name|tojson }},
"media_type": "video", "url": {{ item.playback|tojson }}}
{% else %}null{% endif %}"""
MOVIE_TEMPLATE = """[
{% for card in html|xpath_all('//article[@class="movie"]') %}
{% set year = card|xpath_first('.//time/@data-year') %}
{"title": {{ card|xpath_first('.//h2/text()')|tojson }},
"id": {{ card|xpath_first('./@data-id')|tojson }}, "site": "example.test",
"genres": {{ card|xpath_all('.//span[@class="genre"]/text()')|tojson }}
{% if year %}, "year": {{ year|int }}{% endif %}}
{% if not loop.last %},{% endif %}
{% endfor %}]"""
MOVIE_HTML = (
    '<article class="movie" data-id="m1"><h2>Lantern &amp; Rain</h2>'
    '<time data-year="2024"/><span class="genre">Mystery</span></article>'
)


def search_page(number):
    """Return distinct synthetic pages with a stable remote total.

    Args:
        number: One-based page, each containing at most one result.

    Returns:
        Remote JSON payload including an empty out-of-range page.
    """
    return {
        "count": 2,
        "results": [
            {
                "id": number,
                "name": f"Lantern {number}",
                "download": f"https://example.test/{number}.torrent",
            }
        ]
        if number <= 2
        else [],
    }


def check(value, mode, **fixtures):
    """Validate a literal output with the public template-checking service.

    Args:
        value: JSON-compatible business result.
        mode: Output contract.
        **fixtures: Explicit context values used for boundary checks.

    Returns:
        Shared validation result without mutating the supplied fixture.
    """
    return authoring.validate_workflow_template(
        TemplateValidation(
            template="{{ result|tojson }}",
            context={"result": value, **fixtures},
            mode=mode,
        )
    )


@pytest.mark.parametrize(
    ("value", "mode", "fixtures", "path"),
    [
        ({"items": [{}]}, "search", {}, "items.0.id"),
        ({"items": [{"id": 1, "title": "One"}]}, "search", {}, "items.0.id"),
        ({"items": [{"id": "a", "title": " "}]}, "search", {}, "items.0.title"),
        ({"items": [{"id": "a", "title": "A"}] * 2}, "search", {}, "items"),
        ({"items": [], "total": True}, "search", {}, "total"),
        (
            {"items": [{"id": "a", "title": "A"}], "total": 1},
            "search",
            {"page_num": 2, "page_size": 1},
            "total",
        ),
        (
            {"items": [{"id": "a", "title": "A"}], "totalPages": 1},
            "search",
            {"page_num": 2, "page_size": 1},
            "totalPages",
        ),
        ({"items": []}, "search", {"page_size": False}, "context.page_size"),
        ({"media_type": "video", "url": 42}, "details", {}, "url"),
        ({"media_type": "video"}, "details", {}, "$"),
        ({"media_type": "text", "text": [1]}, "details", {}, "text.list[str].0"),
        (
            {"media_type": "image", "images": ["a", "b"], "image_count": 1},
            "details",
            {},
            "image_count",
        ),
        ({"media_type": "video", "chapters": [{}]}, "details", {}, "chapters[0]"),
        ([{"title": "A", "genres": "Mystery"}], "movie", {}, "0.genres"),
        ([{"title": "A", "year": "2024"}], "movie", {}, "0.year"),
        ([{"title": "A", "rating": 11}], "movie", {}, "0.rating"),
        ([{"title": "A", "premiered": "2024-02-30"}], "movie", {}, "0.premiered"),
        ([{"title": "A", "actors": [{"name": 1}]}], "movie", {}, "0.actors.0.name"),
        ([{"title": "\u0001"}], "movie", {}, "[0]"),
        ([{"title": "A" * 300}], "movie", {}, "[0]"),
    ],
)
def test_output_errors(value, mode, fixtures, path):
    original = dumps(value)
    result = check(value, mode, **fixtures)

    assert not result.valid, result
    assert path in [issue.path for issue in result.issues]
    assert "validate" in result.error
    assert dumps(value) == original
    assert result.value == value
    assert result.rendered is None


@pytest.mark.parametrize(
    ("value", "mode", "fixtures"),
    [
        ({"total": 0, "items": []}, "search", {}),
        ({"total": 2, "items": []}, "search", {"page_num": 3, "page_size": 1}),
        ({"totalPages": 2, "items": [{"id": "a", "title": "A"}]}, "search", {}),
        ({"items": []}, "search", {}),
        (None, "details", {}),
        ({"url": "https://example.test/video.mp4"}, "details", {"media_type": "video"}),
        ({"media_type": "text", "text": ["First", "Second"]}, "details", {}),
        (
            {"media_type": "image", "images": ["https://example.test/a.jpg"]},
            "details",
            {},
        ),
        (
            {"media_type": "video", "chapters": [{"id": "ch1", "title": "First"}]},
            "details",
            {},
        ),
        (
            {
                "media_type": "video",
                "url": "a",
                "definitions": [{"url": "b", "definition": "HD"}],
                "danmakus": [{"text": "Hi", "start": 0.5}],
            },
            "details",
            {},
        ),
        ([], "movie", {}),
        ([{"title": "A & B", "year": 2024, "rating": 0}], "movie", {}),
    ],
)
def test_output_values(value, mode, fixtures):
    result = check(value, mode, **fixtures)

    assert result.valid, result
    assert result.value == value
    assert result.warnings


def test_shared_examples():
    for mode, filename in (
        ("search", "resp/search.jsonc"),
        ("details", "resp/details.jsonc"),
        ("movie", "nfo/movie.jsonc"),
    ):
        text = (Path(__file__).parents[1] / "static/flow" / filename).read_text()
        assert check(loads_jsonc(text), mode).valid
    movie = check([{"title": "A", "poster_url": "https://example.test/a.jpg"}], "movie")
    assert any("Unknown" in warning for warning in movie.warnings)


@pytest.mark.parametrize(
    ("total", "number", "valid"),
    [(2, 1, False), (2, 2, False), (2, 3, True), (0, 1, True)],
)
def test_empty_search_page(total, number, valid):
    result = check(
        {"total": total, "items": []}, "search", page_num=number, page_size=1
    )

    assert result.valid is valid
    if not valid:
        assert result.issues[0].path == "items"


@pytest.mark.parametrize(
    ("pages", "number", "valid"),
    [(3, 1, False), (3, 3, False), (3, 4, True), (1, 1, True), (0, 1, True)],
)
def test_empty_counted_page(pages, number, valid):
    result = check(
        {"totalPages": pages, "items": []}, "search", page_num=number, page_size=1
    )

    assert result.valid is valid
    if not valid:
        assert result.issues[0].path == "items"


def test_blank_image_addresses():
    result = check(
        {
            "media_type": "image",
            "images": ["", "  ", "https://example.test/one.jpg"],
            "image_count": 1,
        },
        "details",
    )

    assert result.valid, result
    assert result.value["image_count"] == 1


@pytest.mark.parametrize("media_type", [{}, [], 1])
def test_invalid_media_default(media_type):
    result = check(
        {"url": "https://example.test/one.mp4"}, "details", media_type=media_type
    )

    assert not result.valid
    assert any(issue.path == "media_type" for issue in result.issues)


def test_business_result_limits():
    result = check({"items": [{}] * 20}, "search")
    assert not result.valid
    assert len(result.issues) == 12
    assert any("omitted" in warning for warning in result.warnings)
    assert len(dumps(result.model_dump())) <= authoring.MAX_RENDER_BYTES

    oversized = check([{"title": "Film", "plot": "x" * 9000}], "movie")
    assert not oversized.valid
    assert "exceeds 8000" in oversized.error
    assert oversized.value is None


@pytest.mark.parametrize("page", [1, 2, 3])
def test_search_recipe(monkeypatch, page):
    async def run():
        payload = search_page(page)
        fixture = {"page_num": page, "page_size": 1, "keyword": "灯 & rain"}
        calls = []

        def endpoint(request):
            calls.append(request)
            assert request.url.params["q"] == fixture["keyword"]
            assert request.url.params["page"] == str(page)
            return httpx.Response(200, json=payload)

        node_data = {
            "url": "https://example.test/search?q={{ keyword|quote('') }}"
            "&page={{ page_num }}&size={{ page_size }}",
            "formatter": JSON_FORMATTER,
        }
        monkeypatch.setattr(
            authoring, "NetworkTransport", lambda **_: httpx.MockTransport(endpoint)
        )
        probe = await authoring.probe_http_request(
            HTTPProbe(
                request={"url": node_data["url"]},
                context=fixture,
                formatter=JSON_FORMATTER,
            ),
            SimpleNamespace(role="admin"),
        )
        assert probe.formatted.valid
        validation = authoring.validate_workflow_template(
            TemplateValidation(
                template=SEARCH_TEMPLATE,
                context={**fixture, **probe.formatted.value},
                mode="search",
            )
        )
        assert validation.valid, validation
        ctx = context(fixture)
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
            monkeypatch.setattr(
                http.Sanic,
                "get_app",
                lambda: SimpleNamespace(ctx=SimpleNamespace(httpx=client)),
            )
            await http.HTTPNode.execute(node_data=node_data, context=ctx)
        await SearchEndNode.execute(
            node_data={"response": SEARCH_TEMPLATE}, context=ctx
        )
        assert ctx[RETVAL_KEY] == validation.value
        assert validation.value["total"] == 2
        assert [item["id"] for item in validation.value["items"]] == (
            [str(page)] if page <= 2 else []
        )
        assert len(calls) == 2

    asyncio.run(run())


@pytest.mark.parametrize("found", [True, False])
def test_details_recipe(found):
    async def run():
        fixture = {
            "payload": dumps(
                {
                    "resource": {
                        "id": 1,
                        "name": "Lantern 1",
                        "playback": "https://example.test/1.mp4",
                    }
                    if found
                    else None
                }
            ).decode()
        }
        result = authoring.validate_workflow_template(
            TemplateValidation(
                template=DETAILS_TEMPLATE, context=fixture, mode="details"
            )
        )
        ctx = context(fixture)
        await DetailsEndNode.execute(
            node_data={"response": DETAILS_TEMPLATE}, context=ctx
        )
        assert result.valid
        assert ctx[RETVAL_KEY] == result.value
        if found:
            assert result.value["url"].endswith(".mp4")

    asyncio.run(run())


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        (
            MOVIE_HTML,
            [
                {
                    "title": "Lantern & Rain",
                    "id": "m1",
                    "site": "example.test",
                    "genres": ["Mystery"],
                    "year": 2024,
                }
            ],
        ),
        (
            MOVIE_HTML.replace('<time data-year="2024"/>', ""),
            [
                {
                    "title": "Lantern & Rain",
                    "id": "m1",
                    "site": "example.test",
                    "genres": ["Mystery"],
                }
            ],
        ),
        ("<html><p>No match</p></html>", []),
    ],
)
def test_movie_recipe(monkeypatch, html, expected):
    write = AsyncMock(
        side_effect=AssertionError("Fixture validation must not write NFOs")
    )
    monkeypatch.setattr(shelver, "gen_nfo", write)

    async def run():
        fixture = {"html": html, MANUAL_KEY: True}
        result = authoring.validate_workflow_template(
            TemplateValidation(template=MOVIE_TEMPLATE, context=fixture, mode="movie")
        )
        ctx = context(fixture)
        await MovieNode.execute(node_data={"response": MOVIE_TEMPLATE}, context=ctx)
        assert result.valid, result
        assert ctx[RETVAL_KEY] == result.value == expected
        if result.value:
            xml = render_nfo(
                "movie",
                (TEMPLATES_PATH / "movie.nfo").read_text(encoding="utf-8-sig"),
                result.value[0],
            )
            assert "Lantern &amp; Rain" in xml
        write.assert_not_awaited()

    asyncio.run(run())


def test_movie_metadata(monkeypatch):
    html = """<article><h2>Lantern &amp; Rain</h2><p>A &lt; B</p>
    <span class="rating">8.5</span><time datetime="2024-02-29"/>
    <img class="poster" src="https://example.test/poster.jpg"/>
    <span class="actor" data-role="Lead">Alex Chen</span></article>"""
    template = """[{
    "title": {{ html|xpath_first('//h2/text()')|tojson }},
    "plot": {{ html|xpath_first('//p/text()')|tojson }},
    "rating": {{ html|xpath_first('//span[@class="rating"]/text()')|float }},
    "premiered": {{ html|xpath_first('//time/@datetime')|tojson }},
    "poster": {{ html|xpath_first('//img[@class="poster"]/@src')|tojson }},
    "actors": [{"name": {{ html|xpath_first('//span[@class="actor"]/text()')|tojson }},
    "role": {{ html|xpath_first('//span[@class="actor"]/@data-role')|tojson }}}]
    }]"""
    write = AsyncMock(side_effect=AssertionError("Must not write metadata"))
    monkeypatch.setattr(shelver, "gen_nfo", write)

    async def run():
        fixture = {"html": html, MANUAL_KEY: True}
        result = authoring.validate_workflow_template(
            TemplateValidation(template=template, context=fixture, mode="movie")
        )
        ctx = context(fixture)
        await MovieNode.execute(node_data={"response": template}, context=ctx)

        assert result.valid, result
        assert (
            ctx[RETVAL_KEY]
            == result.value
            == [
                {
                    "title": "Lantern & Rain",
                    "plot": "A < B",
                    "rating": 8.5,
                    "premiered": "2024-02-29",
                    "poster": "https://example.test/poster.jpg",
                    "actors": [{"name": "Alex Chen", "role": "Lead"}],
                }
            ]
        )
        xml = render_nfo(
            "movie",
            (TEMPLATES_PATH / "movie.nfo").read_text(encoding="utf-8-sig"),
            result.value[0],
        )
        assert "<plot>A &lt; B</plot>" in xml
        assert "<rating>8.5</rating>" in xml
        assert "<premiered>2024-02-29</premiered>" in xml
        assert "<poster>https://example.test/poster.jpg</poster>" in xml
        assert "<name>Alex Chen</name>" in xml
        assert "<role>Lead</role>" in xml
        write.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize(
    "topic", ["json_indexer", "html_indexer", "indexer_auth", "movie_scraper"]
)
def test_recipe_guidance(topic):
    guide = get_workflow_reference(topic).content
    assert guide.isascii()
    assert "twice" in guide
    assert "Keep/Revert" in guide
    assert "mode=" in guide
