# Unread Filing Tracking — Design

**Date:** 2026-05-26
**Status:** Approved, ready for implementation plan

---

## Summary

Track which filings the user has scanned so they can pick up where they left off after a long backfill instead of losing their place. A filing becomes "read" automatically when its row scrolls into the viewport on the dashboard for ~1.5 seconds (or when the user opens its detail page). Unread filings get a subtle visual marker, and the dashboard gains a "Show unread only" filter.

---

## Goals & non-goals

**Goals**
- Make it obvious at a glance which filings the user hasn't reviewed.
- Let the user filter the dashboard down to just unread filings.
- Require zero manual marking — passive tracking based on scroll visibility and detail-page visits.
- Survive across sessions (state stored server-side, not in cookies/localStorage).

**Non-goals (explicitly not building)**
- Per-backfill progress counters or warnings ("47 unreviewed from your last run").
- Per-user tracking. The app uses a single shared access code; read state is global.
- Manual mark-as-unread or mark-as-read controls.
- Counting only-glanced-at rows differently from fully-read rows.

---

## Storage

Add one column to the existing `filings` table:

```sql
ALTER TABLE filings ADD COLUMN read_at TIMESTAMP NULL;
CREATE INDEX idx_filings_read_at ON filings(read_at);
```

- `read_at IS NULL` → unread.
- `read_at` set to a timestamp → read at that time.

The migration runs on app boot via the existing `_migrate_add_columns` flow in [database.py](../../../database.py). It must execute identical DDL on both SQLite and PostgreSQL — `TIMESTAMP NULL` works on both.

**Clean-slate seed:** the migration sets `read_at = CURRENT_TIMESTAMP` for every existing row in a single `UPDATE filings SET read_at = CURRENT_TIMESTAMP WHERE read_at IS NULL` statement, immediately after adding the column. This is a one-time backfill — subsequent boots are no-ops because all old rows already have `read_at` set, and new filings from the SEC come in with `read_at = NULL` (the column default).

**Index rationale:** the "Show unread only" filter queries `WHERE read_at IS NULL` against a table that will eventually hold tens of thousands of rows. A partial/regular index on `read_at` keeps that query fast.

---

## Backend

### New endpoint: `POST /api/filings/mark-read`

Lives in [app.py](../../../app.py), wrapped in the existing `@require_auth` (or equivalent session check) decorator.

**Request body** (JSON):
```json
{ "filing_ids": [1, 2, 3] }
```

**Behavior:**
- Parse JSON, validate `filing_ids` is a list of ints (reject otherwise with 400).
- Cap at 100 ids per request (defensive — batch size client-side will be much smaller).
- Execute one UPDATE: `UPDATE filings SET read_at = CURRENT_TIMESTAMP WHERE id = ANY(%s) AND read_at IS NULL` (use the SQLite-compatible `IN (...)` form via the project's existing parameterization helpers).
- Return `{"marked": <rowcount>}`.

**Idempotency:** the `AND read_at IS NULL` clause means re-marking an already-read filing is a no-op. The endpoint can be called freely without side effects.

### New endpoint: detail-page side-effect

The existing `GET /filing/<id>` handler in [app.py](../../../app.py) updates `read_at` to `CURRENT_TIMESTAMP` for that single filing if it is currently NULL. Same idempotent pattern as the bulk endpoint. This guarantees that even if scroll-tracking JS fails, opening a filing still marks it.

### Modify: dashboard query

In the index route in [app.py](../../../app.py), add an `unread` query-parameter handler analogous to the existing `urgent` / `market_targets` toggles. When `unread=1`, append `AND read_at IS NULL` to the WHERE clause. Pass `current_unread` into the template context.

---

## Frontend

### Visual marker for unread rows

Each `<tr>` in [templates/index.html](../../../templates/index.html) gets a conditional class: `class="clickable-row {% if not filing['read_at'] %}unread{% endif %}"`.

In [static/style.css](../../../static/style.css), add:

```css
tr.unread {
    border-left: 4px solid #0d6efd;  /* Bootstrap primary blue */
}
tr.unread td:nth-child(4) {  /* company name column */
    font-weight: 600;
}
```

The marker only reflects state at page-load time. Scrolling does not re-paint rows as they're marked read mid-session — that would be visually jumpy. Next page refresh picks up the new state. This is intentional and documented in the spec.

### "Show unread only" filter checkbox

In [templates/index.html](../../../templates/index.html), inside the filter card next to the URGENT and Market Targets checkboxes:

```jinja
<div class="form-check mb-2">
    <input type="checkbox" name="unread" value="1" class="form-check-input"
           id="unreadCheck" {{ 'checked' if current_unread }}>
    <label class="form-check-label" for="unreadCheck">
        <span class="badge bg-primary">Unread</span> only
    </label>
</div>
```

Also append `&unread=<value>` to the existing `q` URL-builder used by pagination links so the filter survives paging.

### Scroll-tracking JavaScript

A new `<script>` block in [templates/index.html](../../../templates/index.html) — or a small standalone file in `static/` referenced by `index.html`. Pseudocode:

```js
const DWELL_MS = 1500;
const BATCH_INTERVAL_MS = 2000;
const queue = new Set();
const timers = new Map();

const observer = new IntersectionObserver((entries) => {
  for (const entry of entries) {
    const id = entry.target.dataset.filingId;
    if (entry.isIntersecting && entry.intersectionRatio >= 0.5) {
      // start dwell timer
      if (!timers.has(id)) {
        timers.set(id, setTimeout(() => {
          queue.add(parseInt(id, 10));
          timers.delete(id);
        }, DWELL_MS));
      }
    } else {
      // cancel dwell if row leaves before timer fires
      if (timers.has(id)) {
        clearTimeout(timers.get(id));
        timers.delete(id);
      }
    }
  }
}, { threshold: [0.5] });

document.querySelectorAll('tr.clickable-row').forEach(row => observer.observe(row));

setInterval(() => {
  if (queue.size === 0) return;
  const ids = [...queue];
  queue.clear();
  fetch('/api/filings/mark-read', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filing_ids: ids }),
  }).catch(() => {
    // on failure, re-queue so next batch retries
    ids.forEach(id => queue.add(id));
  });
}, BATCH_INTERVAL_MS);

// flush on page unload (best-effort, uses navigator.sendBeacon)
window.addEventListener('beforeunload', () => {
  if (queue.size === 0) return;
  const ids = [...queue];
  navigator.sendBeacon('/api/filings/mark-read',
    new Blob([JSON.stringify({ filing_ids: ids })], { type: 'application/json' }));
});
```

Each row in the rendered table needs `data-filing-id="{{ filing['id'] }}"` so the observer can pick it up.

**Why 0.5 intersection threshold + 1.5s dwell:** prevents quick scroll-to-bottom from marking everything read; the row must actually sit in the viewport at least half-visible for over a second.

**Why batch every 2s:** turns 20+ potential POSTs per page into ~1.

---

## Data flow

```
User loads dashboard
  → server query includes read_at for each filing
  → template adds .unread class to rows where read_at IS NULL
  → JS sets up IntersectionObserver on every row

User scrolls
  → each row entering ≥50% viewport starts a 1.5s timer
  → if it stays visible, id added to in-memory Set
  → if it leaves first, timer cancelled
  → every 2s, Set is drained into one POST /api/filings/mark-read
  → server runs UPDATE filings SET read_at = NOW() WHERE id IN (...) AND read_at IS NULL

User clicks into /filing/<id>
  → server handler marks that filing read on page render

User reloads dashboard
  → rows marked read since last load lose their .unread styling
```

---

## Edge cases

| Case | Behavior |
|---|---|
| User opens a filing in a new tab without scrolling its row to 1.5s dwell | Detail-page handler marks it read on render. |
| Network failure on mark-read POST | JS re-queues the ids for the next batch. Worst case: ids lost on tab close after one failed flush — acceptable. |
| User has filter `unread=1` active and scrolls through 20 rows | Rows get marked read in the DB but stay visible on the current page. Next pagination click or filter submit refreshes and they're gone. Intentional — avoids items vanishing under the user mid-scroll. |
| User reloads page while in `unread=1` view | Now-empty rows disappear. Pagination adjusts. Normal. |
| New SEC filings arrive while user is browsing | They come in with `read_at = NULL` (default), appear unread on next refresh. |
| Backfill imports thousands of new filings | All inserted with `read_at = NULL` — the user can spot the entire new batch. (This is the whole point of the feature.) |
| Two browser tabs scrolling the same dashboard | Each tab independently POSTs ids; UPDATE is idempotent due to `AND read_at IS NULL`. No conflict. |
| Filter `unread=1` + category + search | All conditions AND together in the WHERE clause. Standard composition. |
| Pagination of unread filter | Page count derived from the `WHERE read_at IS NULL AND ...` count. Normal pagination behavior. |
| SQLite vs PostgreSQL `IN (?, ?, ?)` parameterization | Use the project's existing `_placeholder()` helper in [database.py](../../../database.py) — returns `?` for SQLite and `%s` for PostgreSQL. |

---

## Testing approach

- **Unit:** existing test infrastructure in [tests/](../../../tests/). Add tests for: `mark_read(ids)` DB helper marks unread rows only; idempotent on repeat call; respects `AND read_at IS NULL` filter; dashboard query with `unread=1` returns only NULL-read_at rows.
- **Manual:** load dashboard, scroll, hard-refresh — confirm rows that scrolled past are no longer styled unread. Toggle "Show unread only" — confirm only unread rows show. Open a filing — confirm it's marked read after navigating back.
- **Migration:** verify on a fresh SQLite DB and on the existing one (which already has thousands of rows) that `_migrate_add_columns` runs cleanly, no duplicates, and the seed UPDATE populates `read_at` for legacy rows.

---

## Files touched

- [database.py](../../../database.py) — add `read_at` column migration; index; one-time clean-slate UPDATE.
- [app.py](../../../app.py) — new `/api/filings/mark-read` POST endpoint; modify `/` route to handle `unread` query param; modify `/filing/<id>` route to set `read_at` on load.
- [templates/index.html](../../../templates/index.html) — `unread` filter checkbox; `data-filing-id` and `unread` class on rows; new scroll-tracking script block; pagination URL-builder update.
- [static/style.css](../../../static/style.css) — `.unread` row styles.
- [tests/](../../../tests/) — new tests for the DB helper and route behavior.

---

## What's NOT in scope (reaffirmed)

- No per-backfill progress UI on the `/backfill` page.
- No warning when starting a new backfill while prior unread items exist.
- No "mark all read" / "mark all unread" admin button.
- No per-user differentiation.
- No "recently viewed" history view.
