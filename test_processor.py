"""Unit tests for processor.py — pure logic functions (no network calls)."""

import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from processor import (
    build_condensed_transcript,
    extract_video_id,
    fmt_ts,
    generate_raw_transcript_html,
    group_chapters,
    insert_transcript_dropdowns,
    parse_chapter_group_output,
)


# ============================================================
# fmt_ts
# ============================================================

class TestFmtTs:
    def test_zero_seconds(self):
        assert fmt_ts(0) == "00:00"

    def test_seconds_only(self):
        assert fmt_ts(45) == "00:45"

    def test_minutes_and_seconds(self):
        assert fmt_ts(125) == "02:05"

    def test_exactly_one_hour(self):
        assert fmt_ts(3600) == "1:00:00"

    def test_hours_minutes_seconds(self):
        assert fmt_ts(3661) == "1:01:01"

    def test_large_value(self):
        assert fmt_ts(7384) == "2:03:04"

    def test_float_input_truncated(self):
        assert fmt_ts(90.7) == "01:30"

    def test_negative_wraps(self):
        # Negative values aren't expected but shouldn't crash
        result = fmt_ts(-1)
        assert isinstance(result, str)


# ============================================================
# extract_video_id
# ============================================================

class TestExtractVideoId:
    def test_standard_url(self):
        assert extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_short_url(self):
        assert extract_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_embed_url(self):
        assert extract_video_id("https://www.youtube.com/v/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_bare_id(self):
        assert extract_video_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_url_with_extra_params(self):
        assert extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=120s") == "dQw4w9WgXcQ"

    def test_invalid_url_returns_none(self):
        assert extract_video_id("https://www.google.com") is None

    def test_empty_string(self):
        assert extract_video_id("") is None

    def test_id_with_hyphens_and_underscores(self):
        assert extract_video_id("PM9rgDBX9u0") == "PM9rgDBX9u0"


# ============================================================
# build_condensed_transcript
# ============================================================

class TestBuildCondensedTranscript:
    def test_single_segment(self):
        segments = [{"start": 0, "duration": 5, "text": "Hello world"}]
        result = build_condensed_transcript(segments)
        assert len(result) == 1
        assert "[00:00]" in result[0]
        assert "Hello world" in result[0]

    def test_groups_within_30s(self):
        segments = [
            {"start": 0, "duration": 5, "text": "First"},
            {"start": 10, "duration": 5, "text": "Second"},
            {"start": 20, "duration": 5, "text": "Third"},
        ]
        result = build_condensed_transcript(segments)
        assert len(result) == 1
        assert "First" in result[0] and "Second" in result[0] and "Third" in result[0]

    def test_splits_at_30s_boundary(self):
        segments = [
            {"start": 0, "duration": 5, "text": "Block one"},
            {"start": 35, "duration": 5, "text": "Block two"},
        ]
        result = build_condensed_transcript(segments)
        assert len(result) == 2
        assert "Block one" in result[0]
        assert "Block two" in result[1]
        assert "[00:00]" in result[0]
        assert "[00:35]" in result[1]

    def test_timestamp_format(self):
        segments = [{"start": 605, "duration": 5, "text": "Ten minutes in"}]
        result = build_condensed_transcript(segments)
        assert "[10:05]" in result[0]


# ============================================================
# group_chapters
# ============================================================

class TestGroupChapters:
    def test_empty_chapters(self):
        assert group_chapters([], 600) == []

    def test_single_chapter(self):
        chapters = [{"start": 0, "title": "Intro"}]
        result = group_chapters(chapters, 300)
        assert len(result) == 1
        assert len(result[0]) == 1

    def test_chapters_within_target(self):
        chapters = [
            {"start": 0, "title": "Part 1"},
            {"start": 60, "title": "Part 2"},
            {"start": 120, "title": "Part 3"},
        ]
        result = group_chapters(chapters, 300, target_minutes=8)
        # All 3 chapters fit within 5 minutes (300s), under 8-min target
        assert len(result) == 1

    def test_chapters_split_across_groups(self):
        chapters = [
            {"start": 0, "title": "Part 1"},
            {"start": 600, "title": "Part 2"},
            {"start": 1200, "title": "Part 3"},
        ]
        result = group_chapters(chapters, 1800, target_minutes=8)
        # Each chapter is ~10 min, so they should split
        assert len(result) >= 2

    def test_all_chapters_accounted_for(self):
        chapters = [
            {"start": 0, "title": "A"},
            {"start": 300, "title": "B"},
            {"start": 900, "title": "C"},
            {"start": 1500, "title": "D"},
        ]
        result = group_chapters(chapters, 2000, target_minutes=8)
        total = sum(len(g) for g in result)
        assert total == 4


# ============================================================
# parse_chapter_group_output
# ============================================================

class TestParseChapterGroupOutput:
    def test_basic_parsing(self):
        content = """<!-- CHAPTER: Introduction -->
<!-- SUMMARY -->
<ul><li>Key point 1</li></ul>
<!-- /SUMMARY -->
<!-- TRANSCRIPT -->
<span id="t0" class="ts-anchor"></span><p>Hello everyone.</p>
<!-- /TRANSCRIPT -->"""

        group = [{"title": "Introduction", "start": 0, "end": 120}]
        result = parse_chapter_group_output(content, group, "test123")
        assert len(result) == 1
        assert result[0]["title"] == "Introduction"
        assert "Key point 1" in result[0]["summary"]

    def test_multiple_chapters(self):
        content = """<!-- CHAPTER: Intro -->
<!-- SUMMARY -->
<ul><li>Point A</li></ul>
<!-- /SUMMARY -->
<!-- TRANSCRIPT -->
<span id="t0" class="ts-anchor"></span><p>First part.</p>
<!-- /TRANSCRIPT -->
<!-- CHAPTER: Main -->
<!-- SUMMARY -->
<ul><li>Point B</li></ul>
<!-- /SUMMARY -->
<!-- TRANSCRIPT -->
<span id="t120" class="ts-anchor"></span><p>Second part.</p>
<!-- /TRANSCRIPT -->"""

        group = [
            {"title": "Intro", "start": 0, "end": 120},
            {"title": "Main", "start": 120, "end": 300},
        ]
        result = parse_chapter_group_output(content, group, "test123")
        assert len(result) == 2
        assert result[0]["title"] == "Intro"
        assert result[1]["title"] == "Main"

    def test_missing_chapters_filled(self):
        content = """<!-- CHAPTER: Only One -->
<!-- SUMMARY -->
<ul><li>Solo</li></ul>
<!-- /SUMMARY -->
<!-- TRANSCRIPT -->
text
<!-- /TRANSCRIPT -->"""

        group = [
            {"title": "Only One", "start": 0, "end": 60},
            {"title": "Missing", "start": 60, "end": 120},
        ]
        result = parse_chapter_group_output(content, group, "test123")
        assert len(result) == 2
        assert result[1]["title"] == "Missing"
        assert result[1]["summary"] == ""


# ============================================================
# generate_raw_transcript_html
# ============================================================

class TestGenerateRawTranscriptHtml:
    def test_basic_output(self):
        segments = [
            {"start": 0, "duration": 5, "text": "Hello"},
            {"start": 5, "duration": 5, "text": "World"},
        ]
        result = generate_raw_transcript_html("test123", segments)
        assert '<p id="t0">' in result
        assert "Hello" in result
        assert "World" in result
        assert "test123" in result  # video_id in links

    def test_30s_block_grouping(self):
        segments = [
            {"start": 0, "duration": 5, "text": "A"},
            {"start": 10, "duration": 5, "text": "B"},
            {"start": 40, "duration": 5, "text": "C"},
        ]
        result = generate_raw_transcript_html("test123", segments)
        # First two segments should be in one <p>, third in another
        assert result.count("<p id=") == 2

    def test_chapter_headers_inserted(self):
        segments = [
            {"start": 0, "duration": 5, "text": "Before"},
            {"start": 60, "duration": 5, "text": "After"},
        ]
        chapters = [{"start": 30, "title": "Chapter One"}]
        result = generate_raw_transcript_html("test123", segments, chapters)
        assert "Chapter One" in result
        assert '<h3 class="chapter">' in result

    def test_timestamp_links(self):
        segments = [{"start": 125, "duration": 5, "text": "Test"}]
        result = generate_raw_transcript_html("test123", segments)
        assert "t=125s" in result
        assert "[02:05]" in result


# ============================================================
# insert_transcript_dropdowns
# ============================================================

class TestInsertTranscriptDropdowns:
    def test_dropdowns_inserted(self):
        summary = '<h1>Title</h1>\n<h2>Section <a href="https://www.youtube.com/watch?v=test&t=0s">[0:00]</a></h2>\n<p>Content</p>'
        chapter_data = [{
            "start": 0,
            "end": 120,
            "edited_paragraphs": ["<p>Edited text</p>"],
        }]
        segments = [{"start": 0, "duration": 5, "text": "Raw text here"}]

        result = insert_transcript_dropdowns(summary, chapter_data, segments, "test")
        assert "Transcript excerpt - edited" in result
        assert "Transcript excerpt - raw" in result
        assert "Raw text here" in result

    def test_no_chapters_no_crash(self):
        summary = '<h1>Title</h1>\n<p>No sections here</p>'
        result = insert_transcript_dropdowns(summary, [], [], "test")
        assert "Title" in result
