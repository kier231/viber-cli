"""Private, account-scoped owner context without changing recipient snapshots."""
from contextlib import closing
import json

from app.viber_inbox import utc_now


class ClientDescriptions:
    def __init__(self, service):
        self.service = service
        with closing(service.store._connect()) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS contact_descriptions (
                account_id TEXT NOT NULL, lead_id INTEGER NOT NULL,
                description TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL, PRIMARY KEY(account_id,lead_id))''')

    def get(self, db, lead_id):
        row = db.execute('SELECT description,revision FROM contact_descriptions WHERE account_id=? AND lead_id=?',
                         (self.service.account_id, lead_id)).fetchone()
        return dict(row) if row else {'description': '', 'revision': 0}

    def all(self):
        with closing(self.service.store._connect()) as db:
            return {r['lead_id']: dict(r) for r in db.execute(
                'SELECT lead_id,description,revision FROM contact_descriptions WHERE account_id=?',
                (self.service.account_id,))}

    def save(self, lead_id, description, revision):
        if not isinstance(description, str) or len(description) > 4000:
            raise ValueError('Opis klijenta može imati najviše 4.000 znakova.')
        if any((ord(c) < 32 and c not in '\n\r\t') or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in description):
            raise ValueError('Opis klijenta sadrži nedozvoljene znakove.')
        if type(revision) is not int or revision < 0:
            raise ValueError('Osvežite kontakte pre čuvanja opisa.')
        description = description.strip()
        # Saving a note must never race the final Send click, or touch Viber.
        with self.service.replies.dispatch_lock, closing(self.service.store._connect()) as db, db:
            if self.service.closed:
                raise ValueError('The localhost app is stopping.')
            lead = self.service.lead(lead_id)
            db.execute('BEGIN IMMEDIATE')
            if self.service.accounts:
                owner = db.execute('SELECT account_id FROM contact_owners WHERE phone=?', (lead.phone,)).fetchone()
                if not owner or owner[0] != self.service.account_id:
                    raise PermissionError('This contact belongs to another Viber account.')
            current = self.get(db, lead_id)
            if current['revision'] != revision:
                raise ValueError('Opis je promenjen u drugoj kartici. Osvežite kontakte pre čuvanja.')
            if current['description'] == description:
                return {'lead_id': lead_id, **current}
            updated = revision + 1
            db.execute('''INSERT INTO contact_descriptions(account_id,lead_id,description,revision,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(account_id,lead_id) DO UPDATE SET
                description=excluded.description,revision=excluded.revision,updated_at=excluded.updated_at''',
                (self.service.account_id, lead_id, description, updated, utc_now()))
            # Rebuild only unsent review drafts; a saved note is never a new turn.
            scope = ' AND source_id IN (SELECT source_id FROM account_sources WHERE account_id=?)' if self.service.managed else ''
            params = (lead.phone, self.service.account_id) if self.service.managed else (lead.phone,)
            for row in db.execute("SELECT id,lead_snapshot FROM auto_reply_jobs WHERE state='DRAFT' AND phone=?" + scope, params).fetchall():
                if json.loads(row['lead_snapshot'])['id'] == lead_id:
                    db.execute("UPDATE auto_reply_jobs SET state='REGENERATE',text='',approved_at=NULL,draft_ready_at=NULL,updated_at=? WHERE id=? AND state='DRAFT'",
                               (utc_now(), row['id']))
        self.service.replies.wake.set()
        return {'lead_id': lead_id, 'description': description, 'revision': updated}
