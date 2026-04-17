"""Tests for summary_utils helpers."""
import json
from summary_utils import parse_subcategories, serialize_subcategories


class TestParseSubcategories:
    def test_parses_json_array(self):
        raw = '["CFO Departure", "CFO Appointment"]'
        assert parse_subcategories(raw) == ["CFO Departure", "CFO Appointment"]

    def test_single_string_becomes_one_element_list(self):
        raw = "CFO Departure"
        assert parse_subcategories(raw) == ["CFO Departure"]

    def test_none_returns_empty_list(self):
        assert parse_subcategories(None) == []

    def test_empty_string_returns_empty_list(self):
        assert parse_subcategories("") == []

    def test_whitespace_string_returns_empty_list(self):
        assert parse_subcategories("   ") == []

    def test_malformed_json_falls_back_to_single_string(self):
        # If someone wrote bad JSON, treat it as a literal string
        raw = '["unclosed'
        assert parse_subcategories(raw) == ['["unclosed']

    def test_json_non_array_falls_back_to_single_string(self):
        # JSON that parses but isn't a list
        raw = '{"foo": "bar"}'
        assert parse_subcategories(raw) == ['{"foo": "bar"}']


class TestSerializeSubcategories:
    def test_serializes_list_to_json_array(self):
        result = serialize_subcategories(["CFO Departure", "CFO Appointment"])
        assert json.loads(result) == ["CFO Departure", "CFO Appointment"]

    def test_empty_list_returns_none(self):
        # Preserve the ability to store NULL in the DB when nothing to say
        assert serialize_subcategories([]) is None

    def test_none_returns_none(self):
        assert serialize_subcategories(None) is None

    def test_strips_empty_strings_from_list(self):
        result = serialize_subcategories(["CFO Departure", "", None, "CFO Appointment"])
        assert json.loads(result) == ["CFO Departure", "CFO Appointment"]


import json as _json
from summary_utils import structured_summary_for_display


class TestStructuredSummaryForDisplay:
    def test_parses_json_blob(self):
        raw = _json.dumps({
            "departures": [{"name": "J. Smith", "title": "CFO"}],
            "appointments": [], "comp_events": [], "other": [],
            "reasoning": "one event",
        })
        result = structured_summary_for_display(raw)
        assert result["departures"][0]["name"] == "J. Smith"
        assert result["appointments"] == []
        assert result["has_any_event"] is True

    def test_handles_none(self):
        result = structured_summary_for_display(None)
        assert result["departures"] == []
        assert result["has_any_event"] is False

    def test_handles_malformed_json(self):
        result = structured_summary_for_display("{broken")
        assert result["has_any_event"] is False
