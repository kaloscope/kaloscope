"""The naming field accepts data placeholders, never executable templates."""

from pathlib import PurePosixPath

import pytest

from app.core.media.naming import render_path, validate_template


def test_placeholders():
    assert validate_template("  ", "movie") is None
    assert render_path(
        "{{show_title}}/{{episode_code}} - {{title}}",
        {"show_title": "Show/A", "title": "Pilot: Part 1", "season": 1, "episode": 2},
        "tv_show",
    ) == PurePosixPath("Show_A/S01E02 - Pilot_ Part 1")
    assert render_path(
        "{{title}} ({{year}})", {"title": "Movie", "year": 2026}, "movie"
    ) == PurePosixPath("Movie (2026)")


@pytest.mark.parametrize(
    ("template", "lib_type"),
    [
        ("../{{title}}", "movie"),
        ("/{{title}}", "movie"),
        ("a//{{title}}", "tv_show"),
        ("{{ title.upper() }}", "movie"),
        ("{{title|safe}}", "movie"),
        ("{{unknown}}", "movie"),
        ("{% if title %}x{% endif %}", "movie"),
        ("{{title}}/{{episode}}", "tv_show"),
        ("{{show_title}}/{{episode_code}}/x", "tv_show"),
        ("{{title}}/a/b", "movie"),
        ("{{title}}", "tv_show"),
        ("C:\\{{title}}", "movie"),
        ("CON", "movie"),
        ("x" * 1025, "movie"),
    ],
)
def test_invalid_template(template, lib_type):
    with pytest.raises(ValueError):
        validate_template(template, lib_type)


@pytest.mark.parametrize("context", [{}, {"title": "NUL"}, {"title": "x" * 241}])
def test_invalid_metadata(context):
    with pytest.raises(ValueError):
        render_path("{{title}}", context, "movie")
