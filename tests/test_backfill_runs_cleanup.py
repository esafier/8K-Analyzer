"""Boot must not declare someone else's live backfill dead.

The reaper was written when the Flask worker was the only thing that ever
wrote this table, so "booting means the owner is gone" held. Now daily.py,
backfill.py, form4.py, rescore.py and outcomes.py all initialize the database,
from Actions and from laptops, against the same Postgres. An unqualified reap
marked a running Actions backfill as failed within seconds of its start.
"""
import database


def _running_run(started_hours_ago=0):
    run_id = database.create_backfill_run("gap_backfill", "2026-08-20",
                                          "2026-09-03", "default")
    if started_hours_ago:
        conn = database.get_connection()
        cursor = conn.cursor()
        p = database._placeholder()
        cursor.execute(
            f"UPDATE backfill_runs SET started_at = "
            f"datetime('now', '-{started_hours_ago} hours') WHERE id = {p}",
            (run_id,),
        )
        conn.commit()
        conn.close()
    return run_id


def _status(run_id):
    conn = database.get_connection()
    cursor = conn.cursor()
    p = database._placeholder()
    cursor.execute(f"SELECT status FROM backfill_runs WHERE id = {p}", (run_id,))
    status = cursor.fetchone()[0]
    conn.close()
    return status


def test_a_live_run_survives_another_process_booting(tmp_sqlite_db):
    run_id = _running_run()

    database.initialize_database()

    assert _status(run_id) == "running"


def test_a_long_abandoned_run_is_still_reaped(tmp_sqlite_db):
    """The original bug this existed for: a killed worker's row, stuck forever."""
    run_id = _running_run(started_hours_ago=database.STUCK_BACKFILL_HOURS + 1)

    database.initialize_database()

    assert _status(run_id) == "failed"


def test_the_grace_window_outlasts_the_actions_job_timeout(tmp_sqlite_db):
    """330 minutes is the workflow's timeout; the cutoff must exceed it."""
    assert database.STUCK_BACKFILL_HOURS * 60 > 330

    run_id = _running_run(started_hours_ago=6)
    database.initialize_database()

    assert _status(run_id) == "running"


def test_a_completed_run_is_left_alone(tmp_sqlite_db):
    run_id = _running_run(started_hours_ago=database.STUCK_BACKFILL_HOURS + 1)
    database.complete_backfill_run(run_id, fetched=1, filtered=1, new=1, skipped=0)

    database.initialize_database()

    assert _status(run_id) == "completed"
