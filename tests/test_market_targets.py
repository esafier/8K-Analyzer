"""Tests for market_targets detection module.

Covers both the new-schema (market_based_targets object) and old-schema
(performance_hurdles free text + stock_price_targets) paths, since the
retrofit needs to handle both correctly.
"""
import json

from market_targets import detect_market_targets, detect_from_json_string


def test_empty_dict_returns_no_targets():
    out = detect_market_targets({})
    assert out["has_any"] is False
    assert out["targets"] == {"stock_price": [], "market_cap": [], "tsr": []}


def test_old_schema_stock_price_targets_field():
    """Pre-split filings have a dedicated stock_price_targets field."""
    structured = {
        "comp_events": [{
            "executive": "Jane Doe (CEO)",
            "grant_type": "PSUs",
            "stock_price_targets": "$150, $200, $250",
            "performance_hurdles": None,
        }]
    }
    out = detect_market_targets(structured)
    assert out["has_any"] is True
    assert len(out["targets"]["stock_price"]) == 1
    assert out["targets"]["stock_price"][0]["value"] == "$150, $200, $250"
    assert out["targets"]["stock_price"][0]["executive"] == "Jane Doe (CEO)"
    assert out["targets"]["market_cap"] == []
    assert out["targets"]["tsr"] == []


def test_old_schema_tsr_in_performance_hurdles():
    """TSR mentioned in free-text performance_hurdles should be detected."""
    structured = {
        "comp_events": [{
            "executive": "John Smith (CFO)",
            "performance_hurdles": "Relative TSR vs. S&P 500 peer group, top quartile required",
            "stock_price_targets": None,
        }]
    }
    out = detect_market_targets(structured)
    assert out["has_any"] is True
    assert len(out["targets"]["tsr"]) == 1
    assert "TSR" in out["targets"]["tsr"][0]["value"]


def test_old_schema_total_shareholder_return_phrase():
    """'total shareholder return' phrase should also fire TSR detection."""
    structured = {
        "comp_events": [{
            "executive": "Dr. K. Nakamura",
            "performance_hurdles": "Annual total shareholder return must exceed 12%",
        }]
    }
    out = detect_market_targets(structured)
    assert out["has_any"] is True
    assert len(out["targets"]["tsr"]) == 1


def test_old_schema_market_cap_in_performance_hurdles():
    structured = {
        "comp_events": [{
            "executive": "CEO Pat Wong",
            "performance_hurdles": "Vests upon reaching $50B market capitalization",
        }]
    }
    out = detect_market_targets(structured)
    assert out["has_any"] is True
    assert len(out["targets"]["market_cap"]) == 1
    assert "market capitalization" in out["targets"]["market_cap"][0]["value"]


def test_operating_only_hurdles_not_flagged():
    """Revenue / EBITDA / EPS hurdles must NOT trigger any market-target flag."""
    structured = {
        "comp_events": [{
            "executive": "CFO",
            "performance_hurdles": "Revenue > $500M and EBITDA margin > 20%",
            "stock_price_targets": None,
        }]
    }
    out = detect_market_targets(structured)
    assert out["has_any"] is False


def test_new_schema_market_based_targets_object():
    """New-schema filings nest the three target types in market_based_targets."""
    structured = {
        "comp_events": [{
            "executive": "Jane Doe (CEO)",
            "market_based_targets": {
                "stock_price": "$200 sustained 60 trading days",
                "market_cap": None,
                "tsr": "Top quartile vs. peer group",
            },
            "operating_hurdles": "Revenue > $1B",
        }]
    }
    out = detect_market_targets(structured)
    assert out["has_any"] is True
    assert len(out["targets"]["stock_price"]) == 1
    assert len(out["targets"]["tsr"]) == 1
    assert out["targets"]["market_cap"] == []


def test_null_string_values_treated_as_absent():
    """LLM sometimes emits the literal string 'null' or 'none' — must not flag."""
    structured = {
        "comp_events": [{
            "executive": "CFO",
            "stock_price_targets": "null",
            "performance_hurdles": "none",
        }]
    }
    out = detect_market_targets(structured)
    assert out["has_any"] is False


def test_multiple_executives_each_listed():
    """Two comp events with TSR — both executives should appear in output."""
    structured = {
        "comp_events": [
            {
                "executive": "CEO Wong",
                "performance_hurdles": "TSR-based vesting",
            },
            {
                "executive": "CFO Smith",
                "performance_hurdles": "TSR vs. peers",
            },
        ]
    }
    out = detect_market_targets(structured)
    assert out["has_any"] is True
    assert len(out["targets"]["tsr"]) == 2


def test_word_boundary_prevents_tsr_false_positive():
    """The substring 'tsr' inside another word (e.g. 'midstrss') must NOT fire."""
    structured = {
        "comp_events": [{
            "executive": "CFO",
            "performance_hurdles": "Free cash flow > $200M (no tsraint actually)",
        }]
    }
    # 'tsraint' contains 'tsr' as substring but not as a whole word — should NOT match
    out = detect_market_targets(structured)
    assert out["has_any"] is False


def test_detect_from_json_string_handles_malformed():
    """Bad JSON input should return empty detection, not raise."""
    out = detect_from_json_string("{ this is not valid json")
    assert out["has_any"] is False
    assert out["targets"] == {"stock_price": [], "market_cap": [], "tsr": []}


def test_detect_from_json_string_handles_none():
    out = detect_from_json_string(None)
    assert out["has_any"] is False


def test_detect_from_json_string_roundtrip_with_real_payload():
    """End-to-end: a realistic JSON blob produces the expected detection."""
    payload = json.dumps({
        "reasoning": "PSU grant with sustained stock price + relative TSR",
        "departures": [],
        "appointments": [],
        "comp_events": [{
            "executive": "Jane Doe (incoming CEO)",
            "grant_type": "PSUs",
            "grant_value": "$8M target",
            "vesting_schedule": "3-year cliff",
            "performance_hurdles": "Relative TSR top tercile",
            "stock_price_targets": "$120, $150, $180",
        }],
        "other": [],
    })
    out = detect_from_json_string(payload)
    assert out["has_any"] is True
    assert len(out["targets"]["stock_price"]) == 1
    assert len(out["targets"]["tsr"]) == 1
    assert out["targets"]["market_cap"] == []
