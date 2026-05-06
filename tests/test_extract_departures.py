"""Tests for llm.extract_departures — LLM extraction of departures from a 5.02 snippet."""
import json
from unittest.mock import patch, MagicMock


def _mock_response(content):
    """Build a fake OpenAI ChatCompletion response object."""
    mock = MagicMock()
    mock.choices = [MagicMock()]
    mock.choices[0].message.content = content
    mock.usage.prompt_tokens = 100
    mock.usage.completion_tokens = 50
    return mock


def test_returns_list_of_departures_on_valid_json():
    from llm import extract_departures

    fake_json = '[{"date": "2025-09-12", "person": "Jane Doe", "position": "CFO", "reason": "Resigned"}]'
    with patch("llm.OpenAI") as mock_openai_class:
        mock_client = mock_openai_class.return_value
        mock_client.chat.completions.create.return_value = _mock_response(fake_json)

        result = extract_departures("Item 5.02 ... Jane Doe ...", filed_date="2025-09-12")

    assert result["departures"] == [
        {"date": "2025-09-12", "person": "Jane Doe", "position": "CFO", "reason": "Resigned"}
    ]
    assert result["error"] is False


def test_returns_empty_list_when_llm_returns_empty_array():
    from llm import extract_departures

    with patch("llm.OpenAI") as mock_openai_class:
        mock_client = mock_openai_class.return_value
        mock_client.chat.completions.create.return_value = _mock_response("[]")
        result = extract_departures("some text", filed_date="2025-01-01")

    assert result["departures"] == []
    assert result["error"] is False


def test_strips_markdown_code_fence_if_present():
    """Some models stubbornly wrap JSON in ```json fences. Tolerate that."""
    from llm import extract_departures

    wrapped = '```json\n[{"date": "2025-01-01", "person": "X", "position": "Y", "reason": "Z"}]\n```'
    with patch("llm.OpenAI") as mock_openai_class:
        mock_client = mock_openai_class.return_value
        mock_client.chat.completions.create.return_value = _mock_response(wrapped)
        result = extract_departures("text", filed_date="2025-01-01")

    assert len(result["departures"]) == 1
    assert result["departures"][0]["person"] == "X"


def test_marks_error_when_json_parse_fails():
    from llm import extract_departures

    with patch("llm.OpenAI") as mock_openai_class:
        mock_client = mock_openai_class.return_value
        mock_client.chat.completions.create.return_value = _mock_response("this is not json")
        result = extract_departures("text", filed_date="2025-01-01")

    assert result["departures"] == []
    assert result["error"] is True


def test_marks_error_when_api_call_raises():
    from llm import extract_departures

    with patch("llm.OpenAI") as mock_openai_class:
        mock_client = mock_openai_class.return_value
        mock_client.chat.completions.create.side_effect = RuntimeError("network down")
        result = extract_departures("text", filed_date="2025-01-01")

    assert result["departures"] == []
    assert result["error"] is True
