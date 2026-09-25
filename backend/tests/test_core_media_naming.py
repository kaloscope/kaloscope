"""The naming field accepts data placeholders, never executable templates."""

from pathlib import PurePosixPath

import pytest

from app.core.media.naming import (
    render_directory,
    render_filename,
    render_path,
    validate_template,
)


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


def test_partial_context():
    template = "{{show_title}}/Season {{season}}/{{episode_code}} - {{title}}"

    directory = render_directory(
        template, {"show_title": "Show", "season": 0}, "tv_show"
    )
    filename = render_filename(
        template, {"title": "Special", "season": 0, "episode": 123}, "tv_show"
    )

    assert directory == PurePosixPath("Show/Season 00")
    assert filename == "S00E123 - Special"


def test_show_originaltitle():
    template = (
        "{{show_originaltitle}}/S{{season}}/{{show_originaltitle}} - {{originaltitle}}"
    )
    context = {
        "show_originaltitle": "Show/A",
        "originaltitle": "Pilot: Part 1",
        "season": 1,
    }

    result = render_path(template, context, "tv_show")

    assert result == PurePosixPath("Show_A/S01/Show_A - Pilot_ Part 1")


@pytest.mark.parametrize("value", [None, "", " "])
def test_missing_show_originaltitle(value):
    context = {"show_title": "Show", "show_originaltitle": value}

    with pytest.raises(ValueError, match="missing NFO field: show_originaltitle"):
        render_path("{{show_originaltitle}}/episode", context, "tv_show")


def test_flat_directory():
    assert render_directory("{{title}}", {}, "movie") == PurePosixPath(".")


def test_literal_metadata():
    context = {"title": "{{year}}/Pilot", "year": 2026}

    result = render_path("{{title}}", context, "movie")

    assert result == PurePosixPath("{{year}}_Pilot")


def test_filename_length():
    title = "影" * 80

    assert render_path("{{title}}", {"title": title}, "movie") == PurePosixPath(title)


@pytest.mark.parametrize(
    "field",
    [
        "show_title",
        "show_originaltitle",
        "show_year",
        "season",
        "episode",
        "episode_code",
    ],
)
@pytest.mark.parametrize("directory", [False, True])
def test_movie_show_fields(field, directory):
    template = "{{" + field + "}}"
    if directory:
        template += "/{{title}}"

    with pytest.raises(ValueError, match=f"unsupported rename placeholder: {field}"):
        validate_template(template, "movie")


@pytest.mark.parametrize(
    "field",
    [
        "show_title",
        "show_originaltitle",
        "show_year",
        "season",
        "episode",
        "episode_code",
    ],
)
def test_show_fields(field):
    placeholder = "{{" + field + "}}"
    template = "{{show_title}}/" + placeholder

    assert validate_template(placeholder) == placeholder
    assert validate_template(template, "tv_show") == template


@pytest.mark.parametrize(
    "field", ["title", "originaltitle", "year", "unique_id", "nfo_source"]
)
def test_movie_fields(field):
    placeholder = "{{" + field + "}}"
    template = f"{placeholder}/{placeholder}"

    assert validate_template(placeholder, "movie") == placeholder
    assert validate_template(template, "movie") == template


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


@pytest.mark.parametrize(
    "context",
    [{}, {"title": "NUL"}, {"title": "x" * 241}, {"title": "影" * 81}],
)
def test_invalid_metadata(context):
    with pytest.raises(ValueError):
        render_path("{{title}}", context, "movie")
