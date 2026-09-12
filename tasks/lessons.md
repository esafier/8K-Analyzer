# Lessons

Patterns from corrections and mistakes on this project. Read at session start.

## From the user

- **Unknown market cap means skip, not keep.** First draft kept filings with
  an unknown cap "to be safe". The providers return nothing precisely for the
  OTC/shell names the floor exists to exclude, so "keep unknowns" re-admits
  them. Fail closed — and pair it with an outage check so a dead provider
  fails the run instead of emptying it.
- **Check the model roster before defaulting.** The plan named last
  generation's models; the account had a newer family. List the account's
  models and test the call shape (the 5.6 family rejects `temperature`)
  before wiring a default.
- **Bound the run to the user's clock and budget.** "Overnight" plans get
  re-scoped when it isn't night; prefer phased work with a commit per step
  so any stop point leaves something usable.

## From our own mistakes

- **Never echo a secret, even to check it's set.** `${K:+x}${K:-MISSING}`
  prints `$K` when set. Check with `${#K}` (length) only.
- **A Python script in the scratchpad can't import project modules.** Run it
  with `PYTHONPATH="$(pwd)"`, or it dies with ModuleNotFoundError after the
  launcher has already reported success.
- **`| grep` hides a failed exit code and buffers output.** A backgrounded
  `python ... | grep` reported exit 0 for a run that had failed. Log to a
  file and filter the file.
- **An API client with no timeout turns one bad request into a dead run.**
  The gap backfill sat inside a single OpenAI call for 16 hours — process
  alive, no error, no progress, and the log's last line looked like normal
  work. Every network client needs an explicit timeout and retry budget
  (`llm._client()`), and a long job needs a heartbeat so "stalled" doesn't
  read as "still going".
- **Check log mtime, not just the last line.** A stalled job and a working
  job have identical tails. The file's modification time is what separates
  them; compare it to now before reporting progress.
- **Validate detectors on real filings, not fixtures.** Every false-positive
  pattern this project hit (disagreement boilerplate, "Cause" defined vs
  invoked, clusters without an exit, new-hire grants as off-cycle) passed
  unit tests and failed on the first real batch.
- **Context signals need an event in the filing.** Company history is not a
  reason to read a filing unless something in *this* filing triggers it.
- **Linux-only strftime (`%-I`) raises on Windows** and surfaces as a 500 in a
  template. Use portable directives and strip in Python.
