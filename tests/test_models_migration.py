from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
import unittest

from app.models import LeadStore


class LeadMigrationTests(unittest.TestCase):
    def test_existing_lead_gains_separate_viber_name_without_renaming_contact(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "leads.db"
            with closing(sqlite3.connect(path)) as db:
                with db:
                    db.execute("""CREATE TABLE leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT NOT NULL,
                    company_name TEXT NOT NULL,
                    contact_name TEXT NOT NULL,
                    created_at DATETIME NOT NULL
                )""")
                    db.execute("INSERT INTO leads VALUES (1, ?, ?, ?, ?)", (
                        "+381641234567", "Test Company", "Test Company | SJT-1",
                        "2026-10-08T00:00:00+00:00"))
            store = LeadStore(path)
            self.assertIsNone(store.get(1).viber_name)
            store.set_viber_name(1, "Person on Viber")
            lead = store.get(1)
            self.assertEqual(lead.viber_name, "Person on Viber")
            self.assertEqual(lead.company_name, "Test Company")
            self.assertEqual(lead.contact_name, "Test Company | SJT-1")

    def test_invalid_name_does_not_replace_existing_value(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LeadStore(Path(directory) / "leads.db")
            store.create_with_android("+381641234567", "Test Company",
                                      lambda name, phone: None)
            store.set_viber_name(1, "Person")
            with self.assertRaises(ValueError):
                store.set_viber_name(1, "Person\nOther")
            self.assertEqual(store.get(1).viber_name, "Person")


if __name__ == "__main__":
    unittest.main()
