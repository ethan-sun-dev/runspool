from runspool.persistence.connection import Database


def test_connection_uses_wal_and_busy_timeout(tmp_path):
    # WAL + a busy timeout let the daemon's worker threads and the CLI write
    # concurrently without immediate "database is locked" failures.
    db = Database(tmp_path / "t.db")
    db.init()
    with db.connect() as conn:
        assert conn.execute("pragma journal_mode").fetchone()[0] == "wal"
        assert conn.execute("pragma busy_timeout").fetchone()[0] >= 5000
