import sqlite3
import tempfile
import unittest
from pathlib import Path

from pylon.outbox import Outbox
from pylon.tasks.quotes import health


class QuoteHealthTests(unittest.TestCase):
    def make_db(self, root: Path, *, validated_at: str = "", count: int = 120) -> Path:
        path = root / "quotes.sqlite3"
        with sqlite3.connect(path) as db:
            db.executescript("""
                CREATE TABLE quotes(
                    sku_key TEXT PRIMARY KEY, brand TEXT NOT NULL, model_spec TEXT NOT NULL,
                    color TEXT NOT NULL, product_name TEXT NOT NULL, quote_price INTEGER NOT NULL,
                    normalized TEXT NOT NULL, source_url TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            """)
            db.executemany(
                "INSERT INTO quotes VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    (f"sku-{index}", "测试品牌", f"测试型号 {index}", "黑", f"测试品牌 测试型号 {index} 黑",
                     1000, f"测试品牌测试型号{index}黑", "https://example.invalid/quotes", "2026-09-30T05:00:00Z")
                    for index in range(count)
                ],
            )
            if validated_at:
                db.execute("INSERT INTO meta VALUES('quotes_validated_at',?)", (validated_at,))
        return path

    def test_old_quote_is_unavailable_after_24_hours(self):
        with tempfile.TemporaryDirectory() as td:
            path = self.make_db(Path(td), validated_at="2026-09-28T05:00:00Z")
            result = health.inspect(path, now=health._timestamp("2026-09-30T06:00:01Z"))
            self.assertFalse(result["available"])
            self.assertIn("24", result["reason"])

    def test_outage_alert_is_one_per_episode_and_recovery_is_silent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            box = Outbox(root / "outbox.sqlite3")
            missing = root / "missing.sqlite3"
            first = health.check(missing, box=box, now=100)
            second = health.check(missing, box=box, now=200)
            self.assertFalse(first["available"])
            self.assertEqual(first["episode"], second["episode"])
            with box.connect() as db:
                rows = db.execute("SELECT id,status FROM events WHERE id LIKE 'quotes-outage-%'").fetchall()
            self.assertEqual(len(rows), 1)
            valid = self.make_db(root, validated_at="2026-09-30T05:00:00Z")
            now = health._timestamp("2026-09-30T06:00:00Z")
            recovered = health.check(valid, box=box, now=now, notify=False)
            self.assertTrue(recovered["available"])
            self.assertEqual(box.get(health.KEY)["episode"], "")
            third = health.check(missing, box=box, now=300)
            self.assertNotEqual(first["episode"], third["episode"])
            with box.connect() as db:
                rows = db.execute("SELECT id FROM events WHERE id LIKE 'quotes-outage-%'").fetchall()
            self.assertEqual(len(rows), 2)

    def test_empty_catalog_is_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            path = self.make_db(Path(td), validated_at="2026-09-30T05:00:00Z", count=0)
            result = health.inspect(path, now=health._timestamp("2026-09-30T06:00:00Z"))
            self.assertFalse(result["available"])
            self.assertEqual("没有有效报价", result["reason"])

    def test_catalog_with_only_invalid_quote_identity_is_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            path = self.make_db(Path(td), validated_at="2026-09-30T05:00:00Z", count=1)
            with sqlite3.connect(path) as db:
                db.execute("UPDATE quotes SET sku_key='',product_name=''")
            result = health.inspect(path, now=health._timestamp("2026-09-30T06:00:00Z"))
            self.assertFalse(result["available"])
            self.assertEqual(result["count"], 0)
            self.assertEqual("没有有效报价", result["reason"])

    def test_small_valid_catalog_is_available(self):
        with tempfile.TemporaryDirectory() as td:
            path = self.make_db(Path(td), validated_at="2026-09-30T05:00:00Z", count=2)
            result = health.inspect(path, now=health._timestamp("2026-09-30T06:00:00Z"))
            self.assertTrue(result["available"], result)

    def test_catalog_is_available_at_exact_24_hour_boundary(self):
        with tempfile.TemporaryDirectory() as td:
            path = self.make_db(Path(td), validated_at="2026-09-29T06:00:00Z")
            result = health.inspect(path, now=health._timestamp("2026-09-30T06:00:00Z"))
            self.assertTrue(result["available"], result)

    def test_scheduled_slot_fallback_never_reports_future_validation(self):
        with tempfile.TemporaryDirectory() as td:
            path = self.make_db(Path(td), validated_at="")
            with sqlite3.connect(path) as db:
                db.execute("INSERT INTO meta VALUES('last_success_slot',?)", ("2026-09-30 17:55",))
            now = health._timestamp("2026-09-30T09:38:11Z")
            result = health.inspect(path, now=now)
            self.assertEqual(result["validated_at"], now)
            self.assertTrue(result["available"], result)


if __name__ == "__main__":
    unittest.main()
