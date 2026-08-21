# screen_grant_timing.py — Rank saved filings by the PRICE EVIDENCE alone.
#
# Usage:
#   python screen_grant_timing.py                        watchlist, from the database
#   python screen_grant_timing.py --all                  every 5.02 filing, not just saved
#   python screen_grant_timing.py --input rows.txt       offline: TICKER|DATE|COMPANY per ';'
#   python screen_grant_timing.py --limit 50 --json out.json
#
# What this is, precisely:
#
#   The spring-load screen has two halves. One half reads the filing with an LLM
#   to find the grant, the recipient, the strike and the hurdles. The other half
#   is arithmetic on the tape: was the stock down into the grant, did it jump
#   straight after, was the strike struck on the cheapest close of its month.
#
#   This runs ONLY the arithmetic half. It needs no API key and costs nothing,
#   so it can sweep every saved filing and tell you which ones sit on a
#   suspicious price path — the shortlist worth spending LLM calls on.
#
#   It cannot tell you a grant happened. A filing can top this ranking because
#   the company reported earnings two days later and the stock jumped, with no
#   grant involved at all. Treat the output as "worth reading", never as a
#   finding.
#
# The date proxy, stated plainly:
#
#   Spring-loading is about the GRANT date. Without the LLM extraction all we
#   have is the FILING date, and an Item 5.02 filing is due within four business
#   days of the event. So the anchor here sits zero to four business days LATE.
#   That biases the pop DOWNWARD — some of the move may already have happened
#   before the anchor — so a name that scores well here would likely score
#   better on its true grant date, not worse. Under-counting is the safe
#   direction for a shortlist.
#
# Writes nothing to the filings database. Price fetches populate the local
# price_history cache, which is why pointing this at a scratch SQLite file
# rather than production is the tidier way to run it.

import argparse
import json
import math

import database
from spring_load import price_path

WATCHLIST_QUERY = """
    SELECT f.ticker, f.filed_date, f.company, f.accession_no
    FROM watchlist w JOIN filings f ON f.id = w.filing_id
    WHERE f.ticker IS NOT NULL AND f.ticker <> ''
    ORDER BY f.filed_date
"""

ALL_QUERY = """
    SELECT f.ticker, f.filed_date, f.company, f.accession_no
    FROM filings f
    WHERE f.ticker IS NOT NULL AND f.ticker <> ''
      AND f.item_codes LIKE '%5.02%'
    ORDER BY f.filed_date
"""

# Filings NOT on the watchlist, over the same span, in a stable pseudo-random
# order. This is the yardstick: if the screen's high scores are no rarer here
# than on the saved filings, the score is measuring volatility, not timing.
CONTROL_QUERY = """
    SELECT f.ticker, f.filed_date, f.company, f.accession_no
    FROM filings f
    WHERE f.ticker IS NOT NULL AND f.ticker <> ''
      AND f.item_codes LIKE '%5.02%'
      AND NOT EXISTS (SELECT 1 FROM watchlist w WHERE w.filing_id = f.id)
    ORDER BY {hash_expr}
    LIMIT {limit}
"""


def load_from_db(all_filings=False):
    conn = database.get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(ALL_QUERY if all_filings else WATCHLIST_QUERY)
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


def load_control(limit=200):
    """A same-period sample of filings the user did NOT save."""
    conn = database.get_connection()
    try:
        cursor = conn.cursor()
        # Both backends need a deterministic shuffle; neither shares a syntax.
        hash_expr = "md5(f.accession_no)" if database._using_postgres() \
            else "substr(f.accession_no, -4)"
        cursor.execute(CONTROL_QUERY.format(hash_expr=hash_expr, limit=int(limit)))
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


def wilson(hits, n, z=1.96):
    """95% CI for a proportion — a rate without one invites over-reading."""
    if n <= 0:
        return (0.0, 0.0)
    p = hits / n
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((center - margin) / denom, (center + margin) / denom)


def _scored(results):
    return [r for r in results
            if r["points"] is not None and r["path"].get("windows_mature")]


def compare_report(results, control):
    """Is a high score rarer among the saved filings than among random ones?

    Without this the ranking is a trap: a 6/6 reads as damning, but the shape it
    describes — down into the date, sharp move out of it — is what any volatile
    microcap does several times a year. The only way to know whether the screen
    selects anything is to run it on filings that were NOT selected.
    """
    subject, ctl = _scored(results), _scored(control)
    if not subject or not ctl:
        print("\n  Not enough scored rows on one side to compare.")
        return

    print(f"\n{'=' * 78}")
    print("SELECTIVITY CHECK — screened filings vs filings that were not saved")
    print(f"  {'threshold':11} {'screened':>22} {'control':>22}")
    for threshold in (3, 4, 5):
        k1 = sum(1 for r in subject if r["points"] >= threshold)
        k2 = sum(1 for r in ctl if r["points"] >= threshold)
        lo1, hi1 = wilson(k1, len(subject))
        lo2, hi2 = wilson(k2, len(ctl))
        print(f"  >={threshold} points  {k1:4}/{len(subject):<4} {k1 / len(subject):6.1%}"
              f" [{lo1:4.1%},{hi1:5.1%}]"
              f"  {k2:4}/{len(ctl):<4} {k2 / len(ctl):6.1%} [{lo2:4.1%},{hi2:5.1%}]")

    k1 = sum(1 for r in subject if r["points"] >= 4)
    k2 = sum(1 for r in ctl if r["points"] >= 4)
    p1, p2 = k1 / len(subject), k2 / len(ctl)
    pooled = (k1 + k2) / (len(subject) + len(ctl))
    se = math.sqrt(pooled * (1 - pooled) * (1 / len(subject) + 1 / len(ctl)))
    z = (p1 - p2) / se if se else 0.0
    print(f"\n  two-proportion z on >=4 points: {z:+.2f}"
          f"  (|z| > 1.96 would be a real difference)")
    if abs(z) < 1.96:
        print("  The screened set is INDISTINGUISHABLE from an unselected one.")
        print("  On its own the price path is not evidence of grant timing — it is")
        print("  a volatility measure. Use it to order reading, never to conclude.")
    elif p1 > p2:
        print("  The screened set scores higher than chance would produce.")
    else:
        print("  The screened set scores LOWER than an unselected one.")


def load_from_file(path):
    """Offline input: 'TICKER|YYYY-MM-DD|Company' records separated by ';'."""
    rows = []
    for chunk in open(path).read().strip().split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split("|")
        rows.append({
            "ticker": parts[0],
            "filed_date": parts[1],
            "company": parts[2] if len(parts) > 2 else "",
            "accession_no": None,
        })
    return rows


def evidence_score(path):
    """A transparent 0-6 tally of price evidence. Deliberately NOT the 0-10
    spring-load score — that one needs the grant facts this screen never sees,
    and reusing its scale would invite reading these numbers as verdicts.

    Every point is one observable fact about the tape, listed in `reasons` so
    the number never has to be taken on trust.
    """
    points = 0
    reasons = []
    run_in, pop = path.get("run_in"), path.get("pop")
    run_out_vs_spy = path.get("run_out_vs_spy")

    if path.get("v_shape"):
        points += 2
        reasons.append("V-shape: down >=10% in, up >=10% out")
    else:
        if run_in is not None and run_in <= -0.10:
            points += 1
            reasons.append(f"run-in {run_in:+.1%}")
    if pop is not None and pop >= 0.10:
        points += 2 if pop >= 0.20 else 1
        reasons.append(f"pop {pop:+.1%} within 10d")
    if path.get("is_monthly_low"):
        points += 1
        reasons.append("anchor is the month's cheapest close")
    if run_out_vs_spy is not None and run_out_vs_spy >= 0.10:
        points += 1
        reasons.append(f"30d excess {run_out_vs_spy:+.1%} vs SPY")
    return min(points, 6), reasons


def screen(rows, verbose=True):
    results = []
    for i, row in enumerate(rows, 1):
        filed = str(row["filed_date"])[:10]
        path = price_path(row["ticker"], filed)
        rec = dict(row)
        rec["filed_date"] = filed
        rec["path"] = path
        if path.get("has_data"):
            rec["points"], rec["reasons"] = evidence_score(path)
        else:
            rec["points"], rec["reasons"] = None, []
        results.append(rec)
        if verbose and (i % 25 == 0 or i == len(rows)):
            print(f"  [{i}/{len(rows)}] screened")
    return results


def report(results, limit=25):
    usable = [r for r in results if r["points"] is not None and r["path"].get("windows_mature")]
    pending = [r for r in results if r["points"] is not None
               and not r["path"].get("windows_mature")]
    nodata = [r for r in results if r["points"] is None]

    print(f"\n{'=' * 78}")
    print("GRANT-TIMING PRICE SCREEN — price evidence only, no filing was read")
    print(f"  screened {len(results)} | scored {len(usable)} | "
          f"windows still open {len(pending)} | no price data {len(nodata)}")
    print("  anchor is the FILING date, 0-4 business days after the grant, so the")
    print("  pop is under-counted rather than over-counted")

    ranked = sorted(usable, key=lambda r: (-r["points"], -(r["path"].get("pop") or 0)))
    print(f"\n  {'ticker':8} {'filed':11} {'pts':>3} {'run-in':>8} {'pop':>8} "
          f"{'30d vs SPY':>11}  company")
    for r in ranked[:limit]:
        p = r["path"]
        run_in = f"{p['run_in']:+.1%}" if p.get("run_in") is not None else "     n/a"
        pop = f"{p['pop']:+.1%}" if p.get("pop") is not None else "     n/a"
        ex = f"{p['run_out_vs_spy']:+.1%}" if p.get("run_out_vs_spy") is not None else "    n/a"
        print(f"  {r['ticker']:8} {r['filed_date']:11} {r['points']:3} {run_in:>8} "
              f"{pop:>8} {ex:>11}  {(r.get('company') or '')[:24]}")

    top = [r for r in ranked if r["points"] >= 4]
    if top:
        print(f"\n  {len(top)} filing(s) at 4+ points — the evidence behind each:")
        for r in top:
            print(f"    {r['ticker']:8} {r['filed_date']}  {r['points']}/6")
            for reason in r["reasons"]:
                print(f"      - {reason}")

    dist = {}
    for r in usable:
        dist[r["points"]] = dist.get(r["points"], 0) + 1
    print(f"\n  points distribution: " +
          "  ".join(f"{k}:{dist[k]}" for k in sorted(dist, reverse=True)))
    print("\n  A high score means the tape looks like a well-timed grant. It does")
    print("  NOT mean a grant occurred — earnings two days later produces the same")
    print("  shape. Read the filing before believing any row here.")


def main():
    parser = argparse.ArgumentParser(
        description="Rank filings by grant-timing price evidence. No LLM, no API key.")
    parser.add_argument("--all", action="store_true",
                        help="Every 5.02 filing rather than just the watchlist")
    parser.add_argument("--input", default=None,
                        help="Offline input file: 'TICKER|DATE|Company' joined by ';'")
    parser.add_argument("--limit", type=int, default=25, help="Rows to print")
    parser.add_argument("--json", default=None, help="Dump full results here")
    parser.add_argument("--control", type=int, nargs="?", const=200, default=None,
                        metavar="N",
                        help="Also screen N unsaved filings and report whether a "
                             "high score is actually rarer among the saved ones")
    parser.add_argument("--control-input", default=None,
                        help="Offline control set, same format as --input")
    args = parser.parse_args()

    if args.input:
        rows = load_from_file(args.input)
        print(f"Reading {len(rows)} rows from {args.input}")
    else:
        backend = "PostgreSQL (DATABASE_URL is set)" if database._using_postgres() \
            else f"SQLite ({database.DATABASE_PATH})"
        print(f"Reading from: {backend}")
        rows = load_from_db(all_filings=args.all)
        print(f"{len(rows)} filings to screen")

    if not rows:
        print("Nothing to screen.")
        return

    results = screen(rows)
    report(results, limit=args.limit)

    control = None
    if args.control_input:
        control = load_from_file(args.control_input)
    elif args.control:
        control = load_control(args.control)
    if control:
        print(f"\nscreening {len(control)} control filings...")
        compare_report(results, screen(control))

    if args.json:
        with open(args.json, "w") as handle:
            json.dump(results, handle, indent=1, default=str)
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
