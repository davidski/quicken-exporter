"""Tests for the read-only SQLite cache helper."""

from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "cache_sqlite.py"
SPEC = importlib.util.spec_from_file_location("cache_sqlite", SCRIPT)
assert SPEC and SPEC.loader
cache_sqlite = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cache_sqlite)


class CacheSqliteTests(unittest.TestCase):
    """Verify cache freshness and SQLite snapshot behavior."""

    def test_reuses_cache_until_source_signature_changes(self):
        """Refresh stale sources and reuse unchanged snapshots."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "quicken.sqlite"
            cache_dir = root / "cache"
            with sqlite3.connect(source) as connection:
                connection.execute("CREATE TABLE values_table(value TEXT NOT NULL)")
                connection.execute("INSERT INTO values_table VALUES ('first')")

            cached, refreshed = cache_sqlite.ensure_cached(source, cache_dir)
            self.assertTrue(refreshed)
            first_cache_mtime = cached.stat().st_mtime_ns
            read_only_uri = cache_sqlite._read_only_uri(cached, immutable=True)
            with sqlite3.connect(read_only_uri, uri=True) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM values_table").fetchone(),
                    ("first",),
                )
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute("INSERT INTO values_table VALUES ('write')")

            reused, refreshed = cache_sqlite.ensure_cached(source, cache_dir)
            self.assertEqual(reused, cached)
            self.assertFalse(refreshed)
            self.assertEqual(cached.stat().st_mtime_ns, first_cache_mtime)

            with sqlite3.connect(source) as connection:
                connection.execute("INSERT INTO values_table VALUES ('second')")
            refreshed_cache, refreshed = cache_sqlite.ensure_cached(source, cache_dir)
            self.assertEqual(refreshed_cache, cached)
            self.assertTrue(refreshed)
            read_only_uri = cache_sqlite._read_only_uri(cached, immutable=True)
            with sqlite3.connect(read_only_uri, uri=True) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM values_table").fetchone(),
                    (2,),
                )

    def test_backup_api_includes_committed_wal_data(self):
        """Include committed WAL data in snapshots of live databases."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "quicken.sqlite"
            connection = sqlite3.connect(source)
            try:
                self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone(), ("wal",))
                connection.execute("CREATE TABLE values_table(value TEXT NOT NULL)")
                connection.execute("INSERT INTO values_table VALUES ('from-wal')")
                connection.commit()
                self.assertTrue(source.with_name(source.name + "-wal").exists())

                cached, refreshed = cache_sqlite.ensure_cached(source, root / "cache")
                self.assertTrue(refreshed)
                read_only_uri = cache_sqlite._read_only_uri(cached, immutable=True)
                with sqlite3.connect(read_only_uri, uri=True) as cached_db:
                    self.assertEqual(
                        cached_db.execute("SELECT value FROM values_table").fetchone(),
                        ("from-wal",),
                    )
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
