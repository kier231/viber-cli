"""Lead and message models, plus the small SQLite contact ledger."""

from dataclasses import dataclass
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Callable


@dataclass(frozen=True)
class Lead:
    id: int
    phone: str
    company_name: str
    contact_name: str
    created_at: str


@dataclass(frozen=True)
class Message:
    text: str
    direction: str = "MESSAGE"


class LeadStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            with db:
                db.execute("""CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT NOT NULL,
                    company_name TEXT NOT NULL,
                    contact_name TEXT NOT NULL,
                    created_at DATETIME NOT NULL
                )""")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def create_with_android(self, phone: str, company: str,
                            add_contact: Callable[[str, str], None]) -> Lead:
        if any(ord(c) < 32 for c in company):
            raise ValueError("Company name cannot contain control characters.")
        company = " ".join(company.split())
        if not company or "|" in company:
            raise ValueError("Company name must be nonempty and cannot contain '|'.")
        if len(company) > 120:
            raise ValueError("Company name is too long (maximum 120 characters).")
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "INSERT INTO leads (phone, company_name, contact_name, created_at) "
                "VALUES (?, ?, ?, ?)", (phone, company, "pending", created_at))
            lead_id = row.lastrowid
            name = f"{company} | SJT-{lead_id}"
            add_contact(name, phone)  # Must verify creation or raise.
            db.execute("UPDATE leads SET contact_name = ? WHERE id = ?", (name, lead_id))
            db.commit()
            return Lead(lead_id, phone, company, name, created_at)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def get(self, lead_id: int) -> Lead | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        return Lead(**dict(row)) if row else None

    def all(self) -> list[Lead]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT * FROM leads ORDER BY id").fetchall()
        return [Lead(**dict(row)) for row in rows]
