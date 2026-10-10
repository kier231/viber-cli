from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from app.accounts import Accounts
from app.controller import create_app
from app.instances import Instances
from app.storage import connect
from app.web_service import WebService
from test_auto_replies import Desktop, Generator, Source
from test_viber_inbox import snapshot


def identity(number, source):
    return {**snapshot(source=source), 'account_phone':number, 'chats':[], 'messages':[]}


class InstanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'ledger.db'
        seed = WebService(self.path,lambda:Desktop(),lambda *_:None)
        seed.store.create_with_android('+381641234567','Old contact',lambda *_:None)
        seed.watcher.inbox.ingest(snapshot())
        seed.close()
        old = Accounts(self.path)
        old.bind_current()
        old.stopped()
        with closing(connect(self.path)) as db,db:
            db.execute("UPDATE accounts SET enabled=0,paused=1 WHERE id='current'")
        self.limit = threading.BoundedSemaphore(2)
        self.one = self.service('instance-1','+381641111112','new-1')
        self.two = None

    def service(self, account, phone, source):
        return WebService(self.path,lambda:Desktop(),lambda *_:None,managed=True,
                          account_id=account,identity_snapshot=identity(phone,source),generation_limit=self.limit)

    def tearDown(self):
        if self.two:
            self.two.close()
        self.one.close()
        self.temp.cleanup()

    def completed(self, task):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = self.one.operation(task['operation_id'])
            if result['state'] not in ('QUEUED', 'RUNNING'):
                return result
            time.sleep(.01)
        self.fail('Contact addition did not finish')

    def contact_desktop(self):
        desktop = Mock(wraps=Desktop())
        self.one.client_factory = lambda: desktop
        # Mock.connect must return the same observable desktop wrapper.
        desktop.connect.return_value = desktop
        self.one.android_add = Mock(side_effect=AssertionError('Android must not be called'))
        self.one.watcher.source = Source(identity('+381641111112', 'new-1'))
        return desktop

    def assert_not_claimed(self, phone):
        with closing(connect(self.path)) as db:
            self.assertIsNone(db.execute('SELECT 1 FROM contact_owners WHERE phone=?', (phone,)).fetchone())
            self.assertIsNone(db.execute('SELECT 1 FROM leads WHERE phone=?', (phone,)).fetchone())

    def test_dashboard_contact_is_verified_by_and_owned_by_selected_account(self):
        desktop = self.contact_desktop()
        self.two = self.service('instance-2','+381641111113','new-2')
        result = self.completed(self.one.add_contact({'phone':'0651234567', 'company_name':'New Salon'}))
        self.assertEqual(result['state'], 'SUCCEEDED', result['error'])
        desktop.open_phone.assert_called_once_with('+381651234567')
        desktop.send_message.assert_not_called()
        self.one.android_add.assert_not_called()
        self.assertEqual([r['phone'] for r in self.one.contacts()], ['+381651234567'])
        self.assertEqual(self.two.contacts(), [])
        self.two.client_factory = Mock(side_effect=AssertionError('Other VM must not be opened'))
        with self.assertRaises(ValueError):
            self.two.add_contact({'phone':'0651234567', 'company_name':'Other account'})
        self.two.client_factory.assert_not_called()

    def test_bad_account_identity_blocks_contact_before_desktop_access(self):
        desktop = self.contact_desktop()
        self.one.watcher.source.data['account_phone'] = '+381641111113'
        result = self.completed(self.one.add_contact({'phone':'0651234567', 'company_name':'Salon'}))
        self.assertEqual(result['state'], 'FAILED')
        desktop.open_phone.assert_not_called()
        self.assert_not_claimed('+381651234567')

    def test_account_identity_change_during_verification_does_not_claim_contact(self):
        desktop = self.contact_desktop()
        def changed(phone):
            self.one.watcher.source.data['account_phone'] = '+381641111113'
            return 'Person'
        desktop.open_phone.side_effect = changed
        result = self.completed(self.one.add_contact({'phone':'0651234567', 'company_name':'Salon'}))
        self.assertEqual(result['state'], 'FAILED')
        desktop.send_message.assert_not_called()
        self.assert_not_claimed('+381651234567')

    def test_recipient_or_database_failure_rolls_back_new_ownership(self):
        desktop = self.contact_desktop()
        desktop.open_phone.side_effect = ValueError('Recipient unavailable')
        result = self.completed(self.one.add_contact({'phone':'0651234567', 'company_name':'Salon'}))
        self.assertEqual(result['state'], 'FAILED')
        self.assert_not_claimed('+381651234567')
        desktop.open_phone.side_effect = None
        desktop.open_phone.return_value = 'Person'
        with patch.object(self.one.store, 'create_verified', side_effect=ValueError('Storage failure')):
            result = self.completed(self.one.add_contact({'phone':'0651234567', 'company_name':'Salon'}))
        self.assertEqual(result['state'], 'FAILED')
        self.assert_not_claimed('+381651234567')

    def test_other_account_claim_during_verification_wins_without_reassignment(self):
        desktop = self.contact_desktop()
        self.two = self.service('instance-2','+381641111113','new-2')
        def other_claim(phone):
            self.two.accounts.claim(phone, 'instance-2')
            return 'Person'
        desktop.open_phone.side_effect = other_claim
        result = self.completed(self.one.add_contact({'phone':'0651234567', 'company_name':'Salon'}))
        self.assertEqual(result['state'], 'FAILED')
        with closing(connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT account_id FROM contact_owners WHERE phone=?', ('+381651234567',)).fetchone()[0], 'instance-2')
            self.assertIsNone(db.execute('SELECT 1 FROM leads WHERE phone=?', ('+381651234567',)).fetchone())
        desktop.send_message.assert_not_called()

    def test_ownership_already_reserved_by_another_account_blocks_before_verification(self):
        desktop = self.contact_desktop()
        self.two = self.service('instance-2','+381641111113','new-2')
        self.two.accounts.claim('+381651234567', 'instance-2')
        with self.assertRaises(PermissionError):
            self.one.add_contact({'phone':'0651234567', 'company_name':'Salon'})
        desktop.open_phone.assert_not_called()

    def test_two_numbers_keep_old_contacts_ignored_and_settings_separate(self):
        self.two = self.service('instance-2','+381641111113','new-2')
        self.assertEqual(self.one.contacts(),[])
        self.assertEqual(self.two.contacts(),[])
        self.assertEqual(self.one.watcher.inbox.status()['messages'],0)
        self.assertIsNone(self.one.watcher.inbox.status()['last_poll'])
        self.one.event('FIRST','First account event')
        self.two.event('SECOND','Second account event')
        self.assertEqual([r['kind'] for r in self.one.records('events')],['FIRST'])
        self.assertEqual([r['kind'] for r in self.two.records('events')],['SECOND'])
        old = next(a for a in self.one.accounts.list() if a['id']=='current')
        self.assertEqual((old['enabled'],old['paused']),(0,1))
        with self.assertRaises(PermissionError):
            self.one.accounts.claim('+381641234567','instance-1')
        ignored = snapshot(source='new-1')
        ignored['account_phone']='+381641111112'
        self.one.accounts.source(ignored)
        self.assertEqual(ignored['chats'],[])
        self.one.replies.generator = Generator()
        self.two.replies.generator = Generator()
        self.two.replies.configure(False,self.two.replies.settings()['instructions'])
        self.assertTrue(self.one.replies.settings()['enabled'])
        self.assertFalse(self.two.replies.settings()['enabled'])

    def test_binding_duplicate_number_or_source_is_rejected(self):
        other = Accounts(self.path,'instance-2')
        for native in (identity('+381641111112','new-2'),identity('+381641111113','new-1'),identity('+381641111111','new-2')):
            with self.assertRaises(PermissionError):
                other.bind_snapshot(native)

    def test_second_worker_does_not_interrupt_first_operations_or_drafts(self):
        with closing(connect(self.path)) as db,db:
            db.execute("INSERT INTO web_operations(id,kind,state,created_at,account_id) VALUES('running','prepare','RUNNING','2026-10-10','instance-1')")
            db.execute("INSERT INTO auto_reply_jobs(id,source_id,chat_id,trigger_id,revision,settings_revision,phone,lead_snapshot,send_fingerprint,state,created_at,updated_at) VALUES('drafting','new-1',10,1,1,1,'+381642222222','{}','','GENERATING','2026-10-10','2026-10-10')")
        self.two = self.service('instance-2','+381641111113','new-2')
        self.assertEqual(self.one.operation('running')['state'],'RUNNING')
        with closing(connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT state FROM auto_reply_jobs WHERE id='drafting'").fetchone()[0],'GENERATING')
        with self.assertRaises(PermissionError):
            self.two.operation('running')

    def test_other_account_campaign_and_schedule_are_not_claimed(self):
        self.two = self.service('instance-2','+381641111113','new-2')
        self.one.accounts.claim('+381642222222','instance-1')
        with closing(connect(self.path)) as db,db:
            db.execute("INSERT INTO web_campaigns(id,creation_key,creation_hash,name,template,rules,state,created_at,updated_at,account_id) VALUES('campaign','key','','First','hello','{}','PAUSED','2026-10-10','2026-10-10','instance-1')")
            db.execute("INSERT INTO web_sends(id,request_key,preview_hash,operation_id,lead_id,phone,company_name,viber_name,text,state,created_at,updated_at,scheduled_at) VALUES('scheduled','scheduled-key','','',1,'+381642222222','Business','Person','hello','SCHEDULED','2026-10-10','2026-10-10','2026-01-01T00:00:00+00:00')")
        self.assertEqual(self.two.campaigns.list(),[])
        with self.assertRaises(PermissionError):
            self.two.campaigns.detail('campaign')
        with self.assertRaises(PermissionError):
            self.two.cancel_scheduled('scheduled')
        self.assertIsNone(self.two._dispatch_due())
        with closing(connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT state FROM web_sends WHERE id='scheduled'").fetchone()[0],'SCHEDULED')

    def test_pending_slots_expose_setup_but_block_work_and_wrong_account(self):
        manifest = Path(self.temp.name)/'instances.json'
        manifest.write_text(json.dumps({'instances':[{'id':f'instance-{n}','label':f'Viber {n}','state':'AWAITING_NUMBER'} for n in (1,2)]}),encoding='utf-8-sig')
        manager = Instances(self.path,manifest)
        with patch.object(manager,'start'), TestClient(create_app(manager),base_url='http://127.0.0.1:4001') as client:
            session = client.post('/viber/api/session',json={},headers={'Origin':'http://127.0.0.1:4001','X-Viber-Browser':'1'})
            headers = {'X-Viber-CSRF':session.json()['csrf'],'X-Viber-Account':'instance-2'}
            response = client.get('/viber/api/instances',headers=headers)
            self.assertEqual(len(response.json()['instances']),2)
            self.assertEqual(client.get('/viber/api/contacts',headers=headers).status_code,409)
            self.assertEqual(client.get('/viber/api/contacts',headers={**headers,'X-Viber-Account':'current'}).status_code,403)
            self.assertEqual(client.post('/worker/incoming',json={'account_id':'instance-2'}).status_code,403)

    def test_both_workers_share_two_generation_slots(self):
        self.two = self.service('instance-2','+381641111113','new-2')
        self.assertIs(self.one.generation_limit,self.two.generation_limit)
        active, maximum = 0, 0
        lock = threading.Lock()
        entered = threading.Event()
        release = threading.Event()
        def generation(service):
            nonlocal active, maximum
            with service.generation_limit:
                with lock:
                    active += 1
                    maximum = max(maximum,active)
                    if active == 2:
                        entered.set()
                release.wait(3)
                with lock:
                    active -= 1
        with ThreadPoolExecutor(4) as pool:
            futures = [pool.submit(generation,s) for s in (self.one,self.two,self.one,self.two)]
            self.assertTrue(entered.wait(2))
            self.assertEqual(maximum,2)
            release.set()
            for future in futures:
                future.result()
        self.assertEqual(maximum,2)

    def test_new_second_account_inherits_automatic_delivery_and_starts_enabled(self):
        with closing(connect(self.path)) as db,db:
            db.execute('UPDATE auto_reply_settings SET require_approval=0 WHERE id=1')
        self.two = self.service('instance-2','+381641111113','new-2')
        self.assertFalse(self.two.replies.settings()['review_required'])
        self.assertEqual(self.two.replies.settings()['delivery_mode'],'automatic')
        self.assertTrue(self.two.replies.settings()['enabled'])

    def test_default_enablement_survives_restart(self):
        self.assertTrue(self.one.replies.settings()['enabled'])
        with closing(connect(self.path)) as db:
            before = dict(db.execute("SELECT * FROM auto_reply_account_settings WHERE id='instance-1'").fetchone())
        self.one.close()
        self.one = self.service('instance-1','+381641111112','new-1')
        self.assertTrue(self.one.replies.settings()['enabled'])
        with closing(connect(self.path)) as db:
            after = dict(db.execute("SELECT * FROM auto_reply_account_settings WHERE id='instance-1'").fetchone())
        self.assertEqual(after,before)

    def test_explicit_pause_survives_restart(self):
        self.one.replies.configure(False,self.one.replies.settings()['instructions'])
        self.one.close()
        self.one = self.service('instance-1','+381641111112','new-1')
        self.assertFalse(self.one.replies.settings()['enabled'])

    def test_default_enabled_account_does_not_reply_to_imported_history(self):
        from test_viber_inbox import message
        self.one.store.create_with_android('+381642222222','New contact',lambda *_:None)
        self.one.accounts.claim('+381642222222','instance-1')
        data = snapshot(message(1, timestamp_ms=int(time.time()*1000)-10000), source='new-1')
        data['account_phone'] = '+381641111112'
        data['chats'][0]['phone'] = '+381642222222'
        self.one.watcher.source = Source(data)
        self.one.watcher.poll_once()
        self.one.replies.generator = Generator()
        self.one.replies.tick()
        self.assertEqual(self.one.replies.jobs(),[])
        self.assertEqual(self.one.replies.generator.contexts,[])

    def test_activation_rechecks_native_identity_after_vm_reader_cached_no_changes(self):
        from app.viber_source_worker import QtViberSource
        manifest = Path(self.temp.name)/'instances.json'
        manifest.write_text(json.dumps({'instances':[{'id':f'instance-{n}','label':f'Viber {n}',
            'state':'AWAITING_NUMBER','bridge_config':'unused'} for n in (1,2)]}),encoding='utf-8')
        manager = Instances(self.path,manifest)
        native = identity('+381641111112','new-1')
        reader = QtViberSource.__new__(QtViberSource)
        reader.viber_process = Mock()
        reader.viber_process.is_running.return_value = True
        reader.path, reader.identity, reader.last_version = Path('unused'), 'new-1', None
        reader.query = lambda sql: [{'data_version':7}] if sql=='PRAGMA data_version' else []
        bridge = Mock()
        bridge.call.side_effect = lambda path,payload=None,**kw: (
            {'status':'ready','instance_id':'instance-1'} if path=='/health' else reader.read(payload))
        service = Mock()
        with patch('app.viber_source_worker.source_identity',return_value='new-1'), \
             patch('app.viber_source_worker.read_snapshot',return_value=native), \
             patch('app.instances.load_vm_config',return_value={}), \
             patch('app.instances.VmBridge',return_value=bridge), \
             patch('app.instances.WebService',return_value=service) as factory:
            self.assertEqual(reader.read({'phones':[],'checkpoints':{}})['account_phone'],native['account_phone'])
            self.assertTrue(reader.read({'phones':[],'checkpoints':{}})['unchanged'])
            manager._activate(manager.slots['instance-1'])
        bound = factory.call_args.kwargs['identity_snapshot']
        self.assertEqual(bound['account_phone'],native['account_phone'])
        self.assertFalse(bound.get('unchanged',False))
        self.assertEqual(manager.slots['instance-1']['state'],'READY')

    def test_unlinked_manager_shutdown_requires_authentication_and_confirmation(self):
        manifest = Path(self.temp.name)/'instances.json'
        manifest.write_text(json.dumps({'instances':[{'id':f'instance-{n}'} for n in (1,2)]}))
        manager = Instances(self.path,manifest)
        manager.request_shutdown = Mock()
        client = TestClient(create_app(manager),base_url='http://127.0.0.1:4001')
        origin = {'Origin':'http://127.0.0.1:4001','X-Viber-Browser':'1'}
        self.assertEqual(client.post('/viber/api/shutdown',json={'confirmed':True},headers=origin).status_code,403)
        session = client.post('/viber/api/session',json={},headers=origin)
        headers = {**origin,'X-Viber-CSRF':session.json()['csrf']}
        self.assertEqual(client.post('/viber/api/shutdown',json={'confirmed':False},headers=headers).status_code,400)
        manager.request_shutdown.assert_not_called()
        self.assertEqual(client.post('/viber/api/shutdown',json={'confirmed':True},headers=headers).status_code,200)
        manager.request_shutdown.assert_called_once_with()
        self.assertEqual(client.get('/viber/api/contacts',headers=headers).status_code,409)

    def test_activation_keeps_owned_chat_and_detects_reply_received_while_offline(self):
        from test_viber_inbox import message
        from copy import deepcopy
        phone = '+381642222222'
        self.one.store.create_with_android(phone,'New contact',lambda *_:None)
        self.one.accounts.claim(phone,'instance-1')
        data = snapshot(message(1,'OUTGOING',timestamp_ms=int(time.time()*1000)-2000),source='new-1')
        data['account_phone'] = '+381641111112'
        data['chats'][0]['phone'] = phone
        self.one.accounts.source(data)
        self.one.watcher.inbox.ingest(data)
        with closing(connect(self.path)) as db:
            cutoff = db.execute("SELECT monitoring_since_ms FROM accounts WHERE id='instance-1'").fetchone()[0]
        data['messages'].append(message(2,body='jesmo'))
        data['max_event_id'] = 2
        manifest = Path(self.temp.name)/'instances.json'
        manifest.write_text(json.dumps({'instances':[{'id':f'instance-{n}','bridge_config':'unused'} for n in (1,2)]}))
        manager = Instances(self.path,manifest)
        bridge = Mock()
        bridge.call.side_effect = lambda path,payload=None,**kw: ({'status':'ready','instance_id':'instance-1'} if path=='/health' else deepcopy(data))
        service = Mock()
        service.accounts = self.one.accounts
        service.watcher.inbox = self.one.watcher.inbox
        with patch('app.instances.load_vm_config',return_value={}), \
             patch('app.instances.VmBridge',return_value=bridge), \
             patch('app.instances.WebService',return_value=service):
            manager._activate(manager.slots['instance-1'])
        request = next(call.args[1] for call in bridge.call.call_args_list if call.args[0]=='/source/read')
        self.assertEqual(request['phones'],[phone])
        self.assertEqual(request['incoming_since_ms'],cutoff)
        self.assertTrue(request['force_refresh'])
        with closing(connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT detection FROM viber_messages WHERE source_id='new-1' AND event_id=2").fetchone()[0],'NEW_INCOMING')

    def test_confirmed_history_reset_preserves_old_ids_and_baselines_new_epoch(self):
        from test_viber_inbox import message
        phone = '+381642222222'
        self.one.store.create_with_android(phone,'New contact',lambda *_:None)
        self.one.accounts.claim(phone,'instance-1')
        before = snapshot(message(30,body='Saved old history'),source='new-1')
        before['account_phone'] = '+381641111112'
        before['chats'][0]['phone'] = phone
        self.one.accounts.source(before)
        self.one.watcher.inbox.ingest(before)
        with closing(connect(self.path)) as db:
            old_message = dict(db.execute("SELECT * FROM viber_messages WHERE source_id='new-1'").fetchone())
        self.one.close()
        with closing(connect(self.path)) as db,db:
            db.execute("UPDATE accounts SET source_id='fresh-epoch' WHERE id='instance-1'")
            db.execute("INSERT INTO account_sources VALUES('fresh-epoch','instance-1')")
        self.one = self.service('instance-1','+381641111112','fresh-epoch')
        baseline = snapshot(message(1,body='New native history'),source='fresh-epoch')
        baseline['account_phone'] = '+381641111112'
        baseline['chats'][0]['phone'] = phone
        self.one.accounts.source(baseline)
        self.assertEqual(self.one.watcher.inbox.ingest(baseline)['new_incoming'],0)
        self.one.replies.generator = Generator()
        self.one.replies.tick()
        self.assertEqual(self.one.replies.generator.contexts,[])
        self.assertTrue(self.one.replies.settings()['enabled'])
        with closing(connect(self.path)) as db:
            self.assertEqual(dict(db.execute("SELECT * FROM viber_messages WHERE source_id='new-1'").fetchone()),old_message)
            self.assertEqual(db.execute("SELECT active FROM viber_conversations WHERE source_id='new-1'").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT detection FROM viber_messages WHERE source_id='fresh-epoch'").fetchone()[0],'BASELINE')
        self.assertEqual(self.one.watcher.inbox.checkpoints(),{'new-1':30,'fresh-epoch':1})
