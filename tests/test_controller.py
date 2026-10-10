from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid

from app.accounts import Accounts, stamp
from app.job_queue import JobQueue
from app.models import LeadStore
from app.storage import connect
from app.web_service import WebService
from app.auto_replies import AutoReplies
from app.codex_replies import DEFAULT_INSTRUCTIONS
from test_auto_replies import Desktop, Generator, Source
from test_viber_inbox import message,snapshot


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'queue.db'
        self.accounts = Accounts(self.path)
        self.queue = JobQueue(self.path)
        with closing(connect(self.path)) as db,db:
            for i in range(10):
                db.execute('INSERT INTO accounts(id,phone,enabled,created_at) VALUES(?,?,1,?)',(str(i),f'+3816400000{i:02}',stamp()))

    def tearDown(self):
        self.temp.cleanup()

    def test_ownership_race_and_fencing(self):
        def claim(i):
            try: return self.accounts.claim('+381649999999',str(i))
            except PermissionError: return None
        with ThreadPoolExecutor(10) as pool:
            winners = [r for r in pool.map(claim,range(10)) if r is not None]
        self.assertEqual(len(winners),1)
        self.accounts.register('0','old','x'*32,'old')
        with self.assertRaises(PermissionError):
            self.accounts.register('0','replacement','y'*32,'new')
        self.accounts.register('0','replacement','y'*32,'new',True)
        with self.assertRaises(PermissionError):
            self.accounts.authenticate('x'*32,'0','old','old')

    def test_dispatch_completion_applies_cooldown_atomically(self):
        with closing(connect(self.path)) as db,db:
            db.execute('UPDATE accounts SET send_gap_seconds=300 WHERE id=?',('0',))
        self.queue.enqueue('0','first','send')
        self.queue.enqueue('0','second','send')
        job=self.queue.claim('0','worker','epoch')
        self.queue.finish(job['id'],'worker','epoch','DONE',{'state':'DISPATCHED'})
        self.assertIsNone(self.queue.claim('0','worker','epoch'))

    def test_priority_order_schedule_duplicates_and_uncertainty(self):
        first = self.queue.enqueue('0','first','outreach',phone='p',priority=10)
        self.queue.enqueue('0','second','reply',phone='p',priority=100)
        self.queue.enqueue('0','other','reply',phone='q',priority=100)
        self.queue.enqueue('0','future','reply',phone='z',priority=200,due_at=stamp(300))
        with closing(connect(self.path)) as db,db:
            db.execute('UPDATE accounts SET next_send_at=? WHERE id=?',(stamp(300),'0'))
        self.assertIsNone(self.queue.claim('0','worker','epoch'))
        with closing(connect(self.path)) as db,db:
            db.execute('UPDATE accounts SET next_send_at=NULL WHERE id=?',('0',))
        self.assertEqual(first['id'],self.queue.enqueue('0','first','outreach',phone='p',priority=10)['id'])
        with ThreadPoolExecutor(10) as pool:
            claims = [r for r in pool.map(lambda _:self.queue.claim('0','worker','epoch'),range(10)) if r]
        self.assertEqual(len(claims),1)
        self.assertEqual(claims[0]['operation_id'],'other')
        self.queue.finish(claims[0]['id'],'worker','epoch','DONE')
        self.queue.finish(claims[0]['id'],'worker','epoch','DONE')
        row = self.queue.claim('0','worker','epoch')
        self.assertEqual(row['operation_id'],'first')
        self.queue.finish(row['id'],'worker','epoch','UNKNOWN')
        self.assertIsNone(self.queue.claim('0','worker','epoch'))

    def test_thousand_jobs_ten_simulated_accounts(self):
        created = stamp()
        with closing(connect(self.path)) as db,db:
            db.executemany('INSERT INTO controller_jobs(id,account_id,operation_id,kind,payload,priority,due_at,created_at) VALUES(?,?,?,?,?,10,?,?)',
                [(str(uuid.uuid4()),str(i%10),str(i),'simulation','{}',created,created) for i in range(1000)])
        def worker(i):
            completed=[]
            while job:=self.queue.claim(str(i),'simulation-'+str(i),'epoch'):
                completed.append(job['id'])
                self.queue.finish(job['id'],'simulation-'+str(i),'epoch','DONE')
            return completed
        with ThreadPoolExecutor(10) as pool:
            jobs=[job for group in pool.map(worker,range(10)) for job in group]
        self.assertEqual(len(jobs),1000)
        self.assertEqual(len(set(jobs)),1000)

    @unittest.skipUnless(os.environ.get('VIBER_TEST_POSTGRES_URL'),'PostgreSQL migration test')
    def test_full_sqlite_migration_preserves_ids_checkpoints_and_order(self):
        from app.storage import import_sqlite,PostgresConnection
        target=os.environ.pop('VIBER_TEST_POSTGRES_URL')
        original=Path(self.temp.name)/'original.sqlite3'
        try:
            legacy=WebService(original)
            lead=legacy.store.create_with_android('+381641234567','Original',lambda *_:None)
            legacy.watcher.inbox.ingest(snapshot(message(73)))
            legacy.close()
        finally:
            os.environ['VIBER_TEST_POSTGRES_URL']=target
        schema='migration_'+uuid.uuid4().hex
        report=import_sqlite(original,target,schema)
        self.assertEqual(report['leads'],1)
        self.assertEqual(report['viber_messages'],1)
        with closing(PostgresConnection(target,schema)) as db:
            self.assertEqual(db.execute('SELECT id,company_name FROM leads').fetchone()[0],lead.id)
            self.assertEqual(db.execute('SELECT checkpoint FROM viber_sources').fetchone()[0],73)
            self.assertEqual(db.execute('SELECT rowid FROM viber_messages').fetchone()[0],1)
        with self.assertRaises(ValueError):import_sqlite(original,target,schema)


class DraftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'ledger.db'
        self.desktop = Desktop()
        seed = WebService(self.path,lambda:self.desktop,lambda *_:None)
        seed.store.create_with_android('+381641234567','Business',lambda *_:None)
        seed.watcher.inbox.ingest(snapshot(message(1,'OUTGOING',timestamp_ms=int(time.time()*1000)-10000)))
        seed.close()
        self.service = WebService(self.path,lambda:self.desktop,lambda *_:None,managed=True)
        self.source = Source(snapshot(message(1,'OUTGOING',timestamp_ms=int(time.time()*1000)-10000)))
        self.service.watcher.source = self.source
        self.service.replies.close()
        self.generator = Generator()
        self.replies = self.service.replies = AutoReplies(self.service,self.generator,settle_seconds=0)
        self.service.watcher.on_ingest = self.replies.wake.set
        self.service.watcher.poll_once()
        self.replies.configure(True,DEFAULT_INSTRUCTIONS)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def incoming(self,event=2):
        self.source.data['messages'].append(message(event))
        self.source.data['max_event_id']=event
        self.service.watcher.poll_once()

    def wait_state(self,state):
        until=time.monotonic()+5
        while time.monotonic()<until:
            jobs=self.replies.jobs()
            if jobs and jobs[0]['state']==state:return jobs[0]
            time.sleep(.01)
        self.fail(f'Did not reach {state}: {self.replies.jobs()}')

    def test_draft_requires_approval_and_stale_approval_rejected(self):
        self.incoming()
        self.replies.tick()
        draft=self.wait_state('DRAFT')
        self.assertEqual(self.desktop.sent,[])
        self.incoming(3)
        with self.assertRaises(ValueError):self.replies.review_draft(draft['id'],True)
        self.wait_state('STALE')
        self.assertEqual(self.desktop.sent,[])

    def test_approved_uncertain_send_is_not_retried(self):
        self.incoming()
        self.replies.tick()
        draft=self.wait_state('DRAFT')
        self.desktop.fail=True
        self.replies.review_draft(draft['id'],True)
        self.wait_state('UNKNOWN')
        self.replies.tick()
        self.assertEqual(len(self.desktop.sent),1)
        with self.assertRaises(ValueError):self.replies.review_draft(draft['id'],True)

    def test_explicit_uncertain_review_allows_future_draft_without_retry(self):
        self.incoming();self.replies.tick()
        draft=self.wait_state('DRAFT')
        self.desktop.fail=True
        self.replies.review_draft(draft['id'],True)
        self.wait_state('UNKNOWN')
        self.replies.conversation_control(draft['source_id'],draft['chat_id'],True)
        self.incoming(3);self.replies.tick()
        fresh=self.wait_state('DRAFT')
        self.assertNotEqual(fresh['id'],draft['id'])
        self.assertEqual(len(self.desktop.sent),1)
        with self.assertRaises(ValueError):self.replies.review_draft(draft['id'],True)

    def test_generation_ignores_unrelated_desktop_work_and_wakes_quickly(self):
        gate=threading.Event()
        self.generator.callback=lambda:gate.wait(3)
        self.service._enqueue('unrelated',lambda:gate.wait(3))
        self.replies.settle_seconds=1
        self.replies.start()
        begin=time.monotonic()
        self.incoming()
        self.wait_state('GENERATING')
        deadline=begin+2.5
        while not self.generator.contexts and time.monotonic()<deadline:time.sleep(.01)
        elapsed=time.monotonic()-begin
        gate.set()
        self.assertLess(elapsed,2.5)
        self.wait_state('DRAFT')

    def test_two_chats_generate_concurrently_and_one_per_chat(self):
        self.service.store.create_with_android('+381641234568','Other',lambda *_:None)
        self.service.accounts.claim('+381641234568','current')
        data=self.source.data
        data['chats'].append({**data['chats'][0],'chat_id':11,'phone':'+381641234568','peer_id':3})
        data['messages'].append(message(2,'OUTGOING',chat_id=11,timestamp_ms=int(time.time()*1000)-10000))
        data['max_event_id']=2
        self.service.watcher.poll_once()
        data['messages'] += [message(3),message(4,chat_id=11,sender_id=3)]
        data['max_event_id']=4
        self.service.watcher.poll_once()
        barrier=threading.Barrier(2)
        self.generator.callback=lambda:barrier.wait(3)
        self.replies.tick();self.replies.tick();self.replies.tick()
        until=time.monotonic()+5
        while time.monotonic()<until and sum(r['state']=='DRAFT' for r in self.replies.jobs())<2:time.sleep(.02)
        self.assertEqual(sum(r['state']=='DRAFT' for r in self.replies.jobs()),2)
        self.assertEqual(len(self.generator.contexts),2)

    def test_graceful_shutdown_stops_old_worker(self):
        self.service.close()
        self.assertFalse(self.service.pool.thread.is_alive())
        self.assertFalse(self.service.pool.heartbeat_thread.is_alive())

    def test_live_old_process_cannot_be_replaced_even_if_lease_expires(self):
        with closing(connect(self.path)) as db,db:
            db.execute('UPDATE account_workers SET lease_until=? WHERE account_id=?',(stamp(-60),'current'))
        with self.assertRaises(PermissionError):Accounts(self.path).bind_current()

    def test_fastapi_browser_security_and_worker_authentication(self):
        from fastapi.testclient import TestClient
        from app.controller import create_app
        client=TestClient(create_app(self.service),base_url='http://127.0.0.1:4001')
        self.assertEqual(client.get('/viber').status_code,200)
        self.assertEqual(client.get('/viber/api/status').status_code,403)
        session=client.post('/viber/api/session',json={},headers={'Origin':'http://127.0.0.1:4001','X-Viber-Browser':'1'})
        self.assertEqual(session.status_code,200)
        headers={'X-Viber-CSRF':session.json()['csrf']}
        self.assertEqual(client.get('/viber/api/status',headers=headers).status_code,200)
        self.assertEqual(client.post('/viber/api/replies/review',json={'job_id':'missing','approve':True},headers={**headers,'Origin':'http://evil.example'}).status_code,403)
        a=self.service.accounts
        identity={'account_id':a.account_id,'worker_id':a.worker_id,'epoch':a.epoch}
        self.assertEqual(client.post('/worker/heartbeat',json=identity).status_code,403)
        self.assertEqual(client.post('/worker/heartbeat',json=identity,headers={'Authorization':'Bearer '+a.token}).status_code,200)

    def test_ownership_change_invalidates_draft(self):
        self.incoming();self.replies.tick()
        draft=self.wait_state('DRAFT')
        with closing(connect(self.path)) as db,db:
            db.execute("INSERT INTO accounts(id,phone,enabled,created_at) VALUES('other','+381641111112',0,?)",(stamp(),))
            db.execute("UPDATE contact_owners SET account_id='other' WHERE phone=?",(draft['phone'],))
        with self.assertRaises(PermissionError):self.replies.review_draft(draft['id'],True)
        self.assertEqual(self.desktop.sent,[])

    def test_foreign_owned_sender_is_filtered_without_blocking_owned_inbox(self):
        with closing(connect(self.path)) as db,db:
            db.execute("INSERT INTO accounts(id,phone,enabled,created_at) VALUES('other','+381641111112',0,?)",(stamp(),))
            db.execute("INSERT INTO contact_owners VALUES(?,'other',?)",('+381641234568',stamp()))
        data=self.source.data
        data['chats'].append({**data['chats'][0],'chat_id':11,'phone':'+381641234568','peer_id':3})
        data['messages'] += [message(2),message(3,chat_id=11,sender_id=3)]
        data['max_event_id']=3
        self.assertIsNotNone(self.service.watcher.poll_once())
        self.assertEqual(len(self.service.watcher.inbox.conversations()),1)
        self.replies.tick()
        self.wait_state('DRAFT')
        self.assertEqual(len(self.generator.contexts),1)


if __name__=='__main__':unittest.main()
