"""Private, durable owner questions and learned business rules per account."""
from contextlib import closing
import json
import uuid

from app.viber_inbox import utc_now


class ReplyFeedback:
    def __init__(self, replies):
        self.replies = replies
        self.account_id = replies.service.accounts.account_id if replies.review else 'legacy'
        with closing(replies.inbox.connect()) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS reply_owner_questions (
                id TEXT PRIMARY KEY, account_id TEXT NOT NULL, topic TEXT NOT NULL,
                job_id TEXT NOT NULL, phone TEXT NOT NULL, assumption TEXT NOT NULL,
                question TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'OPEN',
                answer TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, UNIQUE(account_id,topic))''')

    def record(self, db, job, assumptions):
        now = utc_now()
        db.execute('UPDATE auto_reply_jobs SET assumptions=? WHERE id=?',
                   (json.dumps(assumptions, ensure_ascii=False), job['id']))
        for item in assumptions:
            db.execute('''INSERT INTO reply_owner_questions
                (id,account_id,topic,job_id,phone,assumption,question,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id,topic) DO NOTHING''',
                (str(uuid.uuid4()), self.account_id, item['topic'], job['id'], job['phone'],
                 item['assumption'], item['question'], now, now))

    def questions(self):
        with closing(self.replies.inbox.connect()) as db:
            return [dict(row) for row in db.execute('''SELECT id,topic,job_id,phone,assumption,
                question,state,answer,created_at,updated_at FROM reply_owner_questions
                WHERE account_id=? ORDER BY CASE WHEN state='OPEN' THEN 0 ELSE 1 END,updated_at DESC''',
                (self.account_id,))]

    def rules(self, db):
        rows = db.execute("SELECT topic,question,answer FROM reply_owner_questions WHERE account_id=? AND state='ANSWERED' ORDER BY updated_at,id",
                          (self.account_id,)).fetchall()
        # Authenticated owner answers travel through stdin, avoiding Windows'
        # command-line size limit as the durable rule library grows. POLICY
        # delegates business-rule authority only to this exact top-level field.
        return [{'topic': r['topic'], 'scenario': r['question'], 'owner_rule': r['answer']} for r in rows]

    def answer(self, question_id, answer):
        if not isinstance(question_id, str) or not isinstance(answer, str) or not answer.strip() or len(answer) > 2000:
            raise ValueError('Write the future rule (1–2,000 characters).')
        replies = self.replies
        with replies.dispatch_lock, closing(replies.inbox.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM reply_owner_questions WHERE id=? AND account_id=?',
                             (question_id, self.account_id)).fetchone()
            if not row:
                raise ValueError('Owner question not found for this account.')
            if row['state'] == 'ANSWERED' and row['answer'] == answer.strip():
                return {'state': 'ANSWERED'}
            db.execute("UPDATE reply_owner_questions SET state='ANSWERED',answer=?,updated_at=? WHERE id=? AND account_id=?",
                       (answer.strip(), utc_now(), question_id, self.account_id))
            db.execute(f'UPDATE {replies.settings_table} SET revision=revision+1 WHERE id=?',(replies.settings_id,))
            # Existing approvals must not use an older business rule.
            scope = ' AND EXISTS (SELECT 1 FROM account_sources s WHERE s.source_id=auto_reply_jobs.source_id AND s.account_id=?)' if replies.review else ''
            db.execute("UPDATE auto_reply_jobs SET state='STALE',reason='A saved owner rule changed. Review a fresh draft.',updated_at=? WHERE state='DRAFT'" + scope,
                       (utc_now(), self.account_id) if replies.review else (utc_now(),))
        replies.service.event('OWNER_RULE_SAVED', 'A dashboard answer was saved for future reply drafts.')
        replies.wake.set()
        return {'state': 'ANSWERED'}
