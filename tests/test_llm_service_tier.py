"""Flex processing: half price, with a fallback that never loses a filing.

Flex is billed at 50% of standard in exchange for slower answers and an
occasional "busy" (429 Resource Unavailable, not charged). Some models don't
offer it at all (400 Unsupported service_tier). Every one of those must end
in a standard-tier answer — and an empty account must still stop the run
rather than be retried as if it were busy.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx2 as httpx
import openai
import pytest

import llm


def _error(cls, status, message, code=None):
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    return cls(message, response=httpx.Response(status, request=request),
               body={"message": message, "code": code})


BUSY = _error(openai.RateLimitError, 429, "Resource Unavailable", "resource_unavailable")
NO_FLEX = _error(openai.BadRequestError, 400, "Unsupported service_tier: flex")
NO_TEMPERATURE = _error(openai.BadRequestError, 400,
                        "Unsupported parameter: 'temperature' is not supported with this model.")
EMPTY = _error(openai.RateLimitError, 429, "Your prepaid credit balance is exhausted.",
               "credit_balance_exhausted")


def _ok(tier="flex"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"relevant": true}'))],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=10),
        service_tier=tier,
    )


@pytest.fixture(autouse=True)
def _flex(monkeypatch):
    monkeypatch.setattr(llm, "LLM_SERVICE_TIER", "flex")
    llm._FLEX_UNSUPPORTED.clear()
    llm._NO_TEMPERATURE.clear()
    llm._FLEX_FAILURES.clear()
    yield
    llm._FLEX_UNSUPPORTED.clear()
    llm._NO_TEMPERATURE.clear()
    llm._FLEX_FAILURES.clear()


def _run(*outcomes, model="test-model", clients=None):
    client = MagicMock()
    client.chat.completions.create.side_effect = list(outcomes)

    def make(timeout=None, max_retries=None):
        if clients is not None:
            clients.append({"timeout": timeout, "max_retries": max_retries})
        return client

    with patch("llm._client", side_effect=make):
        result = llm.classify_and_summarize("filing text", model=model)
    return result, client.chat.completions.create.call_args_list


def test_requests_go_out_on_the_flex_tier():
    result, calls = _run(_ok())
    assert calls[0].kwargs["service_tier"] == "flex"
    assert result["_service_tier"] == "flex"


def test_a_busy_flex_tier_falls_back_to_standard_for_that_call():
    result, calls = _run(BUSY, _ok("default"))
    assert result["relevant"] is True
    assert "service_tier" not in calls[1].kwargs
    assert "test-model" not in llm._FLEX_UNSUPPORTED   # busy once is not "unsupported"


def test_a_model_without_flex_is_remembered_for_the_run():
    _run(NO_FLEX, _ok("default"))
    _, calls = _run(_ok("default"))
    assert "service_tier" not in calls[0].kwargs      # no wasted flex attempt the second time


def test_a_model_that_rejects_temperature_is_retried_without_it():
    _, calls = _run(NO_TEMPERATURE, _ok(), model="gpt-6-luna")
    assert calls[0].kwargs.get("temperature") == 0
    assert "temperature" not in calls[1].kwargs
    _, calls = _run(_ok(), model="gpt-6-luna")
    assert "temperature" not in calls[0].kwargs


def test_an_empty_account_is_never_retried_as_busy():
    """Both arrive as 429. Retrying an empty account at standard would just
    fail again — and hide the reason."""
    with pytest.raises(llm.OutOfCredits):
        _run(EMPTY, _ok("default"))


def test_standard_tier_when_flex_is_switched_off(monkeypatch):
    monkeypatch.setattr(llm, "LLM_SERVICE_TIER", "")
    _, calls = _run(_ok("default"))
    assert "service_tier" not in calls[0].kwargs


def test_an_ordinary_error_still_costs_only_that_filing():
    bad = _error(openai.BadRequestError, 400, "context_length_exceeded")
    result, _ = _run(bad)
    assert result is None


def test_a_flex_attempt_has_a_short_timeout_and_no_sdk_retries():
    """A 600s timeout times four SDK attempts stalled a re-score for 40
    minutes a call before the standard fallback could run."""
    clients = []
    _run(_ok(), clients=clients)
    assert clients[0] == {"timeout": llm.OPENAI_FLEX_TIMEOUT_SECONDS, "max_retries": 0}
    assert llm.OPENAI_FLEX_TIMEOUT_SECONDS <= 120


def test_repeated_flex_failures_switch_the_model_to_standard_for_the_run():
    timeout = openai.APITimeoutError(request=httpx.Request("POST", "https://api.openai.com"))
    for _ in range(llm.FLEX_FAILURES_BEFORE_STANDARD):
        _run(timeout, _ok("default"))
    assert "test-model" in llm._FLEX_UNSUPPORTED
    _, calls = _run(_ok("default"))
    assert "service_tier" not in calls[0].kwargs
