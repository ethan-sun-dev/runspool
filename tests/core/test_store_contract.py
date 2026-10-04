"""The default SQLite store passes the store seam contract."""

from runspool.core.store import Store
from runspool.persistence.connection import Database
from runspool.testing import StoreContract


class TestSqliteStore(StoreContract):
    @classmethod
    def open_store(cls, location):
        db = Database(location / "store.db")
        db.init()
        return Store(db)
