"""Durable incoming-turn claims, Codex drafts and serialized background dispatch."""

from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import threading
import time
import uuid

from app.codex_replies import CodexReplies, DEFAULT_INSTRUCTIONS, validate_result
from app.viber_database import international_phone
from app.viber_inbox import utc_now


class AutoReplies:
    def __init__(self, service, generator=None, settle_seconds=5):
        self.service = service
        self.inbox = service.watcher.inbox
        self.generator = generator or CodexReplies()
        self.settle_seconds = settle_seconds
        self.stop = threading.Event()
        self.thread = None
        self.state, self.error = 'STOPPED', None
        # Control changes and the final Send click share this lock. Pausing waits
        # only for a click already in progress, never for a model generation.
        self.dispatch_lock = threading.RLock()
        with closing(self.inbox.connect()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS auto_reply_settings (
                    id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL,
                    instructions TEXT NOT NULL, revision INTEGER NOT NULL,
                    since_rowid INTEGER NOT NULL, since_ms INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS auto_reply_jobs (
                    id TEXT PRIMARY KEY, source_id TEXT NOT NULL, chat_id INTEGER NOT NULL,
                    trigger_id INTEGER NOT NULL, revision INTEGER NOT NULL,
                    settings_revision INTEGER NOT NULL, phone TEXT NOT NULL,
                    lead_snapshot TEXT NOT NULL, send_fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL, text TEXT NOT NULL DEFAULT '', reason TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    operation_id TEXT, send_id TEXT,
                    UNIQUE(source_id,trigger_id));
                CREATE TABLE IF NOT EXISTS auto_reply_events (
                    source_id TEXT NOT NULL, event_id INTEGER NOT NULL, job_id TEXT NOT NULL,
                    PRIMARY KEY(source_id,event_id));
            ''')
            columns = {r[1] for r in db.execute('PRAGMA table_info(viber_conversations)')}
            for name, default in (('reply_enabled', 1), ('reply_since_rowid', 0), ('reply_reviewed_send_rowid', 0)):
                if name not in columns:
                    db.execute(f'ALTER TABLE viber_conversations ADD COLUMN {name} INTEGER NOT NULL DEFAULT {default}')
            db.execute('INSERT OR IGNORE INTO auto_reply_settings VALUES(1,0,?,0,0,0)', (DEFAULT_INSTRUCTIONS,))
            db.execute("UPDATE auto_reply_jobs SET state='UNKNOWN',reason='App stopped during dispatch. Check Viber before resuming replies.',updated_at=? WHERE state='SUBMITTING'", (utc_now(),))
            db.execute("UPDATE auto_reply_jobs SET state='INTERRUPTED',reason='App stopped before dispatch. This turn will not be retried automatically.',updated_at=? WHERE state IN ('GENERATING','QUEUED')", (utc_now(),))
            db.execute("UPDATE viber_conversations SET reply_enabled=0 WHERE EXISTS (SELECT 1 FROM auto_reply_jobs j WHERE j.source_id=viber_conversations.source_id AND j.chat_id=viber_conversations.chat_id AND j.state='UNKNOWN' AND COALESCE((SELECT rowid FROM web_sends WHERE id=j.send_id),9223372036854775807)>reply_reviewed_send_rowid)")

    def settings(self):
        with closing(self.inbox.connect()) as db:
            row = dict(db.execute('SELECT * FROM auto_reply_settings WHERE id=1').fetchone())
        return {'enabled': bool(row['enabled']), 'instructions': row['instructions'],
                'revision': row['revision'], 'state': self.state, 'error': self.error,
                'authentication': 'ChatGPT', 'settle_seconds': self.settle_seconds}

    def configure(self, enabled, instructions):
        if type(enabled) is not bool or not isinstance(instructions, str) or not instructions.strip() or len(instructions) > 8000:
            raise ValueError('Choose on/off and write reply instructions (1–8,000 characters).')
        if enabled:
            self.generator.check_login()
        with self.dispatch_lock, closing(self.inbox.connect()) as db, db:
            old = db.execute('SELECT * FROM auto_reply_settings WHERE id=1').fetchone()
            if bool(old['enabled']) == enabled and old['instructions'] == instructions.strip():
                return self.settings()
            cutoff = db.execute('SELECT COALESCE(MAX(rowid),0) FROM viber_messages').fetchone()[0]
            db.execute('UPDATE auto_reply_settings SET enabled=?,instructions=?,revision=revision+1,'
                       'since_rowid=?,since_ms=? WHERE id=1',
                       (int(enabled), instructions.strip(), cutoff if enabled and not old['enabled'] else old['since_rowid'],
                        int(time.time()*1000) if enabled and not old['enabled'] else old['since_ms']))
        self.service.event('AUTO_REPLIES', 'Automatic replies enabled for future incoming turns.' if enabled else 'Automatic replies paused.')
        return self.settings()

    def conversation_control(self, source, chat_id, enabled):
        if not isinstance(source, str) or type(chat_id) is not int or type(enabled) is not bool:
            raise ValueError('Choose a conversation and whether to reply automatically.')
        with self.dispatch_lock, closing(self.inbox.connect()) as db, db:
            cutoff = db.execute('SELECT COALESCE(MAX(rowid),0) FROM viber_messages').fetchone()[0]
            reviewed = db.execute('SELECT COALESCE(MAX(rowid),0) FROM web_sends').fetchone()[0]
            changed = db.execute('UPDATE viber_conversations SET reply_enabled=?,reply_since_rowid=?,reply_reviewed_send_rowid=?,revision=revision+1 '
                                 'WHERE source_id=? AND chat_id=? AND active=1', (int(enabled), cutoff, reviewed, source, chat_id))
            if changed.rowcount != 1:
                raise ValueError('Conversation not found.')
        return {'enabled': enabled}

    def jobs(self):
        with closing(self.inbox.connect()) as db:
            return [dict(r) for r in db.execute('SELECT id,source_id,chat_id,phone,state,text,reason,created_at,updated_at,send_id '
                                                'FROM auto_reply_jobs ORDER BY rowid DESC LIMIT 30')]

    def _update(self, job_id, state, reason=None, text=None):
        with closing(self.inbox.connect()) as db, db:
            db.execute('UPDATE auto_reply_jobs SET state=?,reason=?,text=COALESCE(?,text),updated_at=? WHERE id=?',
                       (state, reason, text, utc_now(), job_id))

    def _job(self, job_id):
        with closing(self.inbox.connect()) as db:
            return dict(db.execute('SELECT * FROM auto_reply_jobs WHERE id=?', (job_id,)).fetchone())

    @staticmethod
    def _messages(db, source, chat_id):
        # No page limit or silent truncation: the generator sees all retained
        # correspondence, including placeholders for unsupported attachments.
        return [dict(r) for r in db.execute('''SELECT event_id,direction,timestamp_ms,message_type,body,sender_verified,detection
            FROM viber_messages WHERE source_id=? AND chat_id=? AND deleted=0
            AND message_type NOT IN (0,15,72) AND COALESCE(client_flag,0) NOT IN (256,257)
            ORDER BY timestamp_ms,COALESCE(sort_order,0),event_id''', (source, chat_id))]

    @staticmethod
    def _send_fingerprint(db, phone, exclude=None):
        rows = [list(r) for r in db.execute("SELECT id,state FROM web_sends WHERE phone=? AND id!=? "
                                           "AND state NOT IN ('DRAFT','SCHEDULED','CANCELLED','BLOCKED','MISSED') ORDER BY id",
                                           (phone, exclude or ''))]
        return hashlib.sha256(json.dumps(rows).encode()).hexdigest()

    @staticmethod
    def _has_prior_outreach(db, phone, incoming_ms):
        return db.execute('''SELECT 1 FROM web_sends s
            WHERE s.phone=? AND s.state='DISPATCHED' AND s.request_key NOT LIKE 'auto:%'
            AND CAST((julianday(COALESCE(s.attempted_at,s.updated_at,s.created_at)) - 2440587.5)
                     * 86400000 AS INTEGER)<=? LIMIT 1''', (phone, incoming_ms)).fetchone() is not None

    def _claim(self):
        with closing(self.inbox.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            settings = db.execute('SELECT * FROM auto_reply_settings WHERE id=1').fetchone()
            if not settings['enabled'] or self.stop.is_set() or self.service.closed:
                return None
            candidates = db.execute('''SELECT m.*,m.rowid AS native_rowid,c.revision,c.phone
                FROM viber_messages m JOIN viber_conversations c USING(source_id,chat_id)
                WHERE c.active=1 AND c.monitoring=1 AND c.reply_enabled=1
                AND m.detection='NEW_INCOMING' AND m.deleted=0 AND m.sender_verified=1 AND m.direction='INCOMING'
                AND m.rowid>MAX(?,c.reply_since_rowid) AND m.timestamp_ms>=?
                AND EXISTS(SELECT 1 FROM web_sends s WHERE s.phone=c.phone AND s.state='DISPATCHED'
                  AND s.request_key NOT LIKE 'auto:%'
                  AND CAST((julianday(COALESCE(s.attempted_at,s.updated_at,s.created_at)) - 2440587.5)
                           * 86400000 AS INTEGER)<=m.timestamp_ms)
                AND NOT EXISTS(SELECT 1 FROM auto_reply_events e WHERE e.source_id=m.source_id AND e.event_id=m.event_id)
                ORDER BY m.timestamp_ms,COALESCE(m.sort_order,0),m.event_id''', (settings['since_rowid'], settings['since_ms'])).fetchall()
            if not candidates:
                return None
            groups = {}
            for row in candidates:
                groups.setdefault((row['source_id'],row['chat_id']), []).append(row)
            group = None
            for (source, chat_id), batch in groups.items():
                if time.time() - datetime.fromisoformat(batch[-1]['first_seen_at']).timestamp() < self.settle_seconds:
                    continue
                # Do not generate another turn until the previous Send action
                # appears as a verified outgoing event in the native history.
                unconfirmed = db.execute('''SELECT 1 FROM auto_reply_jobs j JOIN viber_conversations c USING(source_id,chat_id)
                    JOIN viber_messages trigger ON trigger.source_id=j.source_id AND trigger.event_id=j.trigger_id
                    WHERE j.source_id=? AND j.chat_id=? AND j.state='DISPATCHED' AND trigger.rowid>c.reply_since_rowid
                    AND NOT EXISTS(SELECT 1 FROM viber_messages m WHERE m.source_id=j.source_id AND m.chat_id=j.chat_id
                      AND m.event_id>j.trigger_id AND m.direction='OUTGOING' AND m.sender_verified=1
                      AND m.deleted=0 AND m.message_type=1 AND m.body=j.text) LIMIT 1''', (source,chat_id)).fetchone()
                if unconfirmed:
                    self.state = 'WAITING_FOR_OUTGOING'
                    continue
                group = batch
                break
            if group is None:
                return None
            latest = group[-1]
            leads = [dict(r) for r in db.execute('SELECT * FROM leads ORDER BY id')
                     if international_phone(r['phone']) == latest['phone']]
            if not leads:
                return None
            # Existing historical duplicate contacts represent one phone/chat.
            # Prefer a verified header name; never rename the business.
            lead = next((r for r in leads if r['viber_name']), leads[0])
            job_id = str(uuid.uuid4())
            db.execute('''INSERT INTO auto_reply_jobs(id,source_id,chat_id,trigger_id,revision,settings_revision,
                phone,lead_snapshot,send_fingerprint,state,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,'GENERATING',?,?)''',
                (job_id, latest['source_id'], latest['chat_id'], latest['event_id'], latest['revision'], settings['revision'],
                 latest['phone'], json.dumps(lead), self._send_fingerprint(db, latest['phone']), utc_now(), utc_now()))
            db.executemany('INSERT INTO auto_reply_events VALUES(?,?,?)', [(r['source_id'],r['event_id'],job_id) for r in group])
            return job_id

    def _context(self, job, refresh=False):
        if refresh and self.service.watcher.poll_once() is None:
            raise ValueError('Viber database is unavailable. Reply held without sending.')
        status = self.service.watcher.status()
        if status['state'] != 'WATCHING' or not status['last_poll'] or time.time() - datetime.fromisoformat(status['last_poll']).timestamp() > 30:
            raise ValueError('Message detection is not current. Reply held without sending.')
        with closing(self.inbox.connect()) as db:
            db.execute('BEGIN')
            settings = db.execute('SELECT * FROM auto_reply_settings WHERE id=1').fetchone()
            chat = db.execute('SELECT * FROM viber_conversations WHERE source_id=? AND chat_id=?', (job['source_id'], job['chat_id'])).fetchone()
            if self.stop.is_set() or self.service.closed or not settings['enabled'] or settings['revision'] != job['settings_revision']:
                raise ValueError('Automatic replies paused or instructions changed. No message sent.')
            if not chat or not chat['active'] or not chat['monitoring'] or not chat['reply_enabled'] or chat['revision'] != job['revision']:
                raise ValueError('Conversation changed or replies paused. This draft was cancelled.')
            if chat['phone'] != job['phone']:
                raise ValueError('Conversation phone changed. No message sent.')
            trigger = db.execute('SELECT timestamp_ms FROM viber_messages WHERE source_id=? AND event_id=?',
                                 (job['source_id'], job['trigger_id'])).fetchone()
            if not trigger or not self._has_prior_outreach(db, job['phone'], trigger['timestamp_ms']):
                raise ValueError('This contact was not messaged by the app before replying. No message sent.')
            lead = db.execute('SELECT * FROM leads WHERE id=?', (json.loads(job['lead_snapshot'])['id'],)).fetchone()
            if not lead or dict(lead) != json.loads(job['lead_snapshot']):
                raise ValueError('Saved contact changed. No message sent.')
            if self._send_fingerprint(db, job['phone'], job['send_id']) != job['send_fingerprint']:
                raise ValueError('Another send changed this conversation. Draft cancelled.')
            if db.execute("SELECT 1 FROM web_sends WHERE phone=? AND id!=? AND (state IN ('QUEUED','SUBMITTING') OR (state='UNKNOWN' AND rowid>?)) LIMIT 1",
                          (job['phone'], job['send_id'] or '', chat['reply_reviewed_send_rowid'])).fetchone():
                raise ValueError('Another send is pending or unverified. Check Sent before replying.')
            messages = self._messages(db, job['source_id'], job['chat_id'])
            if not messages or messages[-1]['event_id'] != job['trigger_id'] or messages[-1]['direction'] != 'INCOMING':
                raise ValueError('A newer message or manual reply superseded this incoming turn.')
            if any(m['direction'] not in ('INCOMING','OUTGOING') or not m['sender_verified'] for m in messages):
                raise ValueError('Message sender or direction needs review.')
            context = {'contact': {'business_name': lead['company_name'], 'viber_name': lead['viber_name'] or chat['viber_name']},
                       'messages': messages}
            if len(json.dumps(context, ensure_ascii=True)) > 120000:
                raise ValueError('Full history is too large for one reply request. Held for review; history was not truncated.')
            return context, settings['instructions']

    def tick(self):
        if self.service.pending or self.stop.is_set():
            return
        if self.service.watcher.status()['state'] != 'WATCHING':
            self.state = 'WAITING_FOR_VIBER'
            return
        self.state = 'WATCHING'
        job_id = self._claim()
        if not job_id:
            if not self.settings()['enabled']:
                self.state = 'PAUSED'
            return
        try:
            self.state, self.error = 'GENERATING', None
            job = self._job(job_id)
            context, instructions = self._context(job)
            result = validate_result(self.generator.generate(context, instructions))
            self._context(job, refresh=True)
            if result['action'] == 'hold':
                self._update(job_id, 'HELD', result['reason'])
                return
            with self.service.lock:
                self._update(job_id, 'QUEUED', result['reason'], result['text'])
                operation = self.service._enqueue('automatic-reply', lambda: self._dispatch(job_id))
                with closing(self.inbox.connect()) as db, db:
                    db.execute('UPDATE auto_reply_jobs SET operation_id=? WHERE id=?', (operation['operation_id'], job_id))
        except Exception as exc:
            self._update(job_id, 'HELD', str(exc))
            self.error = str(exc)
        finally:
            self.state = 'WATCHING' if self.settings()['enabled'] else 'PAUSED'

    def _dispatch(self, job_id):
        dispatched = False
        with self.service.lock:
            job = self._job(job_id)
        try:
            self._context(job, refresh=True)
            lead = self.service.lead(json.loads(job['lead_snapshot'])['id'])
            client, verified, name = self.service._verified(lead.id)
            if verified.phone != job['phone'] or not client.verify_current_name(name):
                raise ValueError('Recipient verification failed. No message sent.')
            # Opening an unknown contact can save its verified Viber header name.
            expected = json.loads(job['lead_snapshot'])
            if not expected['viber_name']:
                expected['viber_name'] = verified.viber_name
                with closing(self.inbox.connect()) as db, db:
                    db.execute('UPDATE auto_reply_jobs SET lead_snapshot=? WHERE id=?', (json.dumps(expected), job_id))
                job = self._job(job_id)
            if asdict(verified) != expected:
                raise ValueError('Saved recipient changed while opening the conversation.')
            self._context(job, refresh=True)
            send_id = str(uuid.uuid4())
            with closing(self.inbox.connect()) as db, db:
                operation_id = db.execute('SELECT operation_id FROM auto_reply_jobs WHERE id=?', (job_id,)).fetchone()[0] or job_id
                db.execute('''INSERT INTO web_sends(id,request_key,preview_hash,operation_id,lead_id,phone,company_name,
                    viber_name,text,state,created_at,updated_at) VALUES(?,?,?, ?,?,?,?,?,?,'QUEUED',?,?)''',
                    (send_id, 'auto:' + job_id, '', operation_id, lead.id, job['phone'], lead.company_name, name, job['text'], utc_now(), utc_now()))
                db.execute('UPDATE auto_reply_jobs SET send_id=? WHERE id=?', (send_id,job_id))
            job = self._job(job_id)

            def before_dispatch():
                self._context(job, refresh=True)

            def on_dispatch():
                self._context(job)
                with closing(self.inbox.connect()) as db, db:
                    db.execute("UPDATE auto_reply_jobs SET state='SUBMITTING',updated_at=? WHERE id=?", (utc_now(),job_id))
                    db.execute("UPDATE web_sends SET state='SUBMITTING',attempted_at=?,updated_at=? WHERE id=?", (utc_now(),utc_now(),send_id))

            # Typing and the source refresh do not hold the pause-control lock.
            # Only the final local checks, durable attempt marker and click do.
            client.send_message(job['text'], before_dispatch=before_dispatch,
                                on_dispatch=on_dispatch, dispatch_lock=self.dispatch_lock)
            with self.dispatch_lock:
                dispatched = True
                with closing(self.inbox.connect()) as db, db:
                    db.execute("UPDATE web_sends SET state='DISPATCHED',updated_at=? WHERE id=?", (utc_now(),send_id))
                    db.execute("UPDATE auto_reply_jobs SET state='DISPATCHED',reason='Send action dispatched; delivery unverified.',updated_at=? WHERE id=?", (utc_now(),job_id))
            self.service.event('AUTO_REPLY_DISPATCHED', f'Automatic reply dispatched for contact #{lead.id}. Delivery unverified.')
            return {'send_id': send_id, 'state': 'DISPATCHED'}
        except Exception as exc:
            current = self._job(job_id)
            uncertain = dispatched or current['state'] == 'SUBMITTING'
            state = 'UNKNOWN' if uncertain else 'BLOCKED'
            with self.dispatch_lock, closing(self.inbox.connect()) as db, db:
                db.execute('UPDATE auto_reply_jobs SET state=?,reason=?,updated_at=? WHERE id=?', (state,str(exc),utc_now(),job_id))
                if current['send_id']:
                    db.execute('UPDATE web_sends SET state=?,error=?,updated_at=? WHERE id=?', (state,str(exc),utc_now(),current['send_id']))
                if uncertain:
                    db.execute('UPDATE viber_conversations SET reply_enabled=0 WHERE source_id=? AND chat_id=?', (job['source_id'],job['chat_id']))
            raise

    def after_operation(self, operation_id):
        with closing(self.inbox.connect()) as db, db:
            operation = db.execute('SELECT * FROM web_operations WHERE id=?', (operation_id,)).fetchone()
            if operation and operation['state'] in ('FAILED','INTERRUPTED'):
                db.execute("UPDATE auto_reply_jobs SET state='BLOCKED',reason=?,updated_at=? WHERE operation_id=? AND state='QUEUED'",
                           (operation['error'] or 'Desktop operation stopped before dispatch.',utc_now(),operation_id))

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._loop, name='codex-viber-replies', daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                self.state, self.error = 'BLOCKED', 'Reply worker failed. Check Inbox before continuing.'
            self.stop.wait(2)

    def close(self):
        self.stop.set()
        self.generator.close()
        if self.thread:
            self.thread.join(timeout=20)
        self.state = 'STOPPED'
