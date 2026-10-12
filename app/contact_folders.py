"""Account-scoped contact lists and safe removal of contact records."""
from contextlib import closing
import json
import uuid

from app.viber_inbox import utc_now


def remove_contacts(db, rows, account_id):
    """Called under the dispatch/service locks, or with the controller stopped."""
    ids = [r['id'] for r in rows]
    operations = []
    campaigns = set()
    for row in rows:
        phone = row['phone']
        db.execute('''INSERT INTO contact_removals(phone,account_id,removed_at) VALUES(?,?,?)
            ON CONFLICT(phone) DO UPDATE SET account_id=excluded.account_id,removed_at=excluded.removed_at''',
            (phone, account_id, utc_now()))
        campaigns.update(r[0] for r in db.execute('SELECT DISTINCT campaign_id FROM web_sends WHERE lead_id=? AND campaign_id IS NOT NULL', (row['id'],)))
        db.execute("UPDATE web_sends SET state='CANCELLED',error='Contact removed by owner.',updated_at=? WHERE lead_id=? AND attempted_at IS NULL AND state IN ('DRAFT','SCHEDULED','QUEUED')", (utc_now(), row['id']))
        db.execute("UPDATE auto_reply_jobs SET state='STALE',reason='Contact removed by owner.',updated_at=? WHERE phone=? AND state IN ('GENERATING','DRAFT','QUEUED','RETRY','REGENERATE','INTERRUPTED') AND NOT EXISTS(SELECT 1 FROM web_sends s WHERE s.id=auto_reply_jobs.send_id AND s.attempted_at IS NOT NULL)", (utc_now(), phone))
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='controller_jobs'").fetchone():
            pending = [r[0] for r in db.execute("SELECT operation_id FROM controller_jobs WHERE account_id=? AND phone=? AND state='PENDING'", (account_id, phone))]
            operations.extend(pending)
            db.execute("UPDATE controller_jobs SET state='INTERRUPTED',error='Contact removed by owner.',finished_at=? WHERE account_id=? AND phone=? AND state='PENDING'", (utc_now(), account_id, phone))
            for operation in pending:
                db.execute("UPDATE web_operations SET state='INTERRUPTED',error='Contact removed by owner.' WHERE id=? AND state='QUEUED'", (operation,))
        db.execute('UPDATE viber_conversations SET active=0,monitoring=0,revision=revision+1 WHERE phone=?', (phone,))
        for table in ('contact_descriptions', 'contact_outreach', 'contact_folder_members', 'web_reads'):
            db.execute('DELETE FROM ' + table + ' WHERE lead_id=?', (row['id'],))
        db.execute('DELETE FROM leads WHERE id=?', (row['id'],))
    for campaign in campaigns:
        waiting = db.execute("SELECT 1 FROM web_sends WHERE campaign_id=? AND state IN ('DRAFT','SCHEDULED','QUEUED','SUBMITTING')", (campaign,)).fetchone()
        db.execute("UPDATE web_campaigns SET state=?,reason='Contacts removed by owner; review remaining recipients.',revision=revision+1,updated_at=? WHERE id=? AND state IN ('DRAFT','ACTIVE','PAUSED')",
                   ('PAUSED' if waiting else 'CANCELLED', utc_now(), campaign))
    return {'deleted': len(ids), 'cancelled_operation_ids': operations}


class ContactFolders:
    def __init__(self, service):
        self.service = service
        self.main_id = 'main:' + service.account_id
        with closing(service.store._connect()) as db, db:
            db.executescript('''CREATE TABLE IF NOT EXISTS contact_folders (
                id TEXT PRIMARY KEY, account_id TEXT NOT NULL, name TEXT NOT NULL,
                normalized_name TEXT NOT NULL, is_main INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, UNIQUE(account_id,normalized_name));
                CREATE TABLE IF NOT EXISTS contact_folder_members (
                account_id TEXT NOT NULL, lead_id INTEGER NOT NULL, folder_id TEXT NOT NULL,
                PRIMARY KEY(account_id,lead_id));''')
            db.execute('INSERT OR IGNORE INTO contact_folders VALUES(?,?,?,?,1,?)',
                       (self.main_id, service.account_id, 'Main', 'main', utc_now()))

    def require(self, db, folder_id=None):
        folder_id = folder_id or self.main_id
        row = db.execute('SELECT * FROM contact_folders WHERE id=? AND account_id=?',
                         (folder_id, self.service.account_id)).fetchone()
        if not row:
            raise ValueError('Choose a contact folder belonging to this account.')
        return dict(row)

    def members(self, db):
        return {r['lead_id']: r['folder_id'] for r in db.execute(
            'SELECT lead_id,folder_id FROM contact_folder_members WHERE account_id=?', (self.service.account_id,))}

    def list(self):
        with closing(self.service.store._connect()) as db:
            folders = [dict(r) for r in db.execute('SELECT * FROM contact_folders WHERE account_id=? ORDER BY is_main DESC,normalized_name,id', (self.service.account_id,))]
            members = self.members(db)
            query = 'SELECT id FROM leads'
            args = ()
            if self.service.accounts:
                query += ' WHERE phone IN (SELECT phone FROM contact_owners WHERE account_id=?)'
                args = (self.service.account_id,)
            counts = {}
            for row in db.execute(query, args):
                folder = members.get(row[0], self.main_id)
                counts[folder] = counts.get(folder, 0) + 1
            return [{**folder, 'contact_count': counts.get(folder['id'], 0)} for folder in folders]

    def create(self, name):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100 or any(ord(c) < 32 for c in name):
            raise ValueError('Enter a folder name containing 1 to 100 characters.')
        name = ' '.join(name.split())
        with self.service.lock, closing(self.service.store._connect()) as db, db:
            if self.service.closed:
                raise ValueError('The local app is stopping.')
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM contact_folders WHERE account_id=? AND normalized_name=?', (self.service.account_id, name.casefold())).fetchone():
                raise ValueError('A contact folder with this name already exists.')
            folder_id = str(uuid.uuid4())
            db.execute('INSERT INTO contact_folders VALUES(?,?,?,?,0,?)', (folder_id, self.service.account_id, name, name.casefold(), utc_now()))
            return self.require(db, folder_id)

    def assign(self, db, lead_id, folder_id=None):
        folder = self.require(db, folder_id)
        db.execute('''INSERT INTO contact_folder_members VALUES(?,?,?) ON CONFLICT(account_id,lead_id)
            DO UPDATE SET folder_id=excluded.folder_id''', (self.service.account_id, lead_id, folder['id']))

    def move(self, lead_id, folder_id):
        with self.service.lock, closing(self.service.store._connect()) as db, db:
            if self.service.closed:
                raise ValueError('The local app is stopping.')
            db.execute('BEGIN IMMEDIATE')
            lead = self.service.lead(lead_id)
            self.assign(db, lead.id, folder_id)
        return {'lead_id': lead.id, 'folder_id': folder_id}

    def delete(self, folder_id):
        with self.service.lock, closing(self.service.store._connect()) as db, db:
            if self.service.closed:
                raise ValueError('The local app is stopping.')
            db.execute('BEGIN IMMEDIATE')
            folder = self.require(db, folder_id)
            if folder['is_main']:
                raise ValueError('The Main contact folder cannot be deleted.')
            if db.execute('SELECT 1 FROM contact_folder_members m JOIN leads l ON l.id=m.lead_id WHERE m.folder_id=? AND m.account_id=?', (folder_id, self.service.account_id)).fetchone():
                raise ValueError('Move this folder\'s contacts to another folder before deleting it.')
            db.execute('DELETE FROM contact_folders WHERE id=? AND account_id=?', (folder_id, self.service.account_id))
        return {'deleted': True, 'folder_id': folder_id}
