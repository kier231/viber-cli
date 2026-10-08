"""Serialized desktop operations and a durable ledger for the localhost UI."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
import hashlib
import json
import secrets
import shutil
import sys
import threading
import time
import uuid

from app.android_contacts import add_android_contact, _find_adb
from app.models import LeadStore
from app.phone import normalize_serbian_phone
from app.viber import ViberError
from app.viber_background import BackgroundViberClient, _usable_viber_name


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_message(text):
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise ValueError("Write a message containing 1 to 4,000 characters.")
    if any(ord(c) < 32 or ord(c) > 0xFFFF or 0xD800 <= ord(c) <= 0xDFFF for c in text):
        raise ValueError("Use one line of plain text without controls or emoji.")
    return text


class WebService:
    def __init__(self, path, client_factory=BackgroundViberClient,
                 android_add=add_android_contact):
        self.store = LeadStore(path)
        self.client_factory = client_factory
        self.android_add = android_add
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="viber-desktop")
        self.previews = {}
        self.pending = 0
        self.closed = False
        with closing(self.store._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS web_operations (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL,
                    created_at TEXT NOT NULL, result TEXT, error TEXT);
                CREATE TABLE IF NOT EXISTS web_sends (
                    id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE,
                    preview_hash TEXT NOT NULL, operation_id TEXT NOT NULL,
                    lead_id INTEGER NOT NULL, phone TEXT NOT NULL,
                    company_name TEXT NOT NULL, viber_name TEXT NOT NULL,
                    text TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, error TEXT);
                CREATE TABLE IF NOT EXISTS web_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                    kind TEXT NOT NULL, detail TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS web_reads (
                    lead_id INTEGER PRIMARY KEY, viber_name TEXT NOT NULL,
                    captured_at TEXT NOT NULL, messages TEXT NOT NULL);
            """)
            db.execute("UPDATE web_operations SET state='INTERRUPTED', "
                       "error='The app stopped during this operation. Review Sent before trying again.' "
                       "WHERE state IN ('QUEUED', 'RUNNING')")
            db.execute("UPDATE web_sends SET state='UNKNOWN', updated_at=?, "
                       "error='The app stopped during this attempt. Check Viber manually; do not resend automatically.' "
                       "WHERE state IN ('QUEUED', 'SUBMITTING')", (now(),))

    def close(self):
        with self.lock:
            self.closed = True
        self.pool.shutdown(wait=True)

    def event(self, kind, detail):
        with closing(self.store._connect()) as db, db:
            db.execute("INSERT INTO web_events(created_at,kind,detail) VALUES(?,?,?)",
                       (now(), kind, detail))

    def operation(self, operation_id):
        with closing(self.store._connect()) as db:
            row = db.execute("SELECT * FROM web_operations WHERE id=?", (operation_id,)).fetchone()
        if not row:
            raise ValueError("Operation not found.")
        result = dict(row)
        result["result"] = json.loads(result["result"]) if result["result"] else None
        return result

    def _enqueue(self, kind, work):
        with self.lock:
            if self.closed:
                raise ValueError("The localhost app is stopping.")
            if self.pending >= 10:
                raise ValueError("Viber has too many queued operations. Wait for them to finish.")
            operation_id = str(uuid.uuid4())
            with closing(self.store._connect()) as db, db:
                db.execute("INSERT INTO web_operations(id,kind,state,created_at) VALUES(?,?,'QUEUED',?)",
                           (operation_id, kind, now()))
            self.pending += 1
            self.pool.submit(self._run, operation_id, work)
            return {"operation_id": operation_id}

    def _run(self, operation_id, work):
        com = None
        try:
            if sys.platform == "win32":
                import pythoncom
                pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
                com = pythoncom
            with closing(self.store._connect()) as db, db:
                db.execute("UPDATE web_operations SET state='RUNNING' WHERE id=?", (operation_id,))
            result = work()
            with closing(self.store._connect()) as db, db:
                db.execute("UPDATE web_operations SET state='SUCCEEDED', result=? WHERE id=?",
                           (json.dumps(result, ensure_ascii=False), operation_id))
        except Exception as exc:
            with closing(self.store._connect()) as db, db:
                db.execute("UPDATE web_operations SET state='FAILED', error=? WHERE id=?",
                           (str(exc), operation_id))
                # An unexpected failure, including COM initialization, never retries a send.
                db.execute("UPDATE web_sends SET state='UNKNOWN', error=?, updated_at=? "
                           "WHERE operation_id=? AND state IN ('QUEUED','SUBMITTING')",
                           (str(exc), now(), operation_id))
            self.event("ERROR", str(exc))
        finally:
            if com:
                com.CoUninitialize()
            with self.lock:
                self.pending -= 1

    def lead(self, lead_id):
        if isinstance(lead_id, bool) or not isinstance(lead_id, int):
            raise ValueError("Choose a saved contact.")
        lead = self.store.get(lead_id)
        if not lead:
            raise ValueError("Contact not found.")
        return lead

    def _verified(self, lead_id):
        lead = self.lead(lead_id)
        client = self.client_factory().connect()
        name = client.open_phone(lead.phone)
        if not _usable_viber_name(name):
            raise ViberError("Viber did not show a usable recipient name. No message was sent.")
        if lead.viber_name and name != lead.viber_name:
            raise ViberError(f"Viber showed {name!r}; this contact stores {lead.viber_name!r}. No message was sent.")
        if not lead.viber_name and lead.company_name not in name and "SJT-" not in name:
            self.store.set_viber_name(lead.id, name)
            lead = self.lead(lead.id)
        return client, lead, name

    def contacts(self):
        return [asdict(lead) for lead in self.store.all()]

    def add_contact(self, payload):
        phone = normalize_serbian_phone(payload.get("phone"))
        company = payload.get("company_name")
        if not isinstance(company, str) or not company.strip():
            raise ValueError("Enter the business name.")
        if any(lead.phone == phone for lead in self.store.all()):
            raise ValueError("This phone number is already in your contact ledger.")
        def work():
            # Check again after earlier queued additions have completed.
            if any(lead.phone == phone for lead in self.store.all()):
                raise ValueError("This phone number is already in your contact ledger.")
            lead = self.store.create_with_android(phone, company, self.android_add)
            self.event("CONTACT_ADDED", f"Added contact #{lead.id}: {lead.company_name}.")
            return asdict(lead)
        return self._enqueue("add-contact", work)

    def set_name(self, lead_id, name):
        with self.lock:
            if self.pending:
                raise ValueError("Wait for the current Viber operation before editing a name.")
            lead = self.lead(lead_id)
            self.store.set_viber_name(lead_id, name)
            self.event("NAME_UPDATED", f"Saved Viber name for contact #{lead_id}; business and Android names preserved.")
            return asdict(self.lead(lead.id))

    def open_contact(self, lead_id, read=False):
        self.lead(lead_id)
        def work():
            client, lead, name = self._verified(lead_id)
            result = {"lead": asdict(lead), "viber_name": name}
            if read:
                if not client.verify_current_name(name):
                    raise ViberError("The conversation changed; reading stopped.")
                messages = [asdict(message) for message in client.read_messages()]
                if not client.verify_current_name(name):
                    raise ViberError("The conversation changed while reading; snapshot discarded.")
                captured_at = now()
                result.update(messages=messages, captured_at=captured_at)
                with closing(self.store._connect()) as db, db:
                    db.execute("INSERT OR REPLACE INTO web_reads VALUES(?,?,?,?)",
                               (lead.id, name, captured_at, json.dumps(messages, ensure_ascii=False)))
            self.event("READ" if read else "OPEN", f"Verified contact #{lead.id}: {name}.")
            return result
        return self._enqueue("read" if read else "open", work)

    def read_current(self):
        def work():
            client = self.client_factory().connect()
            name = client.current_name()
            if not _usable_viber_name(name) or not client.verify_current_name(name):
                raise ViberError("Could not verify the current conversation.")
            messages = [asdict(message) for message in client.read_messages()]
            if not client.verify_current_name(name):
                raise ViberError("The conversation changed while reading; snapshot discarded.")
            return {"viber_name": name, "messages": messages, "captured_at": now()}
        return self._enqueue("read-current", work)

    def prepare(self, lead_id, text):
        self.lead(lead_id)
        text = validate_message(text)
        def work():
            client, lead, name = self._verified(lead_id)
            if not client.verify_current_name(name):
                raise ViberError("The conversation changed; preview stopped.")
            token = secrets.token_urlsafe(32)
            preview = {"token": token, "lead": asdict(lead), "viber_name": name,
                       "text": text, "expires_at": time.time() + 120}
            with self.lock:
                self.previews = {key: value for key, value in self.previews.items()
                                 if value["expires_at"] > time.time()}
                self.previews[token] = preview
            return preview
        return self._enqueue("prepare", work)

    def send(self, payload):
        if payload.get("confirmed") is not True:
            raise ValueError("Review the recipient and message, then confirm Send.")
        token = payload.get("preview_token")
        request_key = payload.get("request_key")
        if not isinstance(token, str) or not isinstance(request_key, str):
            raise ValueError("The send confirmation is incomplete.")
        try:
            uuid.UUID(request_key)
        except (ValueError, AttributeError):
            raise ValueError("A valid submission key is required.") from None
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            if self.closed:
                raise ValueError("The localhost app is stopping.")
            with closing(self.store._connect()) as db:
                existing = db.execute("SELECT * FROM web_sends WHERE request_key=?", (request_key,)).fetchone()
            if existing:
                if existing["preview_hash"] != token_hash:
                    raise ValueError("This submission key belongs to another message.")
                return {"operation_id": existing["operation_id"], "send_id": existing["id"]}
            preview = self.previews.get(token)
            if not preview or preview["expires_at"] <= time.time():
                raise ValueError("This preview expired or was used. Review the message again.")
            if self.pending >= 10:
                raise ValueError("Wait for the queued Viber operations to finish.")
            send_id = str(uuid.uuid4())
            operation_id = str(uuid.uuid4())
            lead = preview["lead"]
            with closing(self.store._connect()) as db, db:
                db.execute("INSERT INTO web_operations(id,kind,state,created_at) VALUES(?,'send','QUEUED',?)",
                           (operation_id, now()))
                db.execute("INSERT INTO web_sends VALUES(?,?,?,?,?,?,?,?,?,'QUEUED',?,?,NULL)",
                           (send_id, request_key, token_hash, operation_id, lead["id"], lead["phone"],
                            lead["company_name"], preview["viber_name"], preview["text"], now(), now()))
            del self.previews[token]
            self.pending += 1
            self.pool.submit(self._run, operation_id, lambda: self._send(send_id, preview))
            return {"operation_id": operation_id, "send_id": send_id}

    def _send(self, send_id, preview):
        submitting = False
        try:
            before = self.lead(preview["lead"]["id"])
            if asdict(before) != preview["lead"]:
                raise ViberError("The saved contact changed after review. No message was sent.")
            client, lead, name = self._verified(before.id)
            if name != preview["viber_name"] or not client.verify_current_name(name):
                raise ViberError("The Viber recipient changed after review. No message was sent.")
            with closing(self.store._connect()) as db, db:
                db.execute("UPDATE web_sends SET state='SUBMITTING', updated_at=? WHERE id=?", (now(), send_id))
            submitting = True
            client.send_message(preview["text"])
            with closing(self.store._connect()) as db, db:
                db.execute("UPDATE web_sends SET state='DISPATCHED', updated_at=? WHERE id=?", (now(), send_id))
            self.event("SEND_DISPATCHED", f"Send action dispatched for contact #{lead.id}: {name}. Delivery unverified.")
            return {"send_id": send_id, "state": "DISPATCHED", "viber_name": name}
        except Exception as exc:
            state = "UNKNOWN" if submitting else "BLOCKED"
            with closing(self.store._connect()) as db, db:
                db.execute("UPDATE web_sends SET state=?, error=?, updated_at=? WHERE id=?",
                           (state, str(exc), now(), send_id))
            raise

    def records(self, kind, limit=100, offset=0):
        table = {"sent": "web_sends", "events": "web_events", "inbox": "web_reads"}[kind]
        order = "captured_at" if kind == "inbox" else "created_at"
        with closing(self.store._connect()) as db:
            rows = db.execute(f"SELECT * FROM {table} ORDER BY {order} DESC, rowid DESC LIMIT ? OFFSET ?",
                              (limit, offset)).fetchall()
        result = [dict(row) for row in rows]
        for row in result:
            # Never return internal confirmation hashes or submission keys in lists.
            row.pop("preview_hash", None)
            row.pop("request_key", None)
            if kind == "inbox":
                row["messages"] = json.loads(row["messages"])
                lead = self.store.get(row["lead_id"])
                row["company_name"] = lead.company_name if lead else "Removed contact"
        return result

    def activity(self, days=30):
        if days not in {7, 30, 90}:
            raise ValueError("Choose 7, 30, or 90 days.")
        today = datetime.now().astimezone().date()
        totals = {str(today - timedelta(days=i)): {"dispatched": 0, "blocked": 0, "unknown": 0}
                  for i in range(days)}
        with closing(self.store._connect()) as db:
            rows = db.execute("SELECT state, created_at FROM web_sends WHERE created_at>=?",
                              ((datetime.now(timezone.utc) - timedelta(days=days + 1)).isoformat(),)).fetchall()
        for row in rows:
            day = str(datetime.fromisoformat(row["created_at"]).astimezone().date())
            state = row["state"].lower()
            if day in totals and state in totals[day]:
                totals[day][state] += 1
        return [{"date": day, **counts} for day, counts in sorted(totals.items(), reverse=True)]

    def diagnostics(self, inspect=False):
        def work():
            import importlib.metadata
            import subprocess
            checks = []
            for name in ("pywinauto", "Pillow", "pytesseract", "pywin32"):
                try:
                    version = importlib.metadata.version(name)
                except importlib.metadata.PackageNotFoundError:
                    version = None
                checks.append({"name": name, "ok": bool(version), "detail": version or "Install requirements.txt"})
            tesseract = shutil.which("tesseract")
            languages = ""
            if tesseract:
                result = subprocess.run([tesseract, "--list-langs"], capture_output=True, text=True, timeout=10,
                                        creationflags=0x08000000 if sys.platform == "win32" else 0)
                languages = result.stdout + result.stderr
            checks.append({"name": "Header recognition", "ok": bool(tesseract and "eng" in languages.split() and "srp_latn" in languages.split()),
                           "detail": "Tesseract with eng and srp_latn" if tesseract else "Tesseract not found on PATH"})
            try:
                adb = _find_adb()
                result = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=10,
                                        creationflags=0x08000000 if sys.platform == "win32" else 0)
                devices = [line for line in result.stdout.splitlines() if "\tdevice" in line]
                checks.append({"name": "Android contacts", "ok": len(devices) == 1,
                               "detail": f"{len(devices)} authorized devices. Android is only needed to add contacts."})
            except Exception as exc:
                checks.append({"name": "Android contacts", "ok": False, "detail": str(exc)})
            tree = None
            try:
                client = self.client_factory().connect()
                client._assert_no_focus_theft()
                checks.append({"name": "Viber Desktop", "ok": True, "detail": "Restored behind another app; background controls ready."})
                if inspect:
                    tree = "\n".join(f"{client.ui._type(node):16} name={client.ui._name(node)!r} auto_id={client.ui._auto_id(node)!r}"
                                     for node in client._nodes())
            except Exception as exc:
                checks.append({"name": "Viber Desktop", "ok": False, "detail": str(exc)})
            return {"checks": checks, "tree": tree}
        return self._enqueue("inspect" if inspect else "diagnostics", work)
