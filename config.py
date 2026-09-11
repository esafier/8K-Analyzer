# config.py — Settings for the 8-K Filing Analyzer
# Change these values to customize what filings you're looking for

import sys


def _make_console_unicode_safe():
    """Stop a stray non-ASCII character in a log line from killing the process.

    Windows consoles default to cp1252, which cannot encode a check mark, an
    arrow, or an emoji. Any print() containing one raises UnicodeEncodeError —
    and because these calls sit at module import time and inside long
    background jobs, the failure lands somewhere unrelated to its cause. A
    single tick in a boot message was enough to make every command-line entry
    point (daily.py, evaluate.py, digest.py) die before doing anything.

    Errors that matter must still be loud; a decorative glyph must never be
    one of them. So encoding failures degrade to a replacement character
    rather than an exception.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass  # already fine, or not a real stream (pytest capture, pipes)


_make_console_unicode_safe()

# Load .env file so we can read API keys from it locally
from dotenv import load_dotenv
load_dotenv()

# Your contact info for SEC EDGAR (required by SEC policy).
# SEC requires a "Sample Company Name AdminContact@samplecompany.com" format —
# a bare email gets throttled harder. See https://www.sec.gov/os/accessing-edgar-data
USER_AGENT = "8K Analyzer elyinvesting1@gmail.com"

# --- Item Code Filtering (Stage 1) ---
# These are the 8-K item codes we care about.
# 5.02 = Director/officer departures, elections, compensation arrangements
# 1.01 = Entry into a material definitive agreement (often employment/severance agreements)
# 1.02 = Termination of a material definitive agreement
# 8.01 = Other events (catch-all, sometimes has comp/management info)
TARGET_ITEM_CODES = ["5.02", "1.01", "1.02", "8.01"]

# --- Keyword Filtering (Stage 2) ---
# After filtering by item code, we scan the filing text for these keywords.
# Organized by category so we can auto-label each filing.

KEYWORD_CATEGORIES = {
    "Management Change": [
        "resignation",
        "resigned",
        "departure",
        "departed",
        "termination of employment",
        "appointed as",
        "appointment of",
        "appointed to serve",
        "was appointed",
        "been appointed",
        "was elected",
        "has been elected",
        "new chief executive",
        "new ceo",
        "new cfo",
        "new coo",
        "new president",
        "named as",
        "successor",
        "interim chief",
        "interim ceo",
        "interim cfo",
        "stepping down",
        "will retire",
        "retirement",
        "separated from the company",
        "separation agreement",
        "no longer serving",
        "cease to serve",
        "will depart",
        "effective immediately",
    ],
    "Compensation": [
        "inducement award",
        "inducement grant",
        "accelerated vesting",
        "acceleration of vesting",
        "compensation plan",
        "compensation arrangement",
        "equity award",
        "stock option",
        "restricted stock",
        "restricted stock unit",
        " rsu ",
        " rsus ",
        "severance",
        "golden parachute",
        "employment agreement",
        "offer letter",
        "sign-on bonus",
        "signing bonus",
        "base salary",
        "annual bonus",
        "performance shares",
        "change in control",
        "clawback",
        "incentive plan",
        "long-term incentive",
    ],
}

# --- Sub-categories for more specific labeling ---
# Maps specific keywords to finer-grained labels
SUB_CATEGORIES = {
    "Executive Departure": ["resignation", "departure", "stepping down", "retire", "separated from", "no longer serving", "cease to serve", "will depart"],
    "New Hire": ["appointed as", "appointment of", "named as", "new ceo", "new cfo", "new coo", "new president", "was elected", "has been elected"],
    "Inducement Award": ["inducement award", "inducement grant"],
    "Accelerated Vesting": ["accelerated vesting", "acceleration of vesting"],
    "Comp Plan Change": ["compensation plan", "incentive plan", "long-term incentive", "clawback"],
    "Severance / Separation": ["severance", "golden parachute", "separation agreement"],
}

# --- LLM Settings (Stage 3) ---
# Your OpenAI API key — get one at https://platform.openai.com/api-keys
# You can also set this as an environment variable: OPENAI_API_KEY
import os

# Read API key from environment variable (set this in Render dashboard)
# Falls back to hardcoded key for local development
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# API Ninjas key — used for market cap and earnings calendar data
# Get one at https://api-ninjas.com (paid plan needed for earnings features)
API_NINJAS_KEY = os.environ.get("API_NINJAS_KEY", "")

# --- Models ---------------------------------------------------------------
# The pipeline splits work across two tiers on purpose. Extraction is a
# high-volume, low-judgment job (pull facts into a fixed schema) and runs on
# the cheapest capable model. Judgment is low-volume and high-stakes — it only
# sees filings that already carry a signal — so it gets a much stronger model
# and still costs well under a dollar a day.
#
# GPT-5.6 family, per 1M input/output tokens:
#   gpt-5.6-luna   $0.20 / $1.20  — extraction default
#   gpt-5.6-terra  $2.00 / $12.00 — judge default
#   gpt-5.6-sol    $4.00 / $20.00 — override for a hard call
#
# All three are env-overridable so the deployed models can change from the
# Render dashboard without a redeploy.
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-5.6-luna")
LLM_MODEL_PREMIUM = os.environ.get("LLM_MODEL_PREMIUM", "gpt-5.6-sol")
LLM_MODEL_JUDGE = os.environ.get("LLM_MODEL_JUDGE", "gpt-5.6-terra")

# Models that reject an explicit `temperature`. The GPT-5.6 family only
# accepts its default, and passing temperature=0 is a hard 400 — so llm.py
# omits the parameter for these rather than discovering it in production.
# Determinism is not lost in any way that matters here: the prompts return
# strict JSON, and the schema is what constrains the output.
MODELS_WITHOUT_TEMPERATURE = ("gpt-5.6",)

# Folder where prompt files are stored (prompt_v1.txt, prompt_v2.txt, etc.)
# The "active" prompt used by the live pipeline is whichever one ACTIVE_PROMPT points to.
PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")
ACTIVE_PROMPT = os.environ.get("ACTIVE_PROMPT", "prompt_v4.txt")

# Stamped onto every filing the pipeline analyzes. Bump it whenever a prompt
# or the signal weights change, so a ranking regression can be traced to the
# generation that produced it instead of being argued about.
PIPELINE_VERSION = "v4.2-signals"

# Filing text is capped before it reaches a model. Exhibits push documents to
# 120k characters; the judge does not need all of it and paying for it on
# every candidate is how a $1/day budget becomes $15/day.
MAX_EXTRACTION_CHARS = 120_000
MAX_JUDGE_CHARS = 24_000

# --- Database ---
# If DATABASE_URL is set (Render provides this), use PostgreSQL
# If not set, fall back to local SQLite file
DATABASE_URL = os.environ.get("DATABASE_URL")
DATABASE_PATH = "filings.db"

# --- Fetcher Settings ---
# Max filings to fetch per API call (SEC returns up to 100 per page)
RESULTS_PER_PAGE = 100

# Delay between API requests in seconds. SEC allows 10/sec, but on Render we
# share an egress IP with other tenants, so the per-IP budget is contested.
# 0.3s ≈ 3.3 req/sec leaves headroom for noisy neighbors.
REQUEST_DELAY = 0.3

# EDGAR full-text search endpoint
EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"

# Base URL for viewing filings on SEC.gov
EDGAR_FILING_BASE_URL = "https://www.sec.gov/Archives/edgar/data"
