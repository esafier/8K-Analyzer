"""Labelling — turning the user's reactions into training data.

The system has no way to get better without knowing when it was wrong. The
old app collected exactly one signal of taste (a watchlist star) and never
read it back, so no prompt or weight change could be shown to be an
improvement over the last one.

The constraint that shapes everything here is that labelling must cost the
user almost nothing, because the moment it feels like data entry it stops
happening — which is how the tool got abandoned the first time. So there are
three routes to the same table, in descending order of effort:

  /review        one filing at a time, two keys                (deliberate)
  digest links   a tap in the email, no app, no login          (ambient)
  watchlist      stars already given, seeded once              (retroactive)

Email links are signed, not guessed: a bare /label?id=42&label=noise would
let anyone who saw a forwarded email rewrite the training set.
"""

import os

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from database import upsert_judgment, delete_judgment

# Digest links stay valid for a month. Long enough that a Friday email opened
# the following week still works; short enough that an old forwarded message
# isn't a permanent write handle.
TOKEN_MAX_AGE_SECONDS = 30 * 24 * 3600

SALT = "8k-analyzer-label-v1"

# The fallback secret in app.py exists so local development works without
# configuration. Signing with it would be theatre — the value is in a public
# repository, so anyone could mint tokens. Refuse instead, loudly.
INSECURE_SECRETS = {"8k-analyzer-secret-key", "", None}


class InsecureSecretError(RuntimeError):
    """Raised when signing is attempted with the public default key."""


def _serializer():
    secret = os.environ.get("SECRET_KEY")
    if secret in INSECURE_SECRETS:
        raise InsecureSecretError(
            "SECRET_KEY is unset or still the public default. Signed label "
            "links are disabled until a real secret is set — otherwise anyone "
            "reading this repository could forge them."
        )
    return URLSafeTimedSerializer(secret, salt=SALT)


def signing_available():
    """Whether signed links can be produced. The digest renders plain filing
    links instead of ✓/✗ buttons when they can't."""
    try:
        _serializer()
        return True
    except InsecureSecretError:
        return False


def make_token(filing_id, label):
    """Sign a (filing, label) pair for a one-tap email link."""
    return _serializer().dumps({"f": int(filing_id), "l": label})


def read_token(token, max_age=TOKEN_MAX_AGE_SECONDS):
    """Verify a token and return (filing_id, label).

    Returns (None, None) for anything invalid, expired, or malformed — the
    caller shows a friendly page rather than an error, since these arrive from
    email clients and link scanners as often as from the user.
    """
    try:
        payload = _serializer().loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired, InsecureSecretError, Exception):
        return None, None

    if not isinstance(payload, dict):
        return None, None
    try:
        return int(payload.get("f")), payload.get("l")
    except (TypeError, ValueError):
        return None, None


def label_url(base_url, filing_id, label):
    """Absolute /label/<token> URL for a digest email.

    Needs an explicit base URL because the digest is composed by a scheduled
    job with no Flask request context to infer the host from.
    """
    base = (base_url or "").rstrip("/")
    return f"{base}/label/{make_token(filing_id, label)}"


def record(filing_id, label, note=None, source="review_ui"):
    """Store a label. Returns True when accepted."""
    return upsert_judgment(filing_id, label, note=note, source=source)


def undo(filing_id):
    """Remove a label — the Undo link after a mis-tap in an email."""
    return delete_judgment(filing_id) > 0
