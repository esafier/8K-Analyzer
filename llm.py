# llm.py — Handles OpenAI API calls for filing classification and summarization
# Used by Stage 3 of the filtering pipeline and by test_prompt.py

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
