"""Durable incoming-turn claims, Codex drafts and serialized background dispatch."""

from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
import hashlib
import json
import threading
import time
import uuid

from app.codex_replies import CodexReplies, DEFAULT_INSTRUCTIONS, validate_result
from app.portfolio import candidates, catalog
from app.reply_feedback import ReplyFeedback
from app.viber_database import international_phone
from app.viber_inbox import utc_now
from app.reply_errors import ReplyRetryable, ViberRetryable


class ConversationRevisionChanged(ValueError):
    """An unsent generation needs new context, rather than consuming the turn."""


class AutoReplies:
    def __init__(self, service, generator=None, settle_seconds=1):
        self.service = service
        self.inbox = service.watcher.inbox
        self.generator = generator or CodexReplies()
        self.settle_seconds = settle_seconds
        self.stop = threading.Event()
        self.wake = threading.Event()
        # Managed-account safeguards/concurrency remain active in both delivery modes.
        self.review = service.managed
        self.settings_table = 'auto_reply_account_settings' if getattr(service,'account_id','current') != 'current' else 'auto_reply_settings'
        self.settings_id = service.account_id if self.settings_table == 'auto_reply_account_settings' else 1
        self.generations = ThreadPoolExecutor(max_workers=2,thread_name_prefix='codex-draft')
        self.active = set()
        self.active_lock = threading.Lock()
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
            columns = {r[1] for r in db.execute('PRAGMA table_info(auto_reply_jobs)')}
            for column in ('generation_started_at','generation_finished_at','draft_ready_at','approved_at','queued_at','dispatch_finished_at','generation_ms','queue_ms','dispatch_queue_ms','review_ms','detection_ms'):
                if column not in columns:
                    db.execute(f'ALTER TABLE auto_reply_jobs ADD COLUMN {column} ' + ('INTEGER' if column.endswith('_ms') else 'TEXT'))
            if 'assumptions' not in columns:
                db.execute("ALTER TABLE auto_reply_jobs ADD COLUMN assumptions TEXT NOT NULL DEFAULT '[]'")
            for column, declaration in (('retry_count','INTEGER NOT NULL DEFAULT 0'),
                                        ('retry_at','TEXT'),('retry_phase','TEXT'),('error_code','TEXT'),
                                        ('outgoing_event_id','INTEGER'),('outgoing_confirmed_at','TEXT')):
                if column not in columns:
                    db.execute(f'ALTER TABLE auto_reply_jobs ADD COLUMN {column} {declaration}')
            db.execute('INSERT OR IGNORE INTO auto_reply_settings(id,enabled,instructions,revision,since_rowid,since_ms) VALUES(1,0,?,0,0,0)', (DEFAULT_INSTRUCTIONS,))
            if 'require_approval' not in {r[1] for r in db.execute('PRAGMA table_info(auto_reply_settings)')}:
                db.execute('ALTER TABLE auto_reply_settings ADD COLUMN require_approval INTEGER NOT NULL DEFAULT 1')
            if self.settings_table == 'auto_reply_account_settings':
                db.execute('''CREATE TABLE IF NOT EXISTS auto_reply_account_settings (
                    id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1, instructions TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0, since_rowid INTEGER NOT NULL DEFAULT 0,
                    since_ms INTEGER NOT NULL DEFAULT 0)''')
                if 'require_approval' not in {r[1] for r in db.execute('PRAGMA table_info(auto_reply_account_settings)')}:
                    db.execute('ALTER TABLE auto_reply_account_settings ADD COLUMN require_approval INTEGER NOT NULL DEFAULT 1')
                defaults = db.execute('SELECT instructions,require_approval FROM auto_reply_settings WHERE id=1').fetchone()
                # New verified accounts reply by default. INSERT OR IGNORE preserves
                # an explicit pause on subsequent restarts. Baseline the first
                # activation so imported conversations cannot trigger old replies.
                cutoff = db.execute('SELECT COALESCE(MAX(rowid),0) FROM viber_messages').fetchone()[0]
                db.execute('INSERT OR IGNORE INTO auto_reply_account_settings(id,enabled,instructions,require_approval,since_rowid,since_ms) VALUES(?,1,?,?,?,?)',
                           (self.settings_id,defaults['instructions'],defaults['require_approval'],cutoff,int(time.time()*1000)))
            scope = ' AND source_id IN (SELECT source_id FROM account_sources WHERE account_id=?)' if self.review else ''
            params = (utc_now(),service.accounts.account_id) if self.review else (utc_now(),)
            db.execute("UPDATE auto_reply_jobs SET state='UNKNOWN',reason='App stopped during dispatch. Check Viber before resuming replies.',updated_at=? WHERE state='SUBMITTING'" + scope, params)
            if self.review:
                db.execute("""UPDATE auto_reply_jobs SET state='RETRY',
                    reason='App restarted before Send. Revalidating the incoming turn.',
                    retry_phase=CASE WHEN text='' THEN 'generation' ELSE 'dispatch' END,
                    retry_at=?,error_code='restart_before_send',updated_at=?
                    WHERE state IN ('GENERATING','QUEUED')
                      AND NOT EXISTS(SELECT 1 FROM web_sends w WHERE w.id=auto_reply_jobs.send_id AND w.attempted_at IS NOT NULL)
                      AND NOT EXISTS(SELECT 1 FROM controller_jobs j WHERE j.operation_id=auto_reply_jobs.operation_id AND j.state='PENDING')""" + scope,
                    (utc_now(),utc_now(),service.accounts.account_id))
                db.execute("""UPDATE web_sends SET state='BLOCKED',error='Restarted before Send.',updated_at=?
                    WHERE attempted_at IS NULL AND state='QUEUED' AND id IN
                      (SELECT send_id FROM auto_reply_jobs WHERE state='RETRY' AND error_code='restart_before_send'
                       AND source_id IN (SELECT source_id FROM account_sources WHERE account_id=?))""", params)
            else:
                db.execute("UPDATE auto_reply_jobs SET state='INTERRUPTED',reason='App stopped before dispatch.',updated_at=? WHERE state IN ('GENERATING','QUEUED')" + scope, params)
            db.execute("UPDATE viber_conversations SET reply_enabled=0 WHERE EXISTS (SELECT 1 FROM auto_reply_jobs j WHERE j.source_id=viber_conversations.source_id AND j.chat_id=viber_conversations.chat_id AND j.state='UNKNOWN' AND COALESCE((SELECT rowid FROM web_sends WHERE id=j.send_id),9223372036854775807)>reply_reviewed_send_rowid)")
            if self.review and not self._settings_row(db)['require_approval']:
                db.execute("UPDATE auto_reply_jobs SET state='STALE',reason='Delivery mode changed. Old drafts are not sent automatically.',updated_at=? WHERE state='DRAFT'" + scope, params)
        self.feedback = ReplyFeedback(self)

    def settings(self):
        with closing(self.inbox.connect()) as db:
            row = dict(self._settings_row(db))
        approval = self.review and bool(row['require_approval'])
        return {'enabled': bool(row['enabled']), 'instructions': row['instructions'],
                'revision': row['revision'], 'state': self.state, 'error': self.error,
                'authentication': 'ChatGPT', 'settle_seconds': self.settle_seconds,'review_required':approval,
                'delivery_mode':'review' if approval else 'automatic','generation_slots':2 if self.review else 1,
                'portfolio_count': len(catalog()), 'owner_questions_location': 'dashboard'}

    def _settings_row(self, db):
        return db.execute(f'SELECT * FROM {self.settings_table} WHERE id=?',(self.settings_id,)).fetchone()

    def configure(self, enabled, instructions, require_approval=None):
        if type(enabled) is not bool or not isinstance(instructions, str) or not instructions.strip() or len(instructions) > 8000:
            raise ValueError('Choose on/off and write reply instructions (1–8,000 characters).')
        if require_approval is not None and type(require_approval) is not bool:
            raise ValueError('Choose whether reply approval is required.')
        if enabled:
            self.generator.check_login()
        with self.dispatch_lock, closing(self.inbox.connect()) as db, db:
            old = self._settings_row(db)
            approval = bool(old['require_approval']) if require_approval is None else require_approval
            if bool(old['enabled']) == enabled and old['instructions'] == instructions.strip() and bool(old['require_approval']) == approval:
                return self.settings()
            cutoff = db.execute('SELECT COALESCE(MAX(rowid),0) FROM viber_messages').fetchone()[0]
            db.execute(f'UPDATE {self.settings_table} SET enabled=?,instructions=?,require_approval=?,revision=revision+1,'
                       'since_rowid=?,since_ms=? WHERE id=?',
                       (int(enabled), instructions.strip(), int(approval), cutoff if enabled and not old['enabled'] else old['since_rowid'],
                        int(time.time()*1000) if enabled and not old['enabled'] else old['since_ms'],self.settings_id))
            if bool(old['require_approval']) != approval:
                scope = ' AND source_id IN (SELECT source_id FROM account_sources WHERE account_id=?)' if self.review else ''
                db.execute("UPDATE auto_reply_jobs SET state='STALE',reason='Delivery mode changed. Generate a fresh reply.',updated_at=? WHERE state='DRAFT'" + scope,
                           (utc_now(),self.service.account_id) if self.review else (utc_now(),))
        self.service.event('AUTO_REPLIES', 'Automatic replies enabled for future incoming turns.' if enabled else 'Automatic replies paused.')
        self.wake.set()
        return self.settings()

    def conversation_control(self, source, chat_id, enabled):
        if not isinstance(source, str) or type(chat_id) is not int or type(enabled) is not bool:
            raise ValueError('Choose a conversation and whether to reply automatically.')
        if self.review:
            self.inbox.conversation(source,chat_id)
        with self.dispatch_lock, closing(self.inbox.connect()) as db, db:
            cutoff = db.execute('SELECT COALESCE(MAX(rowid),0) FROM viber_messages').fetchone()[0]
            reviewed = db.execute('SELECT COALESCE(MAX(rowid),0) FROM web_sends').fetchone()[0]
            changed = db.execute('UPDATE viber_conversations SET reply_enabled=?,reply_since_rowid=?,reply_reviewed_send_rowid=?,revision=revision+1 '
                                 'WHERE source_id=? AND chat_id=? AND active=1', (int(enabled), cutoff, reviewed, source, chat_id))
            if changed.rowcount != 1:
                raise ValueError('Conversation not found.')
            if self.review and enabled:
                phone=db.execute('SELECT phone FROM viber_conversations WHERE source_id=? AND chat_id=?',(source,chat_id)).fetchone()[0]
                db.execute("UPDATE controller_jobs SET reviewed_at=? WHERE account_id=? AND phone=? AND state='UNKNOWN'",(utc_now(),self.service.accounts.account_id,phone))
        return {'enabled': enabled}

    def jobs(self):
        with closing(self.inbox.connect()) as db:
            scope = " WHERE EXISTS(SELECT 1 FROM contact_owners o WHERE o.phone=auto_reply_jobs.phone AND o.account_id=?)" if self.review else ''
            rows = [dict(r) for r in db.execute('SELECT id,source_id,chat_id,phone,state,text,reason,created_at,updated_at,send_id,generation_ms,queue_ms,dispatch_queue_ms,review_ms,detection_ms,assumptions,retry_count,retry_at,error_code '
                                                'FROM auto_reply_jobs' + scope + ' ORDER BY rowid DESC LIMIT 30', (self.service.accounts.account_id,) if self.review else ())]
            for row in rows:
                row['assumptions'] = json.loads(row['assumptions'])
            return rows

    def _update(self, job_id, state, reason=None, text=None):
        with closing(self.inbox.connect()) as db, db:
            db.execute('UPDATE auto_reply_jobs SET state=?,reason=?,text=COALESCE(?,text),updated_at=? WHERE id=?',
                       (state, reason, text, utc_now(), job_id))

    def _job(self, job_id):
        with closing(self.inbox.connect()) as db:
            return dict(db.execute('SELECT * FROM auto_reply_jobs WHERE id=?', (job_id,)).fetchone())

    def _defer(self, job_id, exc, phase='generation'):
        """Retry only a turn with no durable dispatch attempt; keep its unique claim."""
        if not self.review:
            return False
        with self.dispatch_lock, closing(self.inbox.connect()) as db, db:
            job = db.execute('SELECT * FROM auto_reply_jobs WHERE id=?',(job_id,)).fetchone()
            if not job or job['state'] in ('SUBMITTING','DISPATCHED','UNKNOWN','REJECTED'):
                return False
            if job['state']=='RETRY':
                return True
            send = db.execute('SELECT attempted_at,state FROM web_sends WHERE id=?',(job['send_id'],)).fetchone()
            if send and (send['attempted_at'] or send['state'] in ('SUBMITTING','DISPATCHED','UNKNOWN')):
                return False
            count = job['retry_count']+1
            due = (datetime.now(timezone.utc)+timedelta(seconds=min(60,2**min(count,6)))).isoformat()
            db.execute("""UPDATE auto_reply_jobs SET state='RETRY',reason=?,retry_count=?,retry_at=?,
                retry_phase=?,error_code=?,updated_at=? WHERE id=?""",
                (str(exc)[:2000],count,due,phase,getattr(exc,'code','temporary_failure'),utc_now(),job_id))
        self.wake.set()
        return True

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
            settings = self._settings_row(db)
            if not settings['enabled'] or self.stop.is_set() or self.service.closed:
                return None
            if self.review:
                rebuilt = self._claim_rebuild(db)
                if rebuilt:
                    return rebuilt
            outreach = '''AND EXISTS(SELECT 1 FROM web_sends s WHERE s.phone=c.phone AND s.state='DISPATCHED'
                  AND s.request_key NOT LIKE 'auto:%'
                  AND CAST((julianday(COALESCE(s.attempted_at,s.updated_at,s.created_at)) - 2440587.5)
                           * 86400000 AS INTEGER)<=m.timestamp_ms)''' if not self.review else ''
            candidates = db.execute('''SELECT m.*,m.rowid AS native_rowid,c.revision,c.phone
                FROM viber_messages m JOIN viber_conversations c USING(source_id,chat_id)
                WHERE c.active=1 AND c.monitoring=1 AND c.reply_enabled=1
                AND m.detection='NEW_INCOMING' AND m.deleted=0 AND m.sender_verified=1 AND m.direction='INCOMING'
                AND m.rowid>MAX(?,c.reply_since_rowid) AND m.timestamp_ms>=?
                ''' + outreach + '''
                AND NOT EXISTS(SELECT 1 FROM auto_reply_events e WHERE e.source_id=m.source_id AND e.event_id=m.event_id)
                ORDER BY m.timestamp_ms,COALESCE(m.sort_order,0),m.event_id''', (settings['since_rowid'], settings['since_ms'])).fetchall()
            if not candidates:
                return None
            groups = {}
            for row in candidates:
                groups.setdefault((row['source_id'],row['chat_id']), []).append(row)
            group = None
            for (source, chat_id), batch in groups.items():
                if self.review:
                    ownership = db.execute('SELECT 1 FROM account_sources s JOIN contact_owners o ON o.account_id=s.account_id WHERE s.source_id=? AND o.phone=? AND s.account_id=?',(source,batch[-1]['phone'],self.service.accounts.account_id)).fetchone()
                    conflicting = db.execute("SELECT 1 FROM auto_reply_jobs WHERE source_id=? AND chat_id=? AND state IN ('GENERATING','QUEUED','SUBMITTING','REGENERATE')",(source,chat_id)).fetchone()
                    sending = db.execute("SELECT 1 FROM web_sends s JOIN viber_conversations c ON c.source_id=? AND c.chat_id=? WHERE s.phone=? AND (s.state IN ('QUEUED','SUBMITTING') OR (s.state='UNKNOWN' AND s.rowid>c.reply_reviewed_send_rowid))",(source,chat_id,batch[-1]['phone'])).fetchone()
                    if not ownership or conflicting or sending:
                        continue
                if time.time() - datetime.fromisoformat(batch[-1]['first_seen_at']).timestamp() < self.settle_seconds:
                    continue
                # Do not generate another turn until the previous Send action
                # appears as a verified outgoing event in the native history.
                unconfirmed = db.execute('''SELECT 1 FROM auto_reply_jobs j JOIN viber_conversations c USING(source_id,chat_id)
                    JOIN viber_messages trigger ON trigger.source_id=j.source_id AND trigger.event_id=j.trigger_id
                    WHERE j.source_id=? AND j.chat_id=? AND j.state='DISPATCHED' AND j.outgoing_confirmed_at IS NULL AND trigger.rowid>c.reply_since_rowid
                    AND NOT EXISTS(SELECT 1 FROM viber_messages m WHERE m.source_id=j.source_id AND m.chat_id=j.chat_id
                      AND m.event_id>j.trigger_id AND m.direction='OUTGOING' AND m.sender_verified=1
                      AND m.deleted=0 AND m.message_type IN (1,9) AND m.body=j.text) LIMIT 1''', (source,chat_id)).fetchone()
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
            db.execute('UPDATE auto_reply_jobs SET detection_ms=? WHERE id=?',(max(0,int(datetime.fromisoformat(latest['first_seen_at']).timestamp()*1000)-latest['timestamp_ms']),job_id))
            return job_id

    def _claim_rebuild(self, db):
        jobs = db.execute('''SELECT j.* FROM auto_reply_jobs j
            JOIN account_sources s ON s.source_id=j.source_id
            WHERE s.account_id=? AND (j.state='REGENERATE' OR (j.state='RETRY' AND j.retry_at<=?))
            ORDER BY j.created_at''', (self.service.accounts.account_id,utc_now())).fetchall()
        for row in jobs:
            job = dict(row)
            with self.active_lock:
                if job['id'] in self.active:
                    continue
            conflict = db.execute('''SELECT 1 FROM auto_reply_jobs
                WHERE source_id=? AND chat_id=? AND id!=?
                  AND state IN ('GENERATING','QUEUED','SUBMITTING')''',
                (job['source_id'], job['chat_id'], job['id'])).fetchone()
            if conflict:
                continue
            if db.execute("SELECT 1 FROM controller_jobs WHERE operation_id=? AND state IN ('PENDING','RUNNING')",
                          (job['operation_id'],)).fetchone():
                continue
            send = db.execute('SELECT state,attempted_at FROM web_sends WHERE id=?',(job['send_id'],)).fetchone()
            if send and (send['attempted_at'] or send['state'] != 'BLOCKED'):
                db.execute("UPDATE auto_reply_jobs SET state='BLOCKED',reason='Send requires reconciliation before recovery.',updated_at=? WHERE id=?",
                           (utc_now(),job['id']))
                continue
            chat = db.execute('SELECT revision FROM viber_conversations WHERE source_id=? AND chat_id=?',
                              (job['source_id'], job['chat_id'])).fetchone()
            reuse = job['state']=='RETRY' and job['retry_phase']=='dispatch' and job['text'] and chat and chat['revision']==job['revision']
            if chat:
                job['revision'] = chat['revision']
            try:
                # Retain the original recipient, instruction and send snapshots.
                # A newer incoming turn is claimed normally; a manual reply,
                # pause, ownership change or uncertain send cannot be retried.
                self._context(job)
            except ReplyRetryable:
                continue
            except Exception as exc:
                db.execute("UPDATE auto_reply_jobs SET state='STALE',reason=?,updated_at=? WHERE id=?",
                           (str(exc), utc_now(), job['id']))
                continue
            db.execute("""UPDATE auto_reply_jobs SET state='GENERATING',revision=?,text=?,
                retry_phase=?,retry_at=NULL,updated_at=? WHERE id=? AND state IN ('REGENERATE','RETRY')""",
                (job['revision'],job['text'] if reuse else '', 'dispatch' if reuse else 'generation',utc_now(),job['id']))
            return job['id']
        return None

    def _context(self, job, refresh=False):
        if self.review:
            self.service.accounts.assert_phone(job['phone'])
        if refresh and self.service.watcher.poll_once() is None:
            raise ReplyRetryable('Viber database is unavailable. Waiting for fresh detection.', 'inbox_unavailable')
        status = self.service.watcher.status()
        if status['state'] != 'WATCHING' or not status['last_poll'] or time.time() - datetime.fromisoformat(status['last_poll']).timestamp() > 30:
            raise ReplyRetryable('Message detection is not current. Waiting for a fresh inbox read.', 'inbox_stale')
        with closing(self.inbox.connect()) as db:
            db.execute('BEGIN')
            settings = self._settings_row(db)
            chat = db.execute('SELECT * FROM viber_conversations WHERE source_id=? AND chat_id=?', (job['source_id'], job['chat_id'])).fetchone()
            if self.stop.is_set() or self.service.closed or not settings['enabled'] or settings['revision'] != job['settings_revision']:
                raise ValueError('Automatic replies paused or instructions changed. No message sent.')
            if not chat or not chat['active'] or not chat['monitoring'] or not chat['reply_enabled']:
                raise ValueError('Conversation changed or replies paused. This draft was cancelled.')
            if chat['revision'] != job['revision']:
                raise ConversationRevisionChanged('Conversation history changed. Rebuilding the unsent reply with fresh context.')
            if chat['phone'] != job['phone']:
                raise ValueError('Conversation phone changed. No message sent.')
            trigger = db.execute('SELECT timestamp_ms FROM viber_messages WHERE source_id=? AND event_id=?',
                                 (job['source_id'], job['trigger_id'])).fetchone()
            if not trigger or (not self.review and not self._has_prior_outreach(db, job['phone'], trigger['timestamp_ms'])):
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
            # Preserve earlier captured facts when Viber drops older rows.
            # These records never become triggers or replace current messages.
            context['retained_background'] = [dict(r) for r in db.execute('''
                SELECT event_id,direction,timestamp_ms,message_type,body
                FROM viber_messages WHERE source_id=? AND chat_id=? AND deleted=1
                  AND event_id<? AND message_type IN (1,9) AND sender_verified=1
                  AND direction IN ('INCOMING','OUTGOING')
                  AND COALESCE(client_flag,0) NOT IN (256,257)
                ORDER BY timestamp_ms,COALESCE(sort_order,0),event_id''',
                (job['source_id'],job['chat_id'],job['trigger_id']))]
            context['portfolio_candidates'] = candidates(context)
            context['saved_owner_rules'] = self.feedback.rules(db)
            if len(json.dumps(context, ensure_ascii=True)) > 120000:
                raise ValueError('Full history is too large for one reply request. Held for review; history was not truncated.')
            return context, settings['instructions']

    def tick(self):
        if (self.service.pending and not self.review) or self.stop.is_set():
            return
        if self.service.watcher.status()['state'] != 'WATCHING':
            self.state = 'WAITING_FOR_VIBER'
            return
        self.state = 'WATCHING'
        self._confirm_outgoing()
        job_id = self._claim()
        if not job_id:
            if not self.settings()['enabled']:
                self.state = 'PAUSED'
            return
        if self.review:
            with self.active_lock:
                self.active.add(job_id)
            self.generations.submit(self._generate,job_id)
        else:
            self._generate(job_id)

    def _confirm_outgoing(self):
        """Keep proof that the native reader observed an outgoing reply."""
        scope=' AND source_id IN (SELECT source_id FROM account_sources WHERE account_id=?)' if self.review else ''
        with closing(self.inbox.connect()) as db,db:
            rows=db.execute("SELECT id,source_id,chat_id,trigger_id,text FROM auto_reply_jobs WHERE state='DISPATCHED' AND outgoing_confirmed_at IS NULL"+scope,
                            (self.service.accounts.account_id,) if self.review else ()).fetchall()
            for job in rows:
                event=db.execute('''SELECT event_id,first_seen_at FROM viber_messages
                    WHERE source_id=? AND chat_id=? AND event_id>? AND direction='OUTGOING'
                      AND sender_verified=1 AND message_type IN (1,9) AND body=?
                    ORDER BY event_id LIMIT 1''',(job['source_id'],job['chat_id'],job['trigger_id'],job['text'])).fetchone()
                if event:
                    db.execute('UPDATE auto_reply_jobs SET outgoing_event_id=?,outgoing_confirmed_at=? WHERE id=? AND outgoing_confirmed_at IS NULL',
                               (event['event_id'],event['first_seen_at'],job['id']))

    def _generate(self,job_id):
        try:
            self.state, self.error = 'GENERATING', None
            job = self._job(job_id)
            with self.service.generation_limit:
                context, instructions = self._context(job)
                if job['retry_phase']=='dispatch' and job['text']:
                    result = validate_result({'action':'reply','text':job['text'],'reason':job['reason'] or '',
                                              'assumptions':json.loads(job['assumptions'])})
                else:
                    started = time.perf_counter()
                    with closing(self.inbox.connect()) as db, db:
                        db.execute('UPDATE auto_reply_jobs SET generation_started_at=?,queue_ms=? WHERE id=?',(datetime.now(timezone.utc).isoformat(),max(0,int((time.time()-datetime.fromisoformat(job['created_at']).timestamp())*1000)),job_id))
                    result = validate_result(self.generator.generate(context, instructions))
                    with closing(self.inbox.connect()) as db, db:
                        db.execute('UPDATE auto_reply_jobs SET generation_finished_at=?,generation_ms=? WHERE id=?',(datetime.now(timezone.utc).isoformat(),int((time.perf_counter()-started)*1000),job_id))
            with self.dispatch_lock:
                self._context(job, refresh=True)
                with closing(self.inbox.connect()) as db, db:
                    self.feedback.record(db, job, result.get('assumptions', []))
                    if result['action'] == 'hold':
                        db.execute("UPDATE auto_reply_jobs SET state='HELD',reason=?,updated_at=? WHERE id=?", (result['reason'], utc_now(), job_id))
                        return
                    if self.review and self._settings_row(db)['require_approval']:
                        db.execute("UPDATE auto_reply_jobs SET state='DRAFT',reason=?,text=?,updated_at=?,draft_ready_at=? WHERE id=?",(result['reason'],result['text'],utc_now(),datetime.now(timezone.utc).isoformat(),job_id))
                        return
                self._queue_reply(job, result['text'], result['reason'])
        except ConversationRevisionChanged as exc:
            self._update(job_id, 'REGENERATE' if self.review else 'HELD', str(exc))
            self.error = None if self.review else str(exc)
        except (ReplyRetryable, ViberRetryable) as exc:
            if not self._defer(job_id,exc):
                self._update(job_id,'HELD',str(exc))
            self.error = str(exc)
        except Exception as exc:
            if not ((self.stop.is_set() or self.service.closed) and self._defer(job_id,ReplyRetryable('Drafting interrupted before Send.', 'shutdown_before_send'))):
                self._update(job_id, 'HELD', str(exc))
            self.error = str(exc)
        finally:
            with self.active_lock:
                self.active.discard(job_id)
            self.wake.set()
            self.state = 'WATCHING' if self.settings()['enabled'] else 'PAUSED'

    def review_draft(self,job_id,approve):
        if not self.settings()['review_required'] or type(approve) is not bool:
            raise ValueError('Choose approve or reject for a dashboard draft.')
        with self.dispatch_lock, self.service.lock:
            job = self._job(job_id)
            if job['state'] != 'DRAFT':
                raise ValueError('This draft is no longer awaiting review.')
            if not approve:
                self._update(job_id,'REJECTED','Rejected during dashboard review.')
                return {'state':'REJECTED'}
            try:
                self._context(job,refresh=True)
            except Exception as exc:
                self._update(job_id,'STALE',str(exc))
                raise
            return self._queue_reply(job, job['text'], job['reason'], approved=True)

    def _queue_reply(self, job, text, reason, approved=False):
        with self.service.lock:
            queued = datetime.now(timezone.utc).isoformat()
            review_ms = max(0,int((time.time()-datetime.fromisoformat(job['draft_ready_at']).timestamp())*1000)) if approved else 0
            with closing(self.inbox.connect()) as db, db:
                db.execute("UPDATE auto_reply_jobs SET state='QUEUED',text=?,reason=?,updated_at=?,queued_at=?,approved_at=?,review_ms=? WHERE id=?",
                           (text,reason,utc_now(),queued,queued if approved else None,review_ms,job['id']))
            job_id = job['id']
            try:
                operation = self.service._enqueue('automatic-reply',lambda:self._dispatch(job_id),{'phone':job['phone'],'payload':{'reply_job_id':job_id},'priority':100})
            except Exception as exc:
                self._defer(job_id,ReplyRetryable(str(exc),'queue_unavailable'),'dispatch')
                raise ReplyRetryable(str(exc),'queue_unavailable') from exc
            with closing(self.inbox.connect()) as db, db:
                db.execute('UPDATE auto_reply_jobs SET operation_id=? WHERE id=?',(operation['operation_id'],job_id))
            return {**operation,'state':'QUEUED'}

    def invalidate_drafts(self):
        with closing(self.inbox.connect()) as db:
            drafts = [dict(r) for r in db.execute("SELECT * FROM auto_reply_jobs WHERE state='DRAFT' AND EXISTS(SELECT 1 FROM account_sources s WHERE s.source_id=auto_reply_jobs.source_id AND s.account_id=?)",(self.service.accounts.account_id,))]
        for job in drafts:
            try:
                self._context(job)
            except Exception as exc:
                self._update(job['id'],'STALE',str(exc))

    def regenerate(self, job_id):
        if not self.review or not isinstance(job_id, str):
            raise ValueError('Choose a dashboard draft to regenerate.')
        with self.dispatch_lock, self.service.lock, self.active_lock:
            job = self._job(job_id)
            if job['state'] not in ('DRAFT', 'STALE') or job['send_id']:
                raise ValueError('Only an unsent draft can be regenerated.')
            if len(self.active) >= 2:
                raise ValueError('Both drafting slots are busy. Try again shortly.')
            job['settings_revision'] = self.settings()['revision']
            self._context(job, refresh=True)
            with closing(self.inbox.connect()) as db, db:
                conflict = db.execute("SELECT 1 FROM auto_reply_jobs WHERE source_id=? AND chat_id=? AND id!=? AND state IN ('GENERATING','QUEUED','SUBMITTING')",
                                      (job['source_id'], job['chat_id'], job_id)).fetchone()
                if conflict:
                    raise ValueError('This conversation already has an active draft or send.')
                db.execute("UPDATE auto_reply_jobs SET state='GENERATING',settings_revision=?,text='',reason=NULL,assumptions='[]',created_at=?,updated_at=?,draft_ready_at=NULL WHERE id=?",
                           (job['settings_revision'], utc_now(), utc_now(), job_id))
            self.active.add(job_id)
            self.generations.submit(self._generate, job_id)
        return {'state': 'GENERATING'}

    def _dispatch(self, job_id):
        dispatched = False
        with self.service.lock:
            job = self._job(job_id)
        if job['state'] != 'QUEUED':
            raise ValueError('This reply is not queued for sending.')
        queued_at = job['queued_at'] or job['approved_at']
        if queued_at:
            with closing(self.inbox.connect()) as db,db:
                db.execute('UPDATE auto_reply_jobs SET dispatch_queue_ms=? WHERE id=?',(max(0,int((time.time()-datetime.fromisoformat(queued_at).timestamp())*1000)),job_id))
        try:
            self._context(job, refresh=True)
            lead = self.service.lead(json.loads(job['lead_snapshot'])['id'])
            def recover_draft(client):
                if job['send_id'] and getattr(type(client),'recover_pending',None):
                    client.recover_pending('auto:'+job_id+':'+job['send_id'])
            client, verified, name = self.service._verified(lead.id, before_open=recover_draft)
            if verified.phone != job['phone']:
                raise ValueError('Recipient verification failed. No message sent.')
            if not client.verify_current_name(name):
                raise ViberRetryable('Recipient header is unstable before typing. No message was sent.', 'header_unstable')
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
            send_id = job['send_id'] or str(uuid.uuid4())
            with closing(self.inbox.connect()) as db, db:
                operation_id = db.execute('SELECT operation_id FROM auto_reply_jobs WHERE id=?', (job_id,)).fetchone()[0] or job_id
                if job['send_id']:
                    prior = db.execute('SELECT * FROM web_sends WHERE id=?',(send_id,)).fetchone()
                    if not prior or prior['attempted_at'] or prior['state']!='BLOCKED' or prior['phone']!=job['phone'] or prior['request_key']!='auto:'+job_id:
                        raise ValueError('Previous send is not a confirmed unsent attempt. Recovery blocked.')
                    db.execute("UPDATE web_sends SET state='QUEUED',text=?,viber_name=?,operation_id=?,error=NULL,updated_at=? WHERE id=?",
                               (job['text'],name,operation_id,utc_now(),send_id))
                else:
                    db.execute('''INSERT INTO web_sends(id,request_key,preview_hash,operation_id,lead_id,phone,company_name,
                    viber_name,text,state,created_at,updated_at) VALUES(?,?,?, ?,?,?,?,?,?,'QUEUED',?,?)''',
                        (send_id, 'auto:' + job_id, '', operation_id, lead.id, job['phone'], lead.company_name, name, job['text'], utc_now(), utc_now()))
                db.execute('UPDATE auto_reply_jobs SET send_id=? WHERE id=?', (send_id,job_id))
            job = self._job(job_id)
            if getattr(type(client),'recover_pending',None):
                client.prepare_key = 'auto:'+job_id+':'+send_id

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
                    db.execute('UPDATE auto_reply_jobs SET dispatch_finished_at=? WHERE id=?',(datetime.now(timezone.utc).isoformat(),job_id))
            self.service.event('AUTO_REPLY_DISPATCHED', f'Automatic reply dispatched for contact #{lead.id}. Delivery unverified.')
            return {'send_id': send_id, 'state': 'DISPATCHED'}
        except Exception as exc:
            current = self._job(job_id)
            with closing(self.inbox.connect()) as db:
                marked = db.execute('SELECT attempted_at FROM web_sends WHERE id=?',(current['send_id'],)).fetchone()
            uncertain = dispatched or current['state'] == 'SUBMITTING' or bool(marked and marked['attempted_at'])
            state = 'UNKNOWN' if uncertain else 'BLOCKED'
            with self.dispatch_lock, closing(self.inbox.connect()) as db, db:
                db.execute('UPDATE auto_reply_jobs SET state=?,reason=?,updated_at=? WHERE id=?', (state,str(exc),utc_now(),job_id))
                if current['send_id']:
                    db.execute('UPDATE web_sends SET state=?,error=?,updated_at=? WHERE id=?', (state,str(exc),utc_now(),current['send_id']))
                if uncertain:
                    db.execute('UPDATE viber_conversations SET reply_enabled=0 WHERE source_id=? AND chat_id=?', (job['source_id'],job['chat_id']))
            if not uncertain:
                if isinstance(exc,ConversationRevisionChanged):
                    self._defer(job_id,ReplyRetryable(str(exc),'history_changed'),'generation')
                elif isinstance(exc,(ReplyRetryable,ViberRetryable)):
                    self._defer(job_id,exc,'dispatch')
            raise

    def after_operation(self, operation_id):
        with closing(self.inbox.connect()) as db, db:
            operation = db.execute('SELECT * FROM web_operations WHERE id=?', (operation_id,)).fetchone()
            if operation and operation['state'] in ('FAILED','INTERRUPTED'):
                row = db.execute("SELECT id,send_id FROM auto_reply_jobs WHERE operation_id=? AND state='QUEUED'",(operation_id,)).fetchone()
                if row:
                    job_id = row['id']
                else:
                    job_id = None
            else:
                job_id = None
        if job_id:
            if not self._defer(job_id,ReplyRetryable(operation['error'] or 'Desktop operation stopped before dispatch.','desktop_operation_failed'),'dispatch'):
                self._update(job_id,'BLOCKED',operation['error'])

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        try:
            self.generator.check_login()
        except Exception as exc:
            self.error = str(exc)
        self.thread = threading.Thread(target=self._loop, name='codex-viber-replies', daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.stop.is_set():
            try:
                if self.review:
                    self.invalidate_drafts()
                with self.active_lock:
                    available = 2-len(self.active)
                for _ in range(available):
                    self.tick()
            except Exception:
                self.state, self.error = 'BLOCKED', 'Reply worker failed. Check Inbox before continuing.'
            self.wake.wait(.2 if self.review else 2)
            self.wake.clear()

    def close(self):
        self.stop.set()
        self.wake.set()
        self.generator.close()
        if self.thread:
            self.thread.join(timeout=20)
        self.generations.shutdown(wait=True,cancel_futures=True)
        self.state = 'STOPPED'
