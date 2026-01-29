# config.py — Settings for the 8-K Filing Analyzer
# Change these values to customize what filings you're looking for

# Your contact info for SEC EDGAR (required by SEC policy)
# Replace with your actual name and email
USER_AGENT = "elyinvesting1@gmail.com"

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

# Which model to use. GPT-4o mini is cheap and good enough for classification.
LLM_MODEL = "gpt-4o-mini"

# Folder where prompt files are stored (prompt_v1.txt, prompt_v2.txt, etc.)
# The "active" prompt used by the live pipeline is whichever one ACTIVE_PROMPT points to.
PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")
ACTIVE_PROMPT = "prompt_v1.txt"

# --- Database ---
# If DATABASE_URL is set (Render provides this), use PostgreSQL
# If not set, fall back to local SQLite file
DATABASE_URL = os.environ.get("DATABASE_URL")
DATABASE_PATH = "filings.db"

# --- Fetcher Settings ---
# Max filings to fetch per API call (SEC returns up to 100 per page)
RESULTS_PER_PAGE = 100

# Delay between API requests in seconds (SEC allows 10/sec, we'll be conservative)
REQUEST_DELAY = 0.15

# EDGAR full-text search endpoint
EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"

# Base URL for viewing filings on SEC.gov
EDGAR_FILING_BASE_URL = "https://www.sec.gov/Archives/edgar/data"
