"""Lead and message models, plus the dashboard contact ledger."""

from dataclasses import dataclass
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Callable
from app.storage import connect, postgres


def validate_company_name(company: str) -> str:
    if not isinstance(company, str) or not company.strip():
        raise ValueError("Enter the business name.")
    if any(ord(c) < 32 for c in company):
        raise ValueError("Company name cannot contain control characters.")
    company = " ".join(company.split())
    if "|" in company:
        raise ValueError("Company name cannot contain '|'.")
    if len(company) > 120:
        raise ValueError("Company name is too long (maximum 120 characters).")
    return company


@dataclass(frozen=True)
class Lead:
    id: int
    phone: str
    company_name: str
    viber_name: str | None
    contact_name: str
    created_at: str


@dataclass(frozen=True)
class Message:
    text: str
    direction: str = "MESSAGE"


class LeadStore:
    def __init__(self, path: str | Path):
        self.path = str(path) if postgres(path) else Path(path)
        if not postgres(path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            with db:
                db.execute("""CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT NOT NULL,
                    company_name TEXT NOT NULL,
                    viber_name TEXT,
                    contact_name TEXT NOT NULL,
                    created_at DATETIME NOT NULL
                )""")
                columns = {row[1] for row in db.execute("PRAGMA table_info(leads)")}
                if "viber_name" not in columns:
                    db.execute("ALTER TABLE leads ADD COLUMN viber_name TEXT")

    def _connect(self) -> sqlite3.Connection:
        return connect(self.path)

    def create_with_android(self, phone: str, company: str,
                            add_contact: Callable[[str, str], None]) -> Lead:
        company = validate_company_name(company)
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "INSERT INTO leads (phone, company_name, contact_name, created_at) "
                "VALUES (?, ?, ?, ?)", (phone, company, "pending", created_at))
            lead_id = row.lastrowid
            name = f"{company} | SJT-{lead_id}"
            db.commit()  # Never hold a database transaction while operating Android.
            add_contact(name, phone)  # Must verify creation or raise.
            db.execute("UPDATE leads SET contact_name = ? WHERE id = ?", (name, lead_id))
            db.commit()
            return Lead(lead_id, phone, company, None, name, created_at)
        except Exception:
            db.rollback()
            if 'lead_id' in locals():
                db.execute("DELETE FROM leads WHERE id=? AND contact_name='pending'", (lead_id,))
                db.commit()
            raise
        finally:
            db.close()

    def create_verified(self, phone: str, company: str, viber_name: str, db) -> Lead:
        """Save a desktop-verified contact in the caller's ownership transaction."""
        company = validate_company_name(company)
        if db.execute("SELECT 1 FROM leads WHERE phone=?", (phone,)).fetchone():
            raise ValueError("This phone number is already in your contact ledger.")
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        row = db.execute(
            "INSERT INTO leads(phone,company_name,viber_name,contact_name,created_at) VALUES(?,?,?,?,?)",
            (phone, company, viber_name, company, created_at))
        contact_name = f"{company} | SJT-{row.lastrowid}"
        db.execute("UPDATE leads SET contact_name=? WHERE id=?", (contact_name, row.lastrowid))
        return Lead(row.lastrowid, phone, company, viber_name, contact_name, created_at)

    def get(self, lead_id: int) -> Lead | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        return Lead(**dict(row)) if row else None

    def all(self) -> list[Lead]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT * FROM leads ORDER BY id").fetchall()
        return [Lead(**dict(row)) for row in rows]

    def set_viber_name(self, lead_id: int, name: str) -> None:
        if not isinstance(name, str) or any(ord(c) < 32 for c in name):
            raise ValueError("Viber name must be text without control characters.")
        name = " ".join(name.split())
        if not name or len(name) > 120:
            raise ValueError("Viber name must contain 1 to 120 characters.")
        with closing(self._connect()) as db:
            with db:
                row = db.execute("UPDATE leads SET viber_name = ? WHERE id = ?",
                                 (name, lead_id))
                if row.rowcount != 1:
                    raise ValueError(f"Lead {lead_id} does not exist.")
