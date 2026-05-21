"""Retrofit script — scan every filing's stored structured_summary JSON and
flag those that disclose market-based comp targets (stock-price, market-cap,
or TSR hurdles). Zero LLM calls — pure data transform on what's already in
the database.

Used to populate the new "Market Targets" filter retroactively after the
schema split. Can be run any number of times — it's idempotent.
"""
import json
from market_targets import detect_from_json_string


def run_retrofit(verbose=True, run_id=None):
    """Walk every row in `filings` with a non-null structured_summary,
    detect market-based targets, and update:
      - the has_market_targets column (0/1)
      - the structured_summary JSON (adds has_market_targets + market_targets keys)

    Returns a dict with counters: total_scanned, flagged, updated_json, errors.
    If run_id is provided, completes that backfill_runs row at the end.
    """
    # Imported here so this module is safe to import without DB side effects
    from database import get_connection, _placeholder, complete_backfill_run

    stats = {
        "total_scanned": 0,
        "flagged": 0,
        "updated_json": 0,
        "errors": 0,
        "by_type": {"stock_price": 0, "market_cap": 0, "tsr": 0},
    }

    conn = get_connection()
    cursor = conn.cursor()
    p = _placeholder()

    # Stream rows so we don't load the whole table into memory
    cursor.execute(
        "SELECT id, accession_no, company, structured_summary, has_market_targets "
        "FROM filings WHERE structured_summary IS NOT NULL"
    )
    rows = cursor.fetchall()

    if verbose:
        print(f"[RETROFIT] Scanning {len(rows)} filings with structured_summary...", flush=True)

    # Need a separate cursor for updates so we don't disturb the SELECT stream
    update_cursor = conn.cursor()

    for row in rows:
        stats["total_scanned"] += 1

        # Row is sqlite3.Row OR psycopg tuple — both support index access
        filing_id = row[0]
        accession = row[1]
        company = row[2]
        existing_json = row[3]
        existing_flag = row[4]

        try:
            detection = detect_from_json_string(existing_json)
        except Exception as e:
            stats["errors"] += 1
            if verbose:
                print(f"[RETROFIT] ERROR parsing filing {filing_id} ({accession}): {e}", flush=True)
            continue

        if detection["has_any"]:
            stats["flagged"] += 1
            if detection["targets"]["stock_price"]:
                stats["by_type"]["stock_price"] += 1
            if detection["targets"]["market_cap"]:
                stats["by_type"]["market_cap"] += 1
            if detection["targets"]["tsr"]:
                stats["by_type"]["tsr"] += 1

        # Always update the JSON to embed the new keys (even when false) so the
        # template can render reliably without falling back to legacy code paths.
        try:
            parsed = json.loads(existing_json) if existing_json else {}
            if not isinstance(parsed, dict):
                parsed = {}
        except (json.JSONDecodeError, ValueError, TypeError):
            stats["errors"] += 1
            continue

        parsed["has_market_targets"] = detection["has_any"]
        parsed["market_targets"] = detection["targets"]
        new_json = json.dumps(parsed)

        # Only write back if something actually changed (saves Postgres I/O)
        new_flag = 1 if detection["has_any"] else 0
        if new_json == existing_json and new_flag == (existing_flag or 0):
            continue

        try:
            update_cursor.execute(
                f"UPDATE filings SET structured_summary = {p}, has_market_targets = {p} WHERE id = {p}",
                (new_json, new_flag, filing_id),
            )
            stats["updated_json"] += 1
        except Exception as e:
            stats["errors"] += 1
            if verbose:
                print(f"[RETROFIT] ERROR updating filing {filing_id} ({company}): {e}", flush=True)

    conn.commit()
    conn.close()

    if verbose:
        print(
            f"[RETROFIT] Done. Scanned: {stats['total_scanned']}, "
            f"flagged: {stats['flagged']}, updated: {stats['updated_json']}, "
            f"errors: {stats['errors']}",
            flush=True,
        )
        print(
            f"[RETROFIT] Breakdown — stock_price: {stats['by_type']['stock_price']}, "
            f"market_cap: {stats['by_type']['market_cap']}, "
            f"tsr: {stats['by_type']['tsr']}",
            flush=True,
        )

    # If invoked from a tracked backfill_run, close it out
    if run_id is not None:
        try:
            complete_backfill_run(
                run_id,
                fetched=stats["total_scanned"],
                filtered=stats["flagged"],
                new=stats["updated_json"],
                skipped=stats["errors"],
                status="completed",
            )
        except Exception as e:
            if verbose:
                print(f"[RETROFIT] WARN: could not mark backfill_run complete: {e}", flush=True)

    return stats


if __name__ == "__main__":
    # Allow running from the command line: `python retrofit_market_targets.py`
    run_retrofit(verbose=True)
