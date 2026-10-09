"""Durable native-message ledger. Detection only: no sending or model calls."""

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import time

from app.viber_database import DatabaseReadError, international_phone


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def message_hash(message):
    keys = ('chat_id', 'sender_id', 'direction', 'timestamp_ms', 'token', 'sort_order',
            'message_type', 'body', 'client_flag', 'sender_verified')
    return hashlib.sha256(json.dumps([message[k] for k in keys], ensure_ascii=True).encode()).hexdigest()


class InboxStore:
    def __init__(self, path):
        self.path = path
        with closing(self.connect()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS viber_sources (
                    source_id TEXT PRIMARY KEY, account_phone TEXT NOT NULL,
                    checkpoint INTEGER NOT NULL, initialized_at TEXT NOT NULL, last_poll TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS viber_conversations (
                    source_id TEXT NOT NULL, chat_id INTEGER NOT NULL, peer_id INTEGER NOT NULL,
                    phone TEXT NOT NULL, viber_name TEXT NOT NULL, monitoring INTEGER NOT NULL DEFAULT 1,
                    monitor_since_ms INTEGER NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(source_id,chat_id));
                CREATE TABLE IF NOT EXISTS viber_messages (
                    source_id TEXT NOT NULL, event_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
                    sender_id INTEGER, direction TEXT NOT NULL, timestamp_ms INTEGER NOT NULL,
                    token TEXT, sort_order INTEGER, message_type INTEGER NOT NULL,
                    body TEXT NOT NULL, client_flag INTEGER, sender_verified INTEGER NOT NULL,
                    content_hash TEXT NOT NULL, baseline INTEGER NOT NULL, detection TEXT NOT NULL,
                    deleted INTEGER NOT NULL DEFAULT 0, first_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(source_id,event_id));
                CREATE INDEX IF NOT EXISTS viber_messages_chat
                    ON viber_messages(source_id,chat_id,timestamp_ms,sort_order,event_id);
            ''')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def checkpoints(self):
        with closing(self.connect()) as db:
            return {row['source_id']: row['checkpoint'] for row in db.execute('SELECT * FROM viber_sources')}

    def ingest(self, snapshot):
        if snapshot.get('unchanged'):
            with closing(self.connect()) as db, db:
                db.execute('UPDATE viber_sources SET last_poll=? WHERE source_id=?',
                           (utc_now(), snapshot['source_id']))
            return {'imported': 0, 'new_incoming': 0, 'updated': 0}
        source = snapshot['source_id']
        stamp = utc_now()
        now_ms = int(time.time() * 1000)
        counts = {'imported': 0, 'new_incoming': 0, 'updated': 0}
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM viber_sources WHERE source_id=?', (source,)).fetchone()
            if previous and snapshot['max_event_id'] < previous['checkpoint']:
                raise DatabaseReadError('Viber history was reset. Detection is paused; rebaseline required.')
            # An account/profile switch deactivates old conversations, preserving
            # their history without mixing identities or reusing checkpoints.
            old_chats = {row['chat_id']: row for row in db.execute(
                'SELECT * FROM viber_conversations WHERE source_id=?', (source,))}
            recent_outreach = self._recent_outreach_cutoffs(db, now_ms)
            db.execute('UPDATE viber_conversations SET active=0')
            chats = {chat['chat_id']: chat for chat in snapshot['chats']}
            for chat in chats.values():
                old = old_chats.get(chat['chat_id'])
                if old and (old['phone'] != chat['phone'] or old['peer_id'] != chat['peer_id']):
                    raise DatabaseReadError('Viber conversation identity changed. Detection is paused.')
                db.execute('''INSERT INTO viber_conversations(source_id,chat_id,peer_id,phone,viber_name,monitor_since_ms)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(source_id,chat_id) DO UPDATE
                    SET viber_name=excluded.viber_name,active=1''',
                           (source, chat['chat_id'], chat['peer_id'], chat['phone'], chat['viber_name'], now_ms))
                if old and not old['active']:
                    db.execute('UPDATE viber_conversations SET monitor_since_ms=? WHERE source_id=? AND chat_id=?',
                               (now_ms, source, chat['chat_id']))
            existing = {row['event_id']: row for row in db.execute(
                'SELECT * FROM viber_messages WHERE source_id=?', (source,))}
            seen, revised = set(), set()
            for message in snapshot['messages']:
                event_id, chat_id = message['event_id'], message['chat_id']
                if event_id in seen or chat_id not in chats:
                    raise DatabaseReadError('Ambiguous Viber message identity. Detection is paused.')
                seen.add(event_id)
                old = existing.get(event_id)
                if old and (old['chat_id'] != chat_id or old['token'] != message['token']):
                    raise DatabaseReadError('Viber message IDs were reused. Detection is paused; rebaseline required.')
                digest = message_hash(message)
                if old and old['content_hash'] == digest and not old['deleted']:
                    continue
                baseline = not previous or chat_id not in old_chats or not old_chats[chat_id]['active']
                if old:
                    baseline = bool(old['baseline'])
                    # Editing or deleting an existing message never creates a
                    # fresh incoming trigger. Its conversation revision changes.
                    detection = 'EDITED'
                    counts['updated'] += 1
                else:
                    cutoff = recent_outreach.get(chats[chat_id]['phone'])
                    if (baseline and cutoff is not None and
                            cutoff <= message['timestamp_ms'] <= now_ms + 300_000 and
                            self.classify(message, False, None) == 'NEW_INCOMING'):
                        # A recipient can answer before the next watcher poll
                        # discovers the conversation. The app's just-recorded
                        # outbound attempt provides a narrow, durable boundary
                        # without treating older chat history as new.
                        baseline = False
                    detection = self.classify(message, baseline, old_chats.get(chat_id))
                    if detection == 'NEW_INCOMING':
                        counts['new_incoming'] += 1
                    counts['imported'] += 1
                if message['message_type'] == 72:
                    detection = 'DELETED'
                db.execute('''INSERT INTO viber_messages
                    (source_id,event_id,chat_id,sender_id,direction,timestamp_ms,token,sort_order,
                     message_type,body,client_flag,sender_verified,content_hash,baseline,detection,
                     deleted,first_seen_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)
                    ON CONFLICT(source_id,event_id) DO UPDATE SET
                     sender_id=excluded.sender_id,direction=excluded.direction,
                     timestamp_ms=excluded.timestamp_ms,sort_order=excluded.sort_order,
                     message_type=excluded.message_type,body=excluded.body,client_flag=excluded.client_flag,
                     sender_verified=excluded.sender_verified,content_hash=excluded.content_hash,
                     detection=excluded.detection,deleted=0,updated_at=excluded.updated_at''',
                           (source, event_id, chat_id, message['sender_id'], message['direction'],
                            message['timestamp_ms'], message['token'], message['sort_order'],
                            message['message_type'], message['body'], message['client_flag'],
                            int(message['sender_verified']), digest, int(baseline), detection, stamp, stamp))
                revised.add(chat_id)
            # The snapshot is complete for each selected chat; absent rows are
            # tombstoned, including old messages deleted outside the viewport.
            for event_id, old in existing.items():
                if old['chat_id'] in chats and event_id not in seen and not old['deleted']:
                    db.execute("UPDATE viber_messages SET deleted=1,detection='DELETED',updated_at=? "
                               'WHERE source_id=? AND event_id=?', (stamp, source, event_id))
                    revised.add(old['chat_id'])
                    counts['updated'] += 1
            for chat_id in revised:
                db.execute('UPDATE viber_conversations SET revision=revision+1 WHERE source_id=? AND chat_id=?',
                           (source, chat_id))
            db.execute('''INSERT INTO viber_sources VALUES(?,?,?,?,?) ON CONFLICT(source_id)
                DO UPDATE SET checkpoint=excluded.checkpoint,last_poll=excluded.last_poll''',
                       (source, snapshot['account_phone'], snapshot['max_event_id'], stamp, stamp))
        return counts

    @staticmethod
    def _recent_outreach_cutoffs(db, now_ms):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_sends'").fetchone():
            return {}
        rows = db.execute('''SELECT phone,
                MAX(CAST((julianday(COALESCE(attempted_at,updated_at,created_at)) - 2440587.5)
                         * 86400000 AS INTEGER)) AS cutoff
            FROM web_sends
            WHERE state='DISPATCHED' AND request_key NOT LIKE 'auto:%'
            GROUP BY phone''')
        return {row['phone']: row['cutoff'] for row in rows
                if row['cutoff'] is not None and 0 <= now_ms - row['cutoff'] <= 300_000}

    @staticmethod
    def classify(message, baseline, chat):
        if baseline:
            return 'BASELINE'
        if message['direction'] == 'OUTGOING':
            return 'OUTGOING'
        if message['direction'] != 'INCOMING' or not message['sender_verified']:
            return 'AMBIGUOUS'
        if message['client_flag'] in (256, 257) or message['message_type'] in (0, 15, 72):
            return 'IGNORED'
        if chat and not chat['monitoring']:
            return 'IGNORED'
        if chat and message['timestamp_ms'] < chat['monitor_since_ms']:
            return 'HISTORICAL'
        if message['timestamp_ms'] > int(time.time() * 1000) + 300_000:
            return 'REVIEW'
        if message['message_type'] != 1 or not message['body'].strip():
            return 'REVIEW'
        return 'NEW_INCOMING'

    def status(self):
        with closing(self.connect()) as db:
            return {'conversations': db.execute('SELECT count(*) FROM viber_conversations WHERE active=1').fetchone()[0],
                    'messages': db.execute('SELECT count(*) FROM viber_messages').fetchone()[0],
                    'new_incoming': db.execute("SELECT count(*) FROM viber_messages m JOIN viber_conversations c "
                                              "USING(source_id,chat_id) WHERE m.detection='NEW_INCOMING' AND c.active=1").fetchone()[0],
                    'last_poll': db.execute('SELECT MAX(last_poll) FROM viber_sources').fetchone()[0],
                    'automatic_replies': False}

    def conversations(self, offset=0, limit=50):
        if type(offset) is not int or offset < 0:
            raise ValueError('Invalid conversation offset.')
        with closing(self.connect()) as db:
            rows = db.execute('''SELECT c.*,
                (SELECT MAX(timestamp_ms) FROM viber_messages m WHERE m.source_id=c.source_id
                 AND m.chat_id=c.chat_id AND m.deleted=0) AS last_timestamp_ms,
                (SELECT count(*) FROM viber_messages m WHERE m.source_id=c.source_id
                 AND m.chat_id=c.chat_id AND m.detection='NEW_INCOMING') AS new_incoming
                FROM viber_conversations c WHERE active=1 ORDER BY last_timestamp_ms DESC,chat_id
                LIMIT ? OFFSET ?''', (limit, offset)).fetchall()
            leads = list(db.execute('SELECT id,phone,company_name FROM leads ORDER BY id'))
        result = []
        for row in rows:
            item = dict(row)
            matches = [dict(lead) for lead in leads if international_phone(lead['phone']) == row['phone']]
            item['leads'] = matches
            item['company_name'] = matches[0]['company_name'] if matches else row['phone']
            result.append(item)
        return result

    def conversation(self, source, chat_id, *, before=None, limit=100):
        if type(chat_id) is not int or (before is not None and (type(before) is not int or before < 1)):
            raise ValueError('Invalid conversation or message cursor.')
        with closing(self.connect()) as db:
            chat = db.execute('SELECT * FROM viber_conversations WHERE source_id=? AND chat_id=? AND active=1',
                              (source, chat_id)).fetchone()
            if not chat:
                raise ValueError('Conversation not found in the active Viber profile.')
            where, parameters = '', [source, chat_id]
            if before is not None:
                cursor = db.execute('SELECT timestamp_ms,COALESCE(sort_order,0),event_id FROM viber_messages '
                                    'WHERE source_id=? AND chat_id=? AND event_id=?', (source, chat_id, before)).fetchone()
                if not cursor:
                    raise ValueError('Message cursor does not belong to this conversation.')
                where = ' AND (timestamp_ms,COALESCE(sort_order,0),event_id)<(?,?,?)'
                parameters.extend(cursor)
            parameters.append(limit)
            rows = db.execute('''SELECT event_id,direction,timestamp_ms,message_type,body,
                sender_verified,baseline,detection,deleted,sort_order,client_flag FROM viber_messages
                WHERE source_id=? AND chat_id=? AND deleted=0 AND message_type NOT IN (0,72)
                AND COALESCE(client_flag,0) NOT IN (256,257)
                ''' + where + ' ORDER BY timestamp_ms DESC,COALESCE(sort_order,0) DESC,event_id DESC LIMIT ?',
                              parameters).fetchall()
        return {'conversation': dict(chat), 'messages': [dict(row) for row in reversed(rows)],
                'older_before': rows[-1]['event_id'] if len(rows) == limit else None,
                'automatic_replies': False}

    def monitor(self, source, chat_id, enabled):
        if type(enabled) is not bool or type(chat_id) is not int:
            raise ValueError('Choose whether to monitor this conversation.')
        with closing(self.connect()) as db, db:
            changed = db.execute('UPDATE viber_conversations SET monitoring=?,monitor_since_ms=?,revision=revision+1 '
                                 'WHERE source_id=? AND chat_id=? AND active=1', (int(enabled), int(time.time() * 1000), source, chat_id))
            if changed.rowcount != 1:
                raise ValueError('Conversation not found.')
            # Enabling does not retroactively trigger historical messages.
            db.execute("UPDATE viber_messages SET detection='IGNORED' WHERE source_id=? AND chat_id=? "
                       "AND detection='NEW_INCOMING'", (source, chat_id))
        return {'monitoring': enabled, 'automatic_replies': False}
