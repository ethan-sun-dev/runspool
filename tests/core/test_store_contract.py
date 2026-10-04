"""The default SQLite store passes the store seam contract."""

import pytest

from runspool.core.store import Store
from runspool.persistence.connection import Database
from runspool.testing import StoreContract


class TestSqliteStore(StoreContract):
    @pytest.fixture
    def store(self, tmp_path):
        db = Database(tmp_path / "store.db")
        db.init()
        return Store(db)
