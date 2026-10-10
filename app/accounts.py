"""Exclusive recipient ownership and fenced account worker registrations."""
from contextlib import closing
from datetime import datetime, timezone, timedelta
import hashlib
import hmac
import secrets
import socket
import os
import uuid

from app.storage import connect
from app.viber_database import international_phone


def stamp(offset=0):
    return (datetime.now(timezone.utc)+timedelta(seconds=offset)).isoformat(timespec='microseconds')


class Accounts:
    def __init__(self, target, account_id='current'):
        self.target = target
        self.account_id = account_id
        self.worker_id = str(uuid.uuid4())
        self.token = secrets.token_urlsafe(32)
        self.epoch = str(uuid.uuid4())
        with closing(connect(target)) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY, phone TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL DEFAULT 0,
                    paused INTEGER NOT NULL DEFAULT 0, source_id TEXT, driver TEXT NOT NULL DEFAULT 'vm',
                    send_gap_seconds INTEGER NOT NULL DEFAULT 0, next_send_at TEXT,
                    monitoring_since_ms INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS contact_owners (
                    phone TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
                    claimed_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS account_sources (
                    source_id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id));
                CREATE TABLE IF NOT EXISTS account_workers (
                    account_id TEXT PRIMARY KEY REFERENCES accounts(id), worker_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL, epoch TEXT NOT NULL, lease_until TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL);
            ''')
            columns = {r[1] for r in db.execute('PRAGMA table_info(account_workers)')}
            for name,kind in (('machine','TEXT'),('process_id','INTEGER'),('process_started','TEXT'),('stopped_at','TEXT')):
                if name not in columns:
                    db.execute(f'ALTER TABLE account_workers ADD COLUMN {name} {kind}')

    def bind_current(self):
        with closing(connect(self.target)) as db, db:
            old = db.execute('SELECT * FROM account_workers WHERE account_id=?',(self.account_id,)).fetchone()
            if old and not old['stopped_at']:
                import psutil
                if old['machine'] != socket.gethostname() or not old['process_id'] or not old['process_started']:
                    raise PermissionError('Previous worker process must be verified stopped before activation.')
                try:
                    process = psutil.Process(old['process_id'])
                    if str(process.create_time())==old['process_started'] and process.is_running():
                        raise PermissionError('Previous worker process is still running; replacement is blocked.')
                except psutil.NoSuchProcess:
                    pass
            row = db.execute('SELECT * FROM viber_sources ORDER BY last_poll DESC LIMIT 1').fetchone()
            if not row:
                raise ValueError('Import the current account identity before activating its worker.')
            phone = international_phone(row['account_phone'])
            if not phone:
                raise ValueError('Current Viber account identity is invalid.')
            existing = db.execute('SELECT * FROM accounts WHERE id=?', (self.account_id,)).fetchone()
            if existing and existing['phone'] != phone:
                raise ValueError('Viber identity changed; account activation stopped.')
            db.execute('INSERT OR IGNORE INTO accounts(id,phone,enabled,source_id,created_at,monitoring_since_ms) VALUES(?,?,1,?,?,?)',
                (self.account_id,phone,row['source_id'],stamp(),int(datetime.now(timezone.utc).timestamp()*1000)))
            db.execute('UPDATE accounts SET enabled=0 WHERE id!=?',(self.account_id,))
            for source in db.execute('SELECT source_id,account_phone FROM viber_sources'):
                if international_phone(source['account_phone']) == phone:
                    db.execute('INSERT OR IGNORE INTO account_sources VALUES(?,?)',(source['source_id'],self.account_id))
            for lead in db.execute('SELECT phone FROM leads'):
                self.claim(lead['phone'], self.account_id, db=db)
        # Called only after the controller's lifetime advisory lock is acquired.
        self.register(self.account_id,self.worker_id,self.token,self.epoch,confirmed_stopped=True)

    def bind_snapshot(self, snapshot):
        """Activate only this slot, from its authenticated native Viber identity."""
        phone = international_phone(snapshot.get('account_phone'))
        source = snapshot.get('source_id')
        if snapshot.get('unchanged') or not phone or not isinstance(source, str) or not source:
            raise ValueError('Link a new number before activating this worker.')
        with closing(connect(self.target)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM account_workers WHERE account_id=?',(self.account_id,)).fetchone()
            if old and not old['stopped_at']:
                import psutil
                if old['machine'] != socket.gethostname() or not old['process_id'] or not old['process_started']:
                    raise PermissionError('Previous worker must be confirmed stopped.')
                try:
                    process = psutil.Process(old['process_id'])
                    if str(process.create_time()) == old['process_started'] and process.is_running():
                        raise PermissionError('Previous worker is still running.')
                except psutil.NoSuchProcess:
                    pass
            existing = db.execute('SELECT * FROM accounts WHERE id=?',(self.account_id,)).fetchone()
            if existing and (existing['phone'] != phone or existing['source_id'] != source):
                raise PermissionError('This slot is already bound to a different Viber identity.')
            owner = db.execute('SELECT id FROM accounts WHERE phone=?',(phone,)).fetchone()
            source_owner = db.execute('SELECT account_id FROM account_sources WHERE source_id=?',(source,)).fetchone()
            if (owner and owner[0] != self.account_id) or (source_owner and source_owner[0] != self.account_id):
                raise PermissionError('This number or Viber profile already belongs to another account.')
            db.execute('INSERT OR IGNORE INTO accounts(id,phone,enabled,source_id,created_at,monitoring_since_ms) VALUES(?,?,1,?,?,?)',
                       (self.account_id,phone,source,stamp(),int(datetime.now(timezone.utc).timestamp()*1000)))
            db.execute('UPDATE accounts SET enabled=1,paused=0 WHERE id=?',(self.account_id,))
            db.execute('INSERT OR IGNORE INTO account_sources VALUES(?,?)',(source,self.account_id))
        self.register(self.account_id,self.worker_id,self.token,self.epoch,confirmed_stopped=True)

    def list(self):
        with closing(connect(self.target)) as db:
            return [dict(r) for r in db.execute('SELECT a.*,w.heartbeat_at,w.lease_until FROM accounts a LEFT JOIN account_workers w ON w.account_id=a.id ORDER BY a.id')]

    def claim(self, phone, account_id=None, db=None):
        phone = international_phone(phone)
        if not phone:
            raise ValueError('A verified international phone number is required.')
        if db is None:
            with closing(connect(self.target)) as db, db:
                return self.claim(phone,account_id,db)
        if account_id is None:
            existing = db.execute('SELECT account_id FROM contact_owners WHERE phone=?',(phone,)).fetchone()
            if existing:
                return existing[0]
            rows = db.execute('SELECT a.id,COUNT(o.phone) AS owned FROM accounts a LEFT JOIN contact_owners o ON o.account_id=a.id WHERE a.enabled=1 AND a.paused=0 GROUP BY a.id ORDER BY COUNT(o.phone),a.id').fetchall()
            if not rows:
                raise ValueError('No enabled account can claim this contact.')
            # Queue load takes precedence over owned-contact count.
            loads = [(db.execute("SELECT COUNT(*) FROM controller_jobs WHERE account_id=? AND state IN ('PENDING','RUNNING')",(r['id'],)).fetchone()[0], r['owned'],r['id']) for r in rows]
            account_id = min(loads)[2]
        if not db.execute('SELECT 1 FROM accounts WHERE id=? AND enabled=1', (account_id,)).fetchone():
            raise ValueError('The owning account is disabled.')
        db.execute('INSERT OR IGNORE INTO contact_owners VALUES(?,?,?)',(phone,account_id,stamp()))
        # Serialize creation with incoming-sender ingestion for this same number.
        lock = ' FOR UPDATE' if getattr(db,'dialect',None)=='postgresql' else ''
        owner = db.execute('SELECT account_id FROM contact_owners WHERE phone=?' + lock,(phone,)).fetchone()[0]
        if owner != account_id:
            raise PermissionError('This phone number belongs to another Viber account.')
        return owner

    def assert_phone(self, phone, account_id=None):
        account_id = account_id or self.account_id
        with closing(connect(self.target)) as db:
            row = db.execute('SELECT o.account_id,a.enabled,a.paused FROM contact_owners o JOIN accounts a ON a.id=o.account_id WHERE o.phone=?',(international_phone(phone),)).fetchone()
            if not row or row['account_id'] != account_id or not row['enabled'] or row['paused']:
                raise PermissionError('Recipient ownership or account state blocks this operation.')
        self.authenticate(self.token,account_id,self.worker_id,self.epoch)

    def source(self, snapshot):
        with closing(connect(self.target)) as db, db:
            account = db.execute('SELECT * FROM accounts WHERE id=?',(self.account_id,)).fetchone()
            if snapshot.get('unchanged'):
                known = db.execute('SELECT account_id FROM account_sources WHERE source_id=?',(snapshot['source_id'],)).fetchone()
                if not known or known[0] != self.account_id:
                    raise PermissionError('Unknown Viber source identity.')
            elif international_phone(snapshot.get('account_phone')) != account['phone']:
                raise PermissionError('The VM is signed into a different Viber account.')
            else:
                db.execute('INSERT OR IGNORE INTO account_sources VALUES(?,?)',(snapshot['source_id'],self.account_id))
                owner = db.execute('SELECT account_id FROM account_sources WHERE source_id=?',(snapshot['source_id'],)).fetchone()
                if owner[0] != self.account_id:
                    raise PermissionError('Viber source belongs to another account.')
            # A worker is bound to its source; another linked profile cannot feed it.
            if snapshot['source_id'] != account['source_id']:
                raise PermissionError('Viber profile changed; rebind it explicitly before proceeding.')
            allowed=set()
            for chat in snapshot.get('chats',[]):
                chat['phone']=international_phone(chat['phone'])
                owner=db.execute('SELECT account_id FROM contact_owners WHERE phone=?',(chat['phone'],)).fetchone()
                if owner and owner[0]!=self.account_id:
                    continue
                self.claim(chat['phone'],self.account_id,db)
                allowed.add(chat['chat_id'])
                if not db.execute('SELECT 1 FROM leads WHERE phone=?',(chat['phone'],)).fetchone():
                    row = db.execute("INSERT INTO leads(phone,company_name,viber_name,contact_name,created_at) VALUES(?,?,?,?,?)",(chat['phone'],chat['phone'],chat['viber_name'],chat['viber_name'],stamp()))
                    db.execute('UPDATE leads SET contact_name=? WHERE id=?',(f"{chat['viber_name']} | SJT-{row.lastrowid}",row.lastrowid))
            if not snapshot.get('unchanged'):
                snapshot['chats']=[c for c in snapshot['chats'] if c['chat_id'] in allowed]
                snapshot['messages']=[m for m in snapshot['messages'] if m['chat_id'] in allowed]

    def phones(self):
        with closing(connect(self.target)) as db:
            return {r[0] for r in db.execute('SELECT phone FROM contact_owners WHERE account_id=?',(self.account_id,))}

    def register(self, account_id, worker_id, token, epoch, confirmed_stopped=False):
        if not all(isinstance(v,str) and v for v in (account_id,worker_id,token,epoch)) or len(token)<32:
            raise ValueError('Invalid worker registration.')
        with closing(connect(self.target)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM account_workers WHERE account_id=?',(account_id,)).fetchone()
            if old and old['worker_id'] != worker_id and not confirmed_stopped:
                raise PermissionError('Confirm the previous worker has stopped before replacement.')
            db.execute('INSERT INTO account_workers(account_id,worker_id,token_hash,epoch,lease_until,heartbeat_at) VALUES(?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET worker_id=excluded.worker_id,token_hash=excluded.token_hash,epoch=excluded.epoch,lease_until=excluded.lease_until,heartbeat_at=excluded.heartbeat_at',
                (account_id,worker_id,hashlib.sha256(token.encode()).hexdigest(),epoch,stamp(60),stamp()))
            if worker_id==self.worker_id:
                import psutil
                db.execute('UPDATE account_workers SET machine=?,process_id=?,process_started=?,stopped_at=NULL WHERE account_id=?',(socket.gethostname(),os.getpid(),str(psutil.Process().create_time()),account_id))
        return {'account_id':account_id,'worker_id':worker_id,'epoch':epoch}

    def authenticate(self, token, account_id, worker_id, epoch):
        with closing(connect(self.target)) as db:
            row = db.execute('SELECT w.*,a.enabled,a.paused FROM account_workers w JOIN accounts a ON a.id=w.account_id WHERE w.account_id=?',(account_id,)).fetchone()
        if (not row or not hmac.compare_digest(row['token_hash'],hashlib.sha256(token.encode()).hexdigest())
            or row['worker_id'] != worker_id or row['epoch'] != epoch or row['lease_until'] <= stamp()
            or not row['enabled'] or row['paused']):
            raise PermissionError('Invalid, expired, or fenced worker credentials.')
        return row

    def heartbeat(self, token, account_id, worker_id, epoch):
        self.authenticate(token,account_id,worker_id,epoch)
        with closing(connect(self.target)) as db, db:
            changed = db.execute('UPDATE account_workers SET lease_until=?,heartbeat_at=? WHERE account_id=? AND worker_id=? AND epoch=?', (stamp(60),stamp(),account_id,worker_id,epoch))
            if changed.rowcount != 1:
                raise PermissionError('Worker registration changed.')
        return {'lease_seconds':60}

    def stopped(self):
        with closing(connect(self.target)) as db,db:
            db.execute('UPDATE account_workers SET stopped_at=?,lease_until=? WHERE account_id=? AND worker_id=? AND epoch=?',(stamp(),stamp(),self.account_id,self.worker_id,self.epoch))
