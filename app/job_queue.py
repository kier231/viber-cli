"""Durable account queues with one serialized desktop executor."""
from contextlib import closing
from datetime import datetime, timezone, timedelta
import json
import threading
import uuid

from app.storage import connect
from app.accounts import stamp


class JobQueue:
    def __init__(self,target):
        self.target = target
        with closing(connect(target)) as db, db:
            db.executescript('''CREATE TABLE IF NOT EXISTS controller_jobs (
                id TEXT PRIMARY KEY, account_id TEXT NOT NULL, operation_id TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL, phone TEXT, payload TEXT NOT NULL, priority INTEGER NOT NULL,
                due_at TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'PENDING', worker_id TEXT,
                epoch TEXT, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
                result TEXT, error TEXT);
                CREATE INDEX IF NOT EXISTS controller_jobs_ready ON controller_jobs(account_id,state,due_at,priority);
                CREATE INDEX IF NOT EXISTS controller_jobs_contact ON controller_jobs(account_id,phone,state,created_at);
            ''')
            if 'reviewed_at' not in {r[1] for r in db.execute('PRAGMA table_info(controller_jobs)')}:
                db.execute('ALTER TABLE controller_jobs ADD COLUMN reviewed_at TEXT')

    def enqueue(self,account_id,operation_id,kind,payload=None,phone=None,priority=10,due_at=None):
        value = json.dumps(payload or {},sort_keys=True,ensure_ascii=False)
        job_id = str(uuid.uuid4())
        with closing(connect(self.target)) as db, db:
            db.execute('INSERT OR IGNORE INTO controller_jobs(id,account_id,operation_id,kind,phone,payload,priority,due_at,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                (job_id,account_id,operation_id,kind,phone,value,priority,due_at or stamp(),stamp()))
            row = db.execute('SELECT * FROM controller_jobs WHERE operation_id=?',(operation_id,)).fetchone()
            if row['account_id']!=account_id or row['payload']!=value or row['kind']!=kind or row['phone']!=phone:
                raise ValueError('Submission key already belongs to another job.')
            return dict(row)

    def claim(self,account_id,worker_id,epoch):
        with closing(connect(self.target)) as db, db:
            if getattr(db,'dialect',None) != 'postgresql':
                db.execute('BEGIN IMMEDIATE')
            else:
                if not db.execute("SELECT pg_try_advisory_xact_lock(hashtext(?))", ('queue:'+account_id,)).fetchone()[0]:
                    return None
            if db.execute("SELECT 1 FROM controller_jobs WHERE account_id=? AND state='RUNNING' LIMIT 1", (account_id,)).fetchone():
                return None
            if db.execute("SELECT 1 FROM information_schema.tables WHERE table_schema=current_schema() AND table_name='accounts'").fetchone() if getattr(db,'dialect',None)=='postgresql' else db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounts'").fetchone():
                account = db.execute('SELECT enabled,paused,next_send_at FROM accounts WHERE id=?', (account_id,)).fetchone()
                if not account or not account['enabled'] or account['paused'] or (account['next_send_at'] and account['next_send_at']>stamp()):
                    return None
            lock = ' FOR UPDATE OF j SKIP LOCKED' if getattr(db,'dialect',None)=='postgresql' else ''
            sql = '''SELECT j.* FROM controller_jobs j WHERE j.account_id=? AND j.state='PENDING' AND j.due_at<=?
                AND NOT EXISTS(SELECT 1 FROM controller_jobs older WHERE older.account_id=j.account_id
                  AND j.phone IS NOT NULL AND older.phone=j.phone AND (older.state IN ('PENDING','RUNNING') OR (older.state='UNKNOWN' AND older.reviewed_at IS NULL))
                  AND (older.created_at<j.created_at OR (older.created_at=j.created_at AND older.rowid<j.rowid)))
                ORDER BY j.priority DESC,j.created_at,j.rowid LIMIT 1''' + lock
            row = db.execute(sql,(account_id,stamp())).fetchone()
            if not row:
                return None
            changed = db.execute("UPDATE controller_jobs SET state='RUNNING',worker_id=?,epoch=?,started_at=? WHERE id=? AND state='PENDING'",(worker_id,epoch,stamp(),row['id']))
            result = dict(row)
            result['started_at'] = stamp()
            return result if changed.rowcount==1 else None

    def finish(self,job_id,worker_id,epoch,state,result=None,error=None):
        if state not in ('DONE','FAILED','UNKNOWN','INTERRUPTED'):
            raise ValueError('Invalid job outcome.')
        with closing(connect(self.target)) as db, db:
            row = db.execute('SELECT * FROM controller_jobs WHERE id=?',(job_id,)).fetchone()
            if not row or row['worker_id']!=worker_id or row['epoch']!=epoch:
                raise PermissionError('This result belongs to another worker.')
            encoded = json.dumps(result) if result is not None else None
            if row['state']!='RUNNING':
                if row['state']==state and row['result']==encoded and row['error']==error:
                    return
                raise ValueError('The job already has a different result.')
            changed = db.execute("UPDATE controller_jobs SET state=?,result=?,error=?,finished_at=? WHERE id=? AND state='RUNNING'",(state,encoded,error,stamp(),job_id))
            if changed.rowcount != 1:
                current=db.execute('SELECT state,result,error FROM controller_jobs WHERE id=?',(job_id,)).fetchone()
                if current['state']==state and current['result']==encoded and current['error']==error:
                    return
                raise ValueError('The job was completed concurrently.')
            if state=='DONE' and isinstance(result,dict) and result.get('state')=='DISPATCHED':
                account=db.execute('SELECT send_gap_seconds FROM accounts WHERE id=?',(row['account_id'],)).fetchone()
                if not account:raise ValueError('Dispatch account configuration is missing.')
                db.execute('UPDATE accounts SET next_send_at=? WHERE id=?',(stamp(account[0]),row['account_id']))

    def status(self):
        with closing(connect(self.target)) as db:
            return [dict(r) for r in db.execute('SELECT account_id,state,COUNT(*) AS count,MIN(created_at) AS oldest FROM controller_jobs GROUP BY account_id,state')]


class DesktopQueue:
    def __init__(self,service):
        self.service = service
        self.queue = JobQueue(service.store.path)
        self.callbacks = {}
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.accepting = True
        self.thread = threading.Thread(target=self._loop,name='viber-desktop',daemon=True)
        # Restarted running GUI actions have an uncertain result, never a retry.
        with closing(connect(service.store.path)) as db, db:
            db.execute("""UPDATE controller_jobs SET state='FAILED',
                error='Automatic reply stopped before its durable Send marker.',finished_at=?
                WHERE account_id=? AND state='RUNNING' AND kind='automatic-reply'
                  AND EXISTS(SELECT 1 FROM auto_reply_jobs a WHERE a.operation_id=controller_jobs.operation_id AND a.state='RETRY')
                  AND NOT EXISTS(SELECT 1 FROM web_sends s WHERE s.operation_id=controller_jobs.operation_id AND s.attempted_at IS NOT NULL)""",
                (stamp(),service.accounts.account_id))
            db.execute("""UPDATE web_operations SET state='FAILED',error='Reply interrupted before Send.'
                WHERE id IN (SELECT operation_id FROM controller_jobs WHERE account_id=? AND state='FAILED'
                  AND error='Automatic reply stopped before its durable Send marker.')""",(service.accounts.account_id,))
            db.execute("UPDATE controller_jobs SET state='UNKNOWN',error='Worker stopped during execution; review before retrying.',finished_at=? WHERE state='RUNNING' AND account_id=?",(stamp(),service.accounts.account_id))
        self.heartbeat_thread = threading.Thread(target=self._heartbeat,name='viber-heartbeat',daemon=True)
        self.heartbeat_thread.start()
        self.thread.start()

    def _heartbeat(self):
        while True:
            try:
                a = self.service.accounts
                a.heartbeat(a.token,a.account_id,a.worker_id,a.epoch)
            except Exception as exc:
                self.service.event('WORKER_HEARTBEAT_ERROR',str(exc))
            if self.stop.wait(10):
                return

    def submit(self,function,operation_id,work,metadata=None):
        metadata = metadata or {}
        with self.lock:
            if not self.accepting:
                raise ValueError('The desktop queue is stopping.')
            with closing(connect(self.service.store.path)) as db:
                operation = db.execute('SELECT kind FROM web_operations WHERE id=?',(operation_id,)).fetchone()
                send = db.execute('SELECT * FROM web_sends WHERE operation_id=?',(operation_id,)).fetchone()
            if send:
                metadata.setdefault('payload',{'send_id':send['id']})
                metadata.setdefault('phone',send['phone'])
            kind = operation['kind']
            metadata.setdefault('priority',100 if kind=='automatic-reply' else 10)
            account = self.service.accounts.account_id if self.service.accounts else 'local'
            self.queue.enqueue(account,operation_id,kind,**metadata)
            self.callbacks[operation_id] = (function,work)
            self.wake.set()

    def _restore(self,job):
        payload = json.loads(job['payload'])
        if payload.get('reply_job_id'):
            return lambda: self.service.replies._dispatch(payload['reply_job_id'])
        if payload.get('send_id'):
            with closing(connect(self.service.store.path)) as db:
                row = db.execute('SELECT * FROM web_sends WHERE id=?',(payload['send_id'],)).fetchone()
            if row and row['state']=='QUEUED' and row['lead_snapshot']:
                preview = {'lead':json.loads(row['lead_snapshot']),'text':row['text'],'viber_name':row['viber_name']}
                for key in ('campaign_id','scheduled_at'):
                    if row[key]: preview[key]=row[key]
                return lambda: self.service._send(row['id'],preview)
        def interrupted():
            raise ValueError('This interrupted UI operation needs to be requested again.')
        return interrupted

    def _loop(self):
        worker = self.service.accounts.worker_id if self.service.accounts else 'local'
        epoch = self.service.accounts.epoch if self.service.accounts else 'local'
        account = self.service.accounts.account_id if self.service.accounts else 'local'
        while True:
            job = None
            if self.stop.is_set(): return
            try:
                if self.service.accounts:
                    self.service.accounts.authenticate(self.service.accounts.token,account,worker,epoch)
                job = self.queue.claim(account,worker,epoch)
                if not job:
                    if self.stop.is_set(): return
                    self.wake.wait(.2); self.wake.clear(); continue
                with self.lock:
                    callback = self.callbacks.pop(job['operation_id'],None)
                if callback:
                    function,work = callback
                else:
                    function,work = self.service._run,self._restore(job)
                    with self.service.lock: self.service.pending += 1
                function(job['operation_id'],work)
                operation = self.service.operation(job['operation_id'])
                with closing(connect(self.service.store.path)) as db:
                    uncertain = db.execute("SELECT 1 FROM web_sends WHERE operation_id=? AND state='UNKNOWN'",(job['operation_id'],)).fetchone()
                state = 'UNKNOWN' if uncertain else 'DONE' if operation['state']=='SUCCEEDED' else 'FAILED'
                self.queue.finish(job['id'],worker,epoch,state,operation.get('result'),operation.get('error'))
            except Exception as exc:
                if 'job' in locals() and job:
                    try:
                        self.queue.finish(job['id'],worker,epoch,'INTERRUPTED',error=str(exc))
                        with self.service.lock: self.service.pending=max(0,self.service.pending-1)
                    except Exception: pass
                if self.stop.is_set(): return
                self.stop.wait(.5)

    def shutdown(self,wait=True):
        with self.lock: self.accepting=False
        self.stop.set(); self.wake.set()
        if wait: self.thread.join(timeout=90)
        self.heartbeat_thread.join(timeout=12)
        if self.thread.is_alive():
            raise RuntimeError('Desktop worker is still active; replacement is not permitted.')
