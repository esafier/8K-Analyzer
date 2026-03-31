# llm.py — Handles OpenAI API calls for filing classification, summarization,
# and signal analysis. Used by Stage 3 of the filtering pipeline, by
# test_prompt.py, and by the signal analysis feature on the dashboard.

import json
import os
from openai import OpenAI
from config import OPENAI_API_KEY, LLM_MODEL, LLM_MODEL_PREMIUM, PROMPTS_DIR, ACTIVE_PROMPT


def _load_prompt(prompt_file=None):
    """Load a prompt template from the prompts/ folder.

    Args:
        prompt_file: Filename like "prompt_v1.txt". Uses ACTIVE_PROMPT from config if None.

    Returns:
        The prompt template string with {filing_text} placeholder
    """
    if prompt_file is None:
        prompt_file = ACTIVE_PROMPT

    path = os.path.join(PROMPTS_DIR, prompt_file)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def classify_and_summarize(filing_text, prompt_file=None, model=None):
    """Send a filing to the LLM for classification and summarization.

    Args:
        filing_text: The plain text content of the 8-K filing
        prompt_file: Which prompt file to use (default: ACTIVE_PROMPT from config)
        model: Which model to use (default: LLM_MODEL from config). Pass a model
               name like "gpt-5.2" to override for premium analysis.

    Returns:
        Dictionary with keys: relevant, category, subcategory, summary
        Returns None if the API call fails
    """
    # Use the default model from config unless a specific one was requested
    use_model = model or LLM_MODEL

    # Load the prompt template and plug in the filing text
    template = _load_prompt(prompt_file)
    prompt = template.replace("{filing_text}", filing_text)

    try:
        client = OpenAI(api_key=OPENAI_API_KEY)

        response = client.chat.completions.create(
            model=use_model,
            temperature=0,  # Deterministic output for consistent classifications
            response_format={"type": "json_object"},  # Force valid JSON output
            messages=[
                {"role": "user", "content": prompt}
            ],
        )

        # Parse the JSON response
        result = json.loads(response.choices[0].message.content)

        # Track token usage (useful for cost monitoring)
        usage = response.usage
        result["_tokens_in"] = usage.prompt_tokens
        result["_tokens_out"] = usage.completion_tokens

        return result

    except Exception as e:
        print(f"    LLM call failed: {e}")
        return None


def deep_analyze(filing_text, model=None):
    """Send a filing to the LLM for comprehensive investor analysis.

    Unlike classify_and_summarize() which returns structured JSON for
    classification, this returns free-form text with section headers
    (Executive Summary, Bullish/Bearish Signals, etc.) for rich display.

    Args:
        filing_text: The plain text content of the 8-K filing
        model: Which model to use (default: LLM_MODEL_PREMIUM / GPT-5.2)

    Returns:
        Dictionary with keys: analysis (str), _tokens_in (int), _tokens_out (int)
        Returns None if the API call fails
    """
    use_model = model or LLM_MODEL_PREMIUM

    # Load the deep analysis prompt (separate from the classification prompt)
    template = _load_prompt("prompt_deep_analysis.txt")
    prompt = template.replace("{filing_text}", filing_text)

    try:
        client = OpenAI(api_key=OPENAI_API_KEY)

        response = client.chat.completions.create(
            model=use_model,
            temperature=0,
            messages=[
                {"role": "user", "content": prompt}
            ],
        )

        usage = response.usage
        return {
            "analysis": response.choices[0].message.content,
            "_tokens_in": usage.prompt_tokens,
            "_tokens_out": usage.completion_tokens,
        }

    except Exception as e:
        print(f"    Deep analysis LLM call failed: {e}")
        return None


# Default model for signal analysis. Now uses Chat Completions (no web_search
# needed) since context is pre-gathered, so any model works — including GPT-5.2.
LLM_MODEL_SIGNAL = "gpt-4o"

# Model for the web search pre-step — must support web_search in Responses API.
LLM_MODEL_WEB_SEARCH = "gpt-4o"


def web_search_context(company, ticker):
    """Search the web for recent company news to enrich signal analysis.

    Makes a focused Responses API call with web_search so the model
    actually searches (short prompt = nothing else to prioritize over
    searching). Results get injected into the signal analysis context block.

    Args:
        company: Company name (e.g., "Apple Inc.")
        ticker: Stock ticker (e.g., "AAPL")

    Returns:
        Dictionary with keys: context (str), _tokens_in (int), _tokens_out (int)
        Returns None if the API call fails
    """
    # Build a short, search-focused prompt — the model has nothing to do
    # except search, so it will actually use the web_search tool
    search_query = f"{company} ({ticker})" if ticker else company
    prompt = (
        f"Search for recent significant news about {search_query} from the "
        f"past 3 months. Include: M&A activity, regulatory actions, earnings "
        f"surprises, guidance changes, product launches, executive changes, "
        f"restatements, lawsuits, or analyst upgrades/downgrades.\n\n"
        f"Return only factual headlines with brief one-line descriptions. "
        f"No analysis or commentary — just the facts. If nothing significant "
        f"found, say 'No significant recent news found.'"
    )

    try:
        client = OpenAI(api_key=OPENAI_API_KEY)

        response = client.responses.create(
            model=LLM_MODEL_WEB_SEARCH,
            tools=[{"type": "web_search"}],
            input=prompt,
        )

        usage = response.usage
        return {
            "context": response.output_text,
            "_tokens_in": usage.input_tokens,
            "_tokens_out": usage.output_tokens,
        }

    except Exception as e:
        print(f"    Web search context call failed: {e}")
        return None


def signal_analyze(filing_text, context_block, model=None, prompt_version="v1"):
    """Run skeptical buy-side signal analysis on a filing.

    Uses Chat Completions (not Responses API) since all context — departure
    history, web search results, market data — is pre-gathered and injected
    into the context block before this function is called.

    Args:
        filing_text: The plain text content of the 8-K filing
        context_block: Pre-formatted string with company context (ticker,
                       market cap, stock price, earnings date, comp details,
                       departure history, and optionally web search results)
        model: Which model to use (default: gpt-4o)
        prompt_version: "v1" for original prompt, "v2" for hardened prompt
                        with data quality gates and broader filing type coverage.

    Returns:
        Dictionary with keys: analysis (str), _tokens_in (int), _tokens_out (int)
        Returns None if the API call fails
    """
    use_model = model or LLM_MODEL_SIGNAL

    # Pick the prompt file based on version toggle
    prompt_file = "prompt_signal_analysis_v2.txt" if prompt_version == "v2" else "prompt_signal_analysis.txt"
    template = _load_prompt(prompt_file)
    prompt = template.replace("{filing_text}", filing_text)
    prompt = prompt.replace("{context_block}", context_block)

    try:
        client = OpenAI(api_key=OPENAI_API_KEY)

        # Chat Completions — all context is already in the prompt, no tools needed
        response = client.chat.completions.create(
            model=use_model,
            temperature=0,
            messages=[
                {"role": "user", "content": prompt}
            ],
        )

        usage = response.usage
        return {
            "analysis": response.choices[0].message.content,
            "_tokens_in": usage.prompt_tokens,
            "_tokens_out": usage.completion_tokens,
        }

    except Exception as e:
        print(f"    Signal analysis LLM call failed: {e}")
        return None
