"""Reviewed campaign batches, local sending windows, and durable controls."""

from collections import Counter
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timedelta, timezone, time as day_time
import hashlib
import json
import re
import secrets
import time
import uuid
from zoneinfo import ZoneInfo

from app.scheduling import parse_schedule, TIME_ZONE


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def integer(value, label, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}.")
    return value


def window_time(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("Use HH:MM for the sending window.")
    return day_time.fromisoformat(value)


def campaign_slots(start, count, rules, used=None, last_attempt=None):
    """Space sends in UTC, with daily limits and windows in Europe/Warsaw."""
    zone = ZoneInfo(TIME_ZONE)
    interval = timedelta(minutes=rules["interval_minutes"])
    opening, closing = window_time(rules["window_start"]), window_time(rules["window_end"])
    if opening >= closing:
        raise ValueError("The sending window must end after it starts, on the same day.")
    cursor = datetime.fromisoformat(start).astimezone(timezone.utc)
    if last_attempt:
        cursor = max(cursor, datetime.fromisoformat(last_attempt) + interval)
    counts = Counter(used or {})
    slots = []
    while len(slots) < count:
        local = cursor.astimezone(zone)
        day = local.date()
        clock = local.time()
        if clock >= closing or counts[str(day)] >= rules["daily_cap"]:
            day += timedelta(days=1)
            cursor = datetime.combine(day, opening, zone).astimezone(timezone.utc)
            continue
        if clock < opening:
            cursor = datetime.combine(day, opening, zone).astimezone(timezone.utc)
            continue
        # A skipped window start normalizes forward through the DST gap.
        slots.append(cursor.isoformat(timespec="seconds"))
        counts[str(day)] += 1
        cursor += interval
    return slots


def same_recipient(expected, actual):
    """Learning a previously unknown Viber name must not rename the business."""
    actual = dict(actual)
    if expected.get("viber_name") is None:
        actual["viber_name"] = None
    return expected == actual


def expand_message(template, lead):
    values = {"company": lead.company_name, "phone": lead.phone,
              "name": lead.viber_name or lead.company_name, "viber_name": lead.viber_name}
    def replace(match):
        key = match.group(1).strip()
        if key not in values:
            raise ValueError(f"Unknown message variable: {key}.")
        if values[key] is None:
            raise ValueError(f"Contact #{lead.id} has no saved Viber name. Use {{{{name}}}} for a business-name fallback.")
        return values[key]
    return re.sub(r"\{\{(.*?)\}\}", replace, template)


def init_campaign_schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS web_campaigns (
            id TEXT PRIMARY KEY, creation_key TEXT NOT NULL UNIQUE, creation_hash TEXT NOT NULL,
            name TEXT NOT NULL, template TEXT NOT NULL, rules TEXT NOT NULL,
            state TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, reason TEXT,
            duplicates_skipped INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS web_campaign_requests (
            request_key TEXT PRIMARY KEY, preview_hash TEXT NOT NULL, campaign_id TEXT NOT NULL);
    """)
    columns = {row[1] for row in db.execute("PRAGMA table_info(web_sends)")}
    for column in ("campaign_id", "attempted_at"):
        if column not in columns:
            db.execute(f"ALTER TABLE web_sends ADD COLUMN {column} TEXT")
    campaign_columns = {row[1] for row in db.execute("PRAGMA table_info(web_campaigns)")}
    if "acknowledged_errors" not in campaign_columns:
        db.execute("ALTER TABLE web_campaigns ADD COLUMN acknowledged_errors TEXT NOT NULL DEFAULT '[]'")
    if 'account_id' not in campaign_columns:
        db.execute("ALTER TABLE web_campaigns ADD COLUMN account_id TEXT NOT NULL DEFAULT 'current'")
    db.execute("CREATE INDEX IF NOT EXISTS web_sends_campaign ON web_sends(campaign_id,state)")
    db.execute("CREATE INDEX IF NOT EXISTS web_sends_due ON web_sends(state,scheduled_at)")


class CampaignManager:
    def __init__(self, service):
        self.service = service
        self.previews = {}

    def _event(self, db, kind, detail):
        db.execute("INSERT INTO web_events(created_at,kind,detail,account_id) VALUES(?,?,?,?)", (utc_now(), kind, detail,self.service.account_id))

    def _get(self, db, campaign_id):
        if not isinstance(campaign_id, str):
            raise ValueError("Choose a campaign.")
        row = db.execute("SELECT * FROM web_campaigns WHERE id=?", (campaign_id,)).fetchone()
        if not row:
            raise ValueError("Campaign not found.")
        if self.service.managed and row['account_id'] != self.service.account_id:
            raise PermissionError('Campaign belongs to another account.')
        return dict(row)

    @staticmethod
    def _key(value):
        if not isinstance(value, str):
            raise ValueError("A submission key is required.")
        try:
            uuid.UUID(value)
        except ValueError:
            raise ValueError("A valid submission key is required.") from None
        return value

    def create(self, payload):
        from app.web_service import validate_message
        request_key = self._key(payload.get("request_key"))
        fingerprint = hashlib.sha256(json.dumps({k: v for k, v in payload.items() if k != "request_key"},
                                               sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.service.lock, closing(self.service.store._connect()) as db, db:
            if self.service.closed:
                raise ValueError("The localhost app is stopping.")
            existing = db.execute("SELECT * FROM web_campaigns WHERE creation_key=?", (request_key,)).fetchone()
            if existing:
                self._get(db, existing['id'])
                if existing["creation_hash"] != fingerprint:
                    raise ValueError("This submission key belongs to another campaign.")
                return self._detail(db, dict(existing))
            name = payload.get("name")
            if not isinstance(name, str) or not name.strip() or len(name) > 120 or any(ord(c) < 32 for c in name):
                raise ValueError("Enter a campaign name containing 1 to 120 characters.")
            template = validate_message(payload.get("text"))
            ids = payload.get("lead_ids")
            if not isinstance(ids, list) or not 1 <= len(ids) <= 100:
                raise ValueError("Select between 1 and 100 contacts.")
            leads, phones = [], set()
            for lead_id in ids:
                lead = self.service.lead(lead_id)
                if lead.phone not in phones:
                    leads.append(lead)
                    phones.add(lead.phone)
            timing = parse_schedule(payload.get("schedule"))
            if not timing:
                raise ValueError("Choose the campaign start time.")
            rules = {"start_at": timing["scheduled_at"], "time_zone": TIME_ZONE,
                     "interval_minutes": integer(payload.get("interval_minutes", 5), "Interval", 1, 1440),
                     "daily_cap": integer(payload.get("daily_cap", 100), "Daily cap", 1, 100),
                     "window_start": payload.get("window_start", "09:00"),
                     "window_end": payload.get("window_end", "17:00")}
            slots = campaign_slots(rules["start_at"], len(leads), rules)
            self._check_horizon(slots)
            messages = [validate_message(expand_message(template, lead)) for lead in leads]
            campaign_id = str(uuid.uuid4())
            db.execute("INSERT INTO web_campaigns(id,creation_key,creation_hash,name,template,rules,state,created_at,updated_at,duplicates_skipped,account_id) "
                       "VALUES(?,?,?,?,?,?,'DRAFT',?,?,?,?)", (campaign_id, request_key, fingerprint, name.strip(), template,
                       json.dumps(rules), utc_now(), utc_now(), len(ids) - len(leads),self.service.account_id))
            for lead, text, slot in zip(leads, messages, slots):
                send_id = str(uuid.uuid4())
                db.execute("INSERT INTO web_sends(id,request_key,preview_hash,operation_id,lead_id,phone,company_name,"
                           "viber_name,text,state,created_at,updated_at,scheduled_at,lead_snapshot,campaign_id) "
                           "VALUES(?,?,?,?,?,?,?,?,?,'DRAFT',?,?,?,?,?)",
                           (send_id, str(uuid.uuid4()), fingerprint, "", lead.id, lead.phone, lead.company_name,
                            lead.viber_name or "", text, utc_now(), utc_now(), slot, json.dumps(asdict(lead), ensure_ascii=False), campaign_id))
            self._event(db, "CAMPAIGN_DRAFT", f"Created {name.strip()}: {len(leads)} unique recipients.")
            return self._detail(db, self._get(db, campaign_id))

    @staticmethod
    def _check_horizon(slots):
        if datetime.fromisoformat(slots[-1]) > datetime.now(timezone.utc) + timedelta(days=365):
            raise ValueError("The campaign would finish more than 365 days ahead. Choose an earlier start or increase the daily cap.")

    def _summary(self, db, campaign):
        result = {key: value for key, value in campaign.items() if key not in {"creation_key", "creation_hash", "acknowledged_errors"}}
        result["rules"] = json.loads(result["rules"])
        result["counts"] = dict(db.execute("SELECT state,count(*) FROM web_sends WHERE campaign_id=? GROUP BY state", (campaign["id"],)).fetchall())
        result["total"] = sum(result["counts"].values())
        row = db.execute("SELECT min(scheduled_at),max(scheduled_at) FROM web_sends WHERE campaign_id=? AND state IN ('DRAFT','SCHEDULED')",
                         (campaign["id"],)).fetchone()
        result["next_at"], result["last_at"] = row
        return result

    def _detail(self, db, campaign):
        result = self._summary(db, campaign)
        result["recipients"] = [dict(row) for row in db.execute(
            "SELECT id,lead_id,phone,company_name,viber_name,text,state,scheduled_at,error FROM web_sends WHERE campaign_id=? ORDER BY scheduled_at,rowid",
            (campaign["id"],))]
        return result

    def list(self):
        with closing(self.service.store._connect()) as db:
            return [self._summary(db, dict(row)) for row in db.execute("SELECT * FROM web_campaigns" + (" WHERE account_id=?" if self.service.managed else '') + " ORDER BY created_at DESC,rowid DESC LIMIT 100",(self.service.account_id,) if self.service.managed else ())]

    def detail(self, campaign_id):
        with closing(self.service.store._connect()) as db:
            return self._detail(db, self._get(db, campaign_id))

    def _usage(self, db, campaign_id):
        attempts = db.execute("SELECT attempted_at FROM web_sends WHERE campaign_id=? AND attempted_at IS NOT NULL ORDER BY attempted_at",
                              (campaign_id,)).fetchall()
        zone = ZoneInfo(TIME_ZONE)
        counts = Counter(str(datetime.fromisoformat(row[0]).astimezone(zone).date()) for row in attempts)
        return counts, attempts[-1][0] if attempts else None

    def review(self, campaign_id):
        with self.service.lock, closing(self.service.store._connect()) as db:
            campaign = self._get(db, campaign_id)
            if campaign["state"] not in {"DRAFT", "PAUSED"}:
                raise ValueError("Only a draft or paused campaign can be reviewed for activation.")
            if db.execute("SELECT 1 FROM web_sends WHERE campaign_id=? AND state IN ('QUEUED','SUBMITTING') LIMIT 1", (campaign_id,)).fetchone():
                raise ValueError("Wait for the active desktop action to finish before reviewing the remaining campaign.")
            rows = db.execute("SELECT * FROM web_sends WHERE campaign_id=? AND state IN ('DRAFT','SCHEDULED') ORDER BY scheduled_at,rowid", (campaign_id,)).fetchall()
            if not rows:
                raise ValueError("This campaign has no unsent recipients left. Failed or uncertain messages are not retried.")
            rules = json.loads(campaign["rules"])
            start = max(datetime.fromisoformat(rules["start_at"]), datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(timespec="seconds")
            used, last = self._usage(db, campaign_id)
            slots = campaign_slots(start, len(rows), rules, used, last)
            self._check_horizon(slots)
            recipients = []
            for row, slot in zip(rows, slots):
                expected = json.loads(row["lead_snapshot"])
                lead = self.service.lead(row["lead_id"])
                if not same_recipient(expected, asdict(lead)):
                    raise ValueError(f"Contact #{lead.id} changed. Create a new draft for the updated recipient.")
                recipients.append({"send_id": row["id"], "lead": asdict(lead), "viber_name": lead.viber_name or "",
                                   "text": row["text"], "scheduled_at": slot})
            token = secrets.token_urlsafe(32)
            preview = {"token": token, "campaign_id": campaign_id, "revision": campaign["revision"], "state": campaign["state"],
                       "name": campaign["name"], "rules": rules, "recipients": recipients, "expires_at": time.time() + 600}
            self.previews = {key: value for key, value in self.previews.items() if value["expires_at"] > time.time()}
            self.previews[token] = preview
            return preview

    def activate(self, payload):
        if payload.get("confirmed") is not True:
            raise ValueError("Review every recipient, message, and schedule, then confirm the campaign.")
        request_key = self._key(payload.get("request_key"))
        token = payload.get("preview_token")
        if not isinstance(token, str):
            raise ValueError("Review the campaign before activation.")
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.service.lock, closing(self.service.store._connect()) as db, db:
            if self.service.closed:
                raise ValueError("The localhost app is stopping.")
            existing = db.execute("SELECT * FROM web_campaign_requests WHERE request_key=?", (request_key,)).fetchone()
            if existing:
                if existing["preview_hash"] != digest:
                    raise ValueError("This confirmation belongs to another campaign review.")
                return self._detail(db, self._get(db, existing["campaign_id"]))
            preview = self.previews.get(token)
            if not preview or preview["expires_at"] <= time.time():
                raise ValueError("This campaign review expired. Review it again.")
            campaign = self._get(db, preview["campaign_id"])
            if campaign["state"] != preview["state"] or campaign["revision"] != preview["revision"]:
                raise ValueError("The campaign changed after review. Review it again.")
            if datetime.fromisoformat(preview["recipients"][0]["scheduled_at"]).timestamp() <= time.time():
                raise ValueError("The reviewed start time passed. Review the remaining schedule again.")
            db.execute("BEGIN IMMEDIATE")
            for item in preview["recipients"]:
                if asdict(self.service.lead(item["lead"]["id"])) != item["lead"]:
                    raise ValueError("A contact changed after review. Review the campaign again.")
                row = db.execute("UPDATE web_sends SET state='SCHEDULED',scheduled_at=?,lead_snapshot=?,viber_name=?,updated_at=? "
                                 "WHERE id=? AND campaign_id=? AND state IN ('DRAFT','SCHEDULED')",
                                 (item["scheduled_at"], json.dumps(item["lead"], ensure_ascii=False), item["viber_name"], utc_now(), item["send_id"], campaign["id"]))
                if row.rowcount != 1:
                    raise ValueError("A campaign recipient changed after review. Review again.")
            acknowledged = [row[0] for row in db.execute("SELECT id FROM web_sends WHERE campaign_id=? AND state IN ('UNKNOWN','BLOCKED','MISSED')", (campaign["id"],))]
            db.execute("UPDATE web_campaigns SET state='ACTIVE',revision=revision+1,reason=NULL,acknowledged_errors=?,updated_at=? WHERE id=?", (json.dumps(acknowledged), utc_now(), campaign["id"]))
            db.execute("INSERT INTO web_campaign_requests VALUES(?,?,?)", (request_key, digest, campaign["id"]))
            self._event(db, "CAMPAIGN_ACTIVE", f"Scheduled {len(preview['recipients'])} messages for {campaign['name']}.")
            del self.previews[token]
            return self._detail(db, self._get(db, campaign["id"]))

    def control(self, campaign_id, action):
        with self.service.lock, closing(self.service.store._connect()) as db, db:
            campaign = self._get(db, campaign_id)
            if action == "pause":
                if campaign["state"] != "ACTIVE":
                    raise ValueError("Only an active campaign can be paused.")
                state = "PAUSED"
            elif action == "cancel":
                if campaign["state"] not in {"DRAFT", "ACTIVE", "PAUSED"}:
                    raise ValueError("This campaign has already ended.")
                state = "CANCELLED"
                db.execute("UPDATE web_sends SET state='CANCELLED',updated_at=? WHERE campaign_id=? AND state IN ('DRAFT','SCHEDULED')", (utc_now(), campaign_id))
            else:
                raise ValueError("Unknown campaign control.")
            db.execute("UPDATE web_campaigns SET state=?,revision=revision+1,updated_at=? WHERE id=?", (state, utc_now(), campaign_id))
            self._event(db, "CAMPAIGN_" + state, f"{campaign['name']}: {state}. An already active desktop action may finish.")
            return self._detail(db, self._get(db, campaign_id))

    def can_dispatch(self, db, campaign_id, current):
        campaign = self._get(db, campaign_id)
        if campaign["state"] != "ACTIVE":
            return False
        rules = json.loads(campaign["rules"])
        local = current.astimezone(ZoneInfo(TIME_ZONE))
        used, last = self._usage(db, campaign_id)
        return (window_time(rules["window_start"]) <= local.time() < window_time(rules["window_end"]) and
                used[str(local.date())] < rules["daily_cap"] and
                (not last or current >= datetime.fromisoformat(last) + timedelta(minutes=rules["interval_minutes"])))

    def reconcile(self, db, campaign_id, replan=False):
        campaign = self._get(db, campaign_id)
        if campaign["state"] != "ACTIVE":
            return
        acknowledged = set(json.loads(campaign["acknowledged_errors"]))
        bad = next((row for row in db.execute("SELECT id,state FROM web_sends WHERE campaign_id=? AND state IN ('UNKNOWN','BLOCKED','MISSED')", (campaign_id,)) if row["id"] not in acknowledged), None)
        waiting = db.execute("SELECT id,scheduled_at FROM web_sends WHERE campaign_id=? AND state IN ('SCHEDULED','QUEUED','SUBMITTING') ORDER BY scheduled_at,rowid", (campaign_id,)).fetchall()
        if bad or not waiting:
            state = "PAUSED" if bad else "COMPLETED"
            reason = f"A message is {bad['state']}. Check its outcome before resuming the remaining recipients." if bad else None
            db.execute("UPDATE web_campaigns SET state=?,reason=?,revision=revision+1,updated_at=? WHERE id=?", (state, reason, utc_now(), campaign_id))
            self._event(db, "CAMPAIGN_" + state, f"{campaign['name']}: {reason or 'Completed.'}")
        elif replan:
            rows = db.execute("SELECT id,scheduled_at FROM web_sends WHERE campaign_id=? AND state='SCHEDULED' ORDER BY scheduled_at,rowid", (campaign_id,)).fetchall()
            if rows:
                rules = json.loads(campaign["rules"])
                start = max(datetime.fromisoformat(rows[0]["scheduled_at"]), datetime.now(timezone.utc)).isoformat(timespec="seconds")
                used, last = self._usage(db, campaign_id)
                for row, slot in zip(rows, campaign_slots(start, len(rows), rules, used, last)):
                    db.execute("UPDATE web_sends SET scheduled_at=? WHERE id=?", (slot, row["id"]))

    def after_operation(self, operation_id):
        with self.service.lock, closing(self.service.store._connect()) as db, db:
            row = db.execute("SELECT campaign_id FROM web_sends WHERE operation_id=? AND campaign_id IS NOT NULL", (operation_id,)).fetchone()
            if row:
                self.reconcile(db, row[0], replan=True)
