"""The PostgreSQL connection pool under more concurrent users than it keeps.

A reanalyze run with 12 workers froze silently: returning a sixth connection
to a full pool called the pool-returning close() that get_connection installs,
re-entering _pg_pool_lock (non-reentrant) and deadlocking. Fake connections,
so this runs without a database.
"""
import threading

import database as db


class FakeConn:
    opened = 0
    closed = 0

    def __init__(self):
        FakeConn.opened += 1
        self.alive = True

    def cursor(self):
        conn = self

        class Cursor:
            def execute(self, *a):
                if not conn.alive:
                    raise RuntimeError("stale")

            def fetchone(self):
                return (1,)
        return Cursor()

    def rollback(self):
        if not self.alive:
            raise RuntimeError("broken")

    def close(self):
        FakeConn.closed += 1
        self.alive = False


def _run_with_timeout(fn, seconds=5):
    t = threading.Thread(target=fn, daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), "pool deadlocked"


def _setup(monkeypatch):
    FakeConn.opened = FakeConn.closed = 0
    monkeypatch.setattr(db, "_using_postgres", lambda: True)
    monkeypatch.setattr(db, "_create_pg_connection", FakeConn)
    monkeypatch.setattr(db, "_pg_pool", [])


def test_returning_more_connections_than_the_pool_keeps(monkeypatch):
    _setup(monkeypatch)

    def go():
        conns = [db.get_connection() for _ in range(db._PG_POOL_MAX + 3)]
        for conn in conns:
            conn.close()

    _run_with_timeout(go)
    assert len(db._pg_pool) == db._PG_POOL_MAX
    assert FakeConn.closed == 3


def test_reused_connection_can_overflow_the_pool(monkeypatch):
    # A pooled connection handed out again gets a second close() override;
    # its overflow close must still reach the socket, not the pool.
    _setup(monkeypatch)

    def go():
        for _ in range(3):
            conns = [db.get_connection() for _ in range(db._PG_POOL_MAX + 2)]
            for conn in conns:
                conn.close()

    _run_with_timeout(go)
    assert len(db._pg_pool) == db._PG_POOL_MAX


def test_stale_and_broken_connections_are_discarded(monkeypatch):
    _setup(monkeypatch)

    def go():
        conn = db.get_connection()
        conn.close()
        db._pg_pool[0].alive = False           # went stale while idle
        fresh = db.get_connection()
        assert fresh.alive
        fresh.alive = False                    # broke while in use
        fresh.close()

    _run_with_timeout(go)
    assert db._pg_pool == []


def test_many_threads(monkeypatch):
    _setup(monkeypatch)

    def worker():
        for _ in range(50):
            conns = [db.get_connection() for _ in range(3)]
            for conn in conns:
                conn.close()

    def go():
        threads = [threading.Thread(target=worker) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    _run_with_timeout(go, seconds=20)
    assert len(db._pg_pool) <= db._PG_POOL_MAX
    assert FakeConn.opened - FakeConn.closed == len(db._pg_pool)
