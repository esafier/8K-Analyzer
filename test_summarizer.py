# test_summarizer.py — Tests for the fallback summarizer and cover page stripping
#
# Run: python -m pytest test_summarizer.py -v
#
# These tests verify that SEC boilerplate (especially the "emerging growth company"
# cover page text) never leaks into summaries.

import pytest
from summarizer import extract_summary
from fetcher import strip_cover_page


# --- Realistic SEC 8-K filing text for testing ---
# This mimics the actual structure: cover page boilerplate first, then real content.

COVER_PAGE_BOILERPLATE = (
    "UNITED STATES SECURITIES AND EXCHANGE COMMISSION Washington, D.C. 20549 "
    "FORM 8-K CURRENT REPORT Pursuant to Section 13 or 15(d) of the Securities "
    "Exchange Act of 1934 Date of Report (Date of earliest event reported): "
    "March 15, 2026 ACME CORP (Exact name of registrant as specified in its charter) "
    "Delaware 001-12345 12-3456789 (State or other jurisdiction of incorporation) "
    "(Commission File Number) (IRS Employer Identification No.) "
    "123 Main Street, New York, NY 10001 (Address of principal executive offices) "
    "(212) 555-0100 (Registrant's telephone number, including area code) "
    "Check the appropriate box below if the Form 8-K filing is intended to "
    "simultaneously satisfy the filing obligation. "
    "Emerging growth company \u2610 If an emerging growth company, indicate by check mark "
    "if the registrant has elected not to use the extended transition period for "
    "complying with any new or revised financial accounting standards provided "
    "pursuant to Section 13(a) of the Exchange Act. "
    "Securities registered pursuant to Section 12(b) of the Act: "
    "Title of each class Trading Symbol Name of each exchange on which registered "
    "Common Stock, par value $0.001 per share ACME The Nasdaq Stock Market LLC "
)

REAL_CONTENT = (
    "Item 5.02 Departure of Directors or Certain Officers; Election of Directors; "
    "Appointment of Certain Officers; Compensatory Arrangements of Certain Officers. "
    "On March 15, 2026, John Smith resigned from his position as Chief Financial "
    "Officer of ACME Corp, effective April 1, 2026. Mr. Smith's resignation was "
    "not the result of any disagreement with the Company on any matter relating to "
    "the Company's operations, policies, or practices. "
    "In connection with his departure, Mr. Smith will receive severance of $2.4 million "
    "and accelerated vesting of 150,000 restricted stock units. "
    "The Board of Directors has appointed Jane Doe as interim Chief Financial Officer, "
    "effective April 1, 2026. Ms. Doe previously served as Vice President of Finance "
    "and has been with the Company since 2019. "
    "Item 9.01 Financial Statements and Exhibits. "
    "Exhibit 10.1 Separation Agreement between ACME Corp and John Smith. "
    "SIGNATURE Pursuant to the requirements of the Securities Exchange Act of 1934. "
)

# Full filing text = cover page + real content (this is what the fetcher returns today)
FULL_FILING_TEXT = COVER_PAGE_BOILERPLATE + REAL_CONTENT

# A filing that's ALL boilerplate with minimal real content
MOSTLY_BOILERPLATE = (
    "UNITED STATES SECURITIES AND EXCHANGE COMMISSION Washington, D.C. 20549 "
    "FORM 8-K CURRENT REPORT Date of Report: March 20, 2026 "
    "SMALLCO INC (Exact name of registrant as specified in its charter) "
    "Emerging growth company \u2610 If an emerging growth company, indicate by check mark "
    "if the registrant has elected not to use the extended transition period for "
    "complying with any new or revised financial accounting standards. "
    "Smaller reporting company \u2612 "
    "Non-accelerated filer \u2612 "
    "Item 8.01 Other Events. "
    "The Company announced organizational changes."
)


# ============================================================
# Tests for strip_cover_page (fetcher.py)
# ============================================================

class TestStripCoverPage:
    """Tests for the cover page removal function in fetcher.py."""

    def test_strips_cover_page_before_item(self):
        """Cover page text before 'Item X.XX' should be removed."""
        result = strip_cover_page(FULL_FILING_TEXT)
        # Should start at the Item marker, not the cover page
        assert result.startswith("Item 5.02")
        # Cover page text should be gone
        assert "emerging growth company" not in result.lower()
        assert "check the appropriate box" not in result.lower()
        assert "irs employer identification" not in result.lower()

    def test_preserves_real_content(self):
        """Real filing content after the Item marker should be preserved."""
        result = strip_cover_page(FULL_FILING_TEXT)
        assert "John Smith resigned" in result
        assert "Chief Financial Officer" in result
        assert "severance of $2.4 million" in result
        assert "Jane Doe" in result

    def test_handles_item_501(self):
        """Should work with any Item number format."""
        text = "Cover page stuff. Item 1.01 Entry into a Material Agreement. Real content here."
        result = strip_cover_page(text)
        assert result.startswith("Item 1.01")
        assert "Cover page stuff" not in result

    def test_handles_lowercase_item(self):
        """Some filings use lowercase 'item' — should still work."""
        text = "Cover page. item 5.02 Departure of Directors. Content here."
        result = strip_cover_page(text)
        assert result.startswith("item 5.02")

    def test_no_item_marker_returns_original(self):
        """If there's no Item marker (unusual), return the text as-is."""
        text = "Some text with no item markers at all."
        result = strip_cover_page(text)
        assert result == text

    def test_empty_text(self):
        """Empty string should return empty string."""
        assert strip_cover_page("") == ""

    def test_multiple_items_keeps_first(self):
        """Should strip up to the FIRST Item marker, keeping all content after."""
        text = "Cover. Item 5.02 Departure stuff. Item 1.01 Agreement stuff."
        result = strip_cover_page(text)
        assert result.startswith("Item 5.02")
        assert "Item 1.01" in result
        assert "Cover." not in result


# ============================================================
# Tests for extract_summary (summarizer.py)
# ============================================================

class TestExtractSummary:
    """Tests for the fallback sentence-scoring summarizer."""

    def test_no_boilerplate_in_summary(self):
        """The summary should NEVER contain SEC cover page boilerplate."""
        keywords = ["resignation", "resigned", "cfo", "severance"]
        summary = extract_summary(FULL_FILING_TEXT, keywords)

        # These boilerplate phrases must never appear in summaries
        bad_phrases = [
            "emerging growth company",
            "indicate by check mark",
            "elected not to use the extended transition",
            "check the appropriate box",
            "exact name of registrant",
            "irs employer identification",
            "title of each class",
            "trading symbol",
            "smaller reporting company",
            "accelerated filer",
        ]
        summary_lower = summary.lower()
        for phrase in bad_phrases:
            assert phrase not in summary_lower, (
                f"Boilerplate leaked into summary: '{phrase}'\nFull summary: {summary}"
            )

    def test_summary_has_real_content(self):
        """The summary should contain actual filing information."""
        keywords = ["resignation", "resigned", "cfo", "severance"]
        summary = extract_summary(FULL_FILING_TEXT, keywords)
        summary_lower = summary.lower()

        # Should mention at least one of the key facts from the real content
        has_name = "smith" in summary_lower or "doe" in summary_lower
        has_role = "cfo" in summary_lower or "chief financial" in summary_lower or "officer" in summary_lower
        has_event = "resign" in summary_lower or "depart" in summary_lower or "sever" in summary_lower

        assert has_name or has_role or has_event, (
            f"Summary doesn't mention key facts from the filing.\nSummary: {summary}"
        )

    def test_stripped_text_produces_clean_summary(self):
        """When cover page is already stripped, summary should be even better."""
        stripped = strip_cover_page(FULL_FILING_TEXT)
        keywords = ["resignation", "resigned", "cfo", "severance"]
        summary = extract_summary(stripped, keywords)

        # After stripping, there's no way boilerplate leaks through
        assert "emerging growth" not in summary.lower()
        # And we should get real content
        assert len(summary) > 20

    def test_mostly_boilerplate_filing(self):
        """Filings that are mostly boilerplate should still not leak it."""
        keywords = ["organizational changes"]
        summary = extract_summary(MOSTLY_BOILERPLATE, keywords)
        assert "emerging growth" not in summary.lower()
        assert "indicate by check mark" not in summary.lower()

    def test_empty_text_returns_empty(self):
        """Empty input should return empty string."""
        assert extract_summary("", ["keyword"]) == ""
        assert extract_summary(None, ["keyword"]) == ""

    def test_no_keywords_still_avoids_boilerplate(self):
        """Even without keywords, boilerplate should not appear."""
        # With no keywords, summarizer falls back to first 2 sentences
        # After stripping, those should be real content
        stripped = strip_cover_page(FULL_FILING_TEXT)
        summary = extract_summary(stripped)
        assert "emerging growth" not in summary.lower()

    def test_hard_skip_works(self):
        """Sentences with hard-skip patterns should be completely excluded."""
        # A text where the only high-scoring sentences are boilerplate
        text = (
            "Emerging growth company check mark if the registrant has elected "
            "not to use the extended transition period. "
            "The CEO resigned effective immediately."
        )
        keywords = ["resigned"]
        summary = extract_summary(text, keywords)
        # The boilerplate sentence should be skipped; only the real sentence remains
        assert "resigned" in summary.lower()
        assert "emerging growth" not in summary.lower()


# ============================================================
# Integration test: full pipeline simulation
# ============================================================

class TestPipelineIntegration:
    """Test the full fallback path: text extraction -> summarizer."""

    def test_full_fallback_path(self):
        """Simulate what happens when LLM fails: strip cover page, then summarize.

        This is the exact code path from app.py lines 604-606:
            keywords = filing.get("matched_keywords", "").split(",")
            filing["summary"] = extract_summary(filing.get("raw_text", ""), keywords)
        """
        # Simulate the text after fetcher extracts it (with cover page stripping)
        raw_text = strip_cover_page(FULL_FILING_TEXT)

        # Simulate keywords from Stage 2 filter
        matched_keywords = "resignation,resigned,severance,cfo"
        keywords = matched_keywords.split(",")

        summary = extract_summary(raw_text, keywords)

        # Verify: no boilerplate, has real content
        assert "emerging growth" not in summary.lower()
        assert len(summary) > 20

        # Summary should be readable and informative
        print(f"\n  Generated summary: {summary}")

    def test_fallback_with_empty_keywords(self):
        """When keywords are empty (e.g., near-miss 5.02 filings)."""
        raw_text = strip_cover_page(FULL_FILING_TEXT)

        # Empty keywords string splits to [""] — this is what actually happens
        keywords = "".split(",")

        summary = extract_summary(raw_text, keywords)
        assert "emerging growth" not in summary.lower()
        assert len(summary) > 20
