# llm.py — Handles OpenAI API calls for filing classification and summarization
# Used by Stage 3 of the filtering pipeline and by test_prompt.py

import json
import os
from openai import OpenAI
from config import OPENAI_API_KEY, LLM_MODEL, PROMPTS_DIR, ACTIVE_PROMPT


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


def classify_and_summarize(filing_text, prompt_file=None):
    """Send a filing to the LLM for classification and summarization.

    Args:
        filing_text: The plain text content of the 8-K filing
        prompt_file: Which prompt file to use (default: ACTIVE_PROMPT from config)

    Returns:
        Dictionary with keys: relevant, category, subcategory, summary
        Returns None if the API call fails
    """
    # Load the prompt template and plug in the filing text
    template = _load_prompt(prompt_file)
    prompt = template.replace("{filing_text}", filing_text)

    try:
        client = OpenAI(api_key=OPENAI_API_KEY)

        response = client.chat.completions.create(
            model=LLM_MODEL,
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
