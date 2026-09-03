"""The daily digest — push, not pull.

The single biggest reason the old tool stopped being used was that it
required remembering to open it. Every day of data depended on someone
clicking Backfill, and when that stopped, the feed died silently.

So the finished product is an email: the day's signals, ranked, each with the
one sentence needed to decide, and a ✓/✗ next to it. Labelling from the email
means the training loop keeps running even on days the app is never opened.

Sends via SMTP (a Gmail app password) or a Slack webhook, whichever is
configured, and dry-runs to stdout when neither is — so the job is always
runnable and never fails for want of a credential.
"""

import json
import os
import smtplib
from email.message import EmailMessage

from database import get_inbox_filings, record_digest, get_recently_digested_ids

# Cap the email. Past a dozen items the reader stops reading, and a digest
# nobody finishes is worse than a shorter one they do.
MAX_ITEMS = 12

# Only genuinely notable filings earn an email. The inbox is for browsing;
# the digest is for interrupting someone.
MIN_SCORE = 5


def build_digest(days=1, min_score=MIN_SCORE, limit=MAX_ITEMS, skip_recent=True):
    """Select and shape the filings for today's digest."""
    filings = get_inbox_filings(days=days, min_score=min_score, limit=limit * 3)

    if skip_recent:
        # A re-run, or a window that overlapped yesterday's, must not mail the
        # same filings twice — that is how a digest teaches someone to ignore it.
        already = get_recently_digested_ids(days=7)
        filings = [f for f in filings if f["id"] not in already]

    filings = filings[:limit]
    for filing in filings:
        try:
            filing["_signals"] = json.loads(filing.get("signals_json") or "[]")
        except (ValueError, TypeError):
            filing["_signals"] = []
        try:
            filing["_judge"] = json.loads(filing.get("judge_json") or "null")
        except (ValueError, TypeError):
            filing["_judge"] = None
    return filings


def render(filings, base_url=None):
    """Render the digest to (subject, html, text).

    Rendered without a Flask request context — the job that sends this has no
    request — so the base URL has to be passed in.
    """
    from labels import signing_available, label_url

    base_url = (base_url or os.environ.get("APP_BASE_URL") or "").rstrip("/")
    can_label = bool(base_url) and signing_available()

    if not filings:
        subject = "8-K signals — nothing flagged today"
        body = ("No filings cleared the signal threshold today.\n\n"
                "That is the system working: routine filings were analyzed and "
                "set aside, not skipped.\n")
        return subject, f"<p>{body.replace(chr(10), '<br>')}</p>", body

    top = filings[0]
    subject = f"8-K signals — {len(filings)} to look at, top: {top['company']}"

    text_lines, html_parts = [], []
    for filing in filings:
        arrow = {"BEARISH": "v", "BULLISH": "^", "MIXED": "~"}.get(filing.get("signal_direction"), "-")
        header = (f"[{filing.get('signal_score')}] {arrow} {filing.get('company')} "
                  f"({filing.get('ticker') or 'no ticker'}) — {filing.get('filed_date')}")
        thesis = filing.get("top_signal") or filing.get("summary") or ""
        chips = ", ".join(s.get("type", "").replace("_", " ")
                          for s in filing.get("_signals", []))

        text_lines += [header, thesis]
        if chips:
            text_lines.append(f"Signals: {chips}")
        text_lines.append(filing.get("filing_document_url") or filing.get("filing_url") or "")
        text_lines.append("")

        html_parts.append(_render_row(filing, arrow, thesis, chips, base_url, can_label))

    html = _HTML_SHELL.format(
        rows="\n".join(html_parts),
        footer=(f'<a href="{base_url}/">Open the full inbox</a>' if base_url else ""),
    )
    return subject, html, "\n".join(text_lines)


def _render_row(filing, arrow, thesis, chips, base_url, can_label):
    from labels import label_url

    color = {"BEARISH": "#d9342b", "BULLISH": "#1a9e5c", "MIXED": "#b8860b"}.get(
        filing.get("signal_direction"), "#8a919b")
    filing_url = filing.get("filing_document_url") or filing.get("filing_url") or "#"

    actions = f'<a href="{filing_url}" style="color:#2f6df6;">Read the filing</a>'
    if can_label:
        # One tap, straight from the phone. This is the labelling path most
        # likely to actually get used.
        yes = label_url(base_url, filing["id"], "signal")
        no = label_url(base_url, filing["id"], "noise")
        actions += (f' &nbsp;·&nbsp; <a href="{yes}" style="color:#14663d;">Signal</a>'
                    f' &nbsp;·&nbsp; <a href="{no}" style="color:#a11a13;">Noise</a>')

    return f"""
    <tr><td style="padding:14px 0;border-bottom:1px solid #eceef1;">
      <div style="font-size:13px;color:#5b626b;">
        <span style="display:inline-block;min-width:30px;font-weight:700;color:{color};">
          {arrow} {filing.get('signal_score')}</span>
        <strong style="color:#1a1d21;font-size:15px;">{_esc(filing.get('company'))}</strong>
        &nbsp;{_esc(filing.get('ticker') or '')}
        &nbsp;·&nbsp;{_esc(filing.get('filed_date'))}
      </div>
      <div style="font-size:14px;color:#1a1d21;margin:6px 0;line-height:1.45;">{_esc(thesis)}</div>
      <div style="font-size:11px;color:#8a919b;text-transform:uppercase;letter-spacing:.04em;">{_esc(chips)}</div>
      <div style="font-size:13px;margin-top:6px;">{actions}</div>
    </td></tr>"""


_HTML_SHELL = """<html><body style="margin:0;background:#f6f7f9;font-family:-apple-system,Segoe UI,Roboto,sans-serif;">
<div style="max-width:640px;margin:0 auto;padding:20px;background:#ffffff;">
  <h2 style="font-size:17px;margin:0 0 4px;color:#1a1d21;">8-K signals</h2>
  <p style="font-size:12px;color:#8a919b;margin:0 0 8px;">Ranked by signal strength. Routine filings are analyzed and set aside.</p>
  <table width="100%" cellpadding="0" cellspacing="0">{rows}</table>
  <p style="font-size:12px;color:#8a919b;margin-top:18px;">{footer}</p>
</div></body></html>"""


def _esc(value):
    from markupsafe import escape
    return str(escape(value or ""))


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

def send(days=1, min_score=MIN_SCORE, dry_run=False, base_url=None):
    """Build and deliver the digest. Returns a summary dict.

    Never raises on a delivery failure — a broken mail server must not fail
    the ingest job that ran before it.
    """
    filings = build_digest(days=days, min_score=min_score)
    subject, html, text = render(filings, base_url=base_url)
    ids = [f["id"] for f in filings]

    if dry_run:
        print(f"--- DIGEST DRY RUN ---\nSubject: {subject}\n\n{text}", flush=True)
        return {"channel": "dry_run", "count": len(ids), "sent": False}

    channel, ok, error = _deliver(subject, html, text)
    try:
        record_digest(channel, ids, status="sent" if ok else "failed")
    except Exception as e:
        print(f"[DIGEST] Could not record digest: {e}", flush=True)

    if ok:
        print(f"[DIGEST] Sent {len(ids)} item(s) via {channel}", flush=True)
    else:
        print(f"[DIGEST] Delivery failed via {channel}: {error}", flush=True)
    return {"channel": channel, "count": len(ids), "sent": ok, "error": error}


def _deliver(subject, html, text):
    smtp_user = os.environ.get("DIGEST_SMTP_USER")
    smtp_pass = os.environ.get("DIGEST_SMTP_PASS")
    recipient = os.environ.get("DIGEST_TO")
    webhook = os.environ.get("DIGEST_SLACK_WEBHOOK")

    if smtp_user and smtp_pass and recipient:
        try:
            _send_email(subject, html, text, smtp_user, smtp_pass, recipient)
            return "email", True, None
        except Exception as e:
            return "email", False, str(e)

    if webhook:
        try:
            _send_slack(webhook, subject, text)
            return "slack", True, None
        except Exception as e:
            return "slack", False, str(e)

    # Nothing configured — print it rather than failing. The job stays
    # runnable before the user has set up credentials.
    print(f"--- DIGEST (no channel configured) ---\nSubject: {subject}\n\n{text}", flush=True)
    return "stdout", True, None


def _send_email(subject, html, text, user, password, recipient):
    host = os.environ.get("DIGEST_SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("DIGEST_SMTP_PORT", 587))

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = user
    message["To"] = recipient
    message.set_content(text)
    message.add_alternative(html, subtype="html")

    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls()
        server.login(user, password)
        server.send_message(message)


def _send_slack(webhook, subject, text):
    import requests
    resp = requests.post(webhook, json={"text": f"*{subject}*\n```{text}```"}, timeout=15)
    resp.raise_for_status()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Send or preview the signal digest")
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--min-score", type=int, default=MIN_SCORE)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--base-url", default=None)
    args = parser.parse_args()

    # Additive, idempotent — and without it this crashes on "no such table:
    # digests" against any database created before the rebuild.
    from database import initialize_database
    initialize_database()

    send(days=args.days, min_score=args.min_score,
         dry_run=args.dry_run, base_url=args.base_url)
