from contextlib import closing
from dataclasses import asdict
import json
import unittest
import uuid

from fastapi.testclient import TestClient
from app.contact_folders import remove_contacts
from app.controller import create_app
from app.storage import connect
from app.accounts import stamp
import test_controller


class ContactFolderTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_controller.DraftTests()
        self.fixture.setUp()
        self.service = self.fixture.service

    def tearDown(self):
        self.fixture.tearDown()

    def import_contact(self, folder=None):
        return self.service.csv_import.import_leads('phone,company_name,opis_klijenta,outreach_message\n0640000000,Salon,Client note,Hello\n', folder)

    def remove(self, ids=None):
        with self.service.replies.dispatch_lock, self.service.lock, closing(connect(self.fixture.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            rows = list(db.execute('SELECT * FROM leads'))
            if ids is not None:
                rows = [r for r in rows if r['id'] in ids]
            return remove_contacts(db, rows, self.service.account_id)

    def test_import_and_move_preserve_identity_context_and_folder_counts(self):
        first = self.service.contact_folders.create('Production test 1')
        second = self.service.contact_folders.create('Follow ups')
        imported = self.import_contact(first['id'])
        lead_id = imported['lead_ids'][0]
        before = asdict(self.service.lead(lead_id))
        self.assertEqual(next(c for c in self.service.contacts() if c['id'] == lead_id)['folder_id'], first['id'])
        counts = {f['name']: f['contact_count'] for f in self.service.contact_folders.list()}
        self.assertEqual(counts, {'Main': 1, 'Production test 1': 1, 'Follow ups': 0})
        self.service.contact_folders.move(lead_id, second['id'])
        self.assertEqual(asdict(self.service.lead(lead_id)), before)
        self.assertEqual(self.import_contact(first['id'])['duplicates_skipped'], 1)
        contact = next(c for c in self.service.contacts() if c['id'] == lead_id)
        self.assertEqual(contact['folder_id'], second['id'])
        self.assertEqual(contact['client_description'], 'Client note')

    def test_folders_are_private_to_account_and_main_is_protected(self):
        with self.assertRaisesRegex(ValueError, 'Main'):
            self.service.contact_folders.delete(self.service.contact_folders.main_id)
        folder = self.service.contact_folders.create('Businesses')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.service.contact_folders.create('businesses')
        self.import_contact(folder['id'])
        with self.assertRaisesRegex(ValueError, 'Move'):
            self.service.contact_folders.delete(folder['id'])
        with closing(connect(self.fixture.path)) as db, db:
            db.execute('INSERT INTO contact_folders VALUES(?,?,?,?,0,?)', ('foreign', 'another-account', 'Other', 'other', stamp()))
        with self.assertRaises(ValueError):
            self.import_contact('foreign')
        with self.assertRaises(ValueError):
            self.service.contact_folders.move(1, 'foreign')
        with self.assertRaises(ValueError):
            self.service.contact_folders.delete('foreign')

    def test_folder_campaign_cannot_include_contacts_outside_its_folder(self):
        folder = self.service.contact_folders.create('Campaign contacts')
        imported = self.import_contact(folder['id'])
        payload = {'request_key': str(uuid.uuid4()), 'name': 'Scoped campaign', 'folder_id': folder['id'],
                   'lead_ids': imported['lead_ids'], 'text': '', 'schedule': {'mode': 'delay', 'minutes': 60},
                   'interval_minutes': 0, 'daily_cap': 1000, 'window_start': '09:00', 'window_end': '17:00'}
        self.assertEqual(self.service.campaigns.create(payload)['total'], 1)
        with self.assertRaisesRegex(ValueError, 'selected contact folder'):
            self.service.campaigns.create({**payload, 'request_key': str(uuid.uuid4()), 'lead_ids': [1]})

    def test_removal_stays_removed_after_native_poll_until_explicit_reimport(self):
        before_settings = self.service.replies.settings()
        self.fixture.incoming()
        with closing(connect(self.fixture.path)) as db:
            messages_before = db.execute('SELECT COUNT(*) FROM viber_messages').fetchone()[0]
        self.assertEqual(self.remove()['deleted'], 1)
        self.assertEqual(self.service.contacts(), [])
        self.service.watcher.poll_once()
        self.assertEqual(self.service.contacts(), [])
        self.assertEqual(self.service.replies.settings(), before_settings)
        with closing(connect(self.fixture.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM viber_messages').fetchone()[0], messages_before)
        with self.assertRaises(PermissionError):
            self.service.accounts.assert_phone('+381641234567')
        imported = self.service.csv_import.import_leads('phone,company_name\n0641234567,Fresh Business\n')
        new_id = imported['lead_ids'][0]
        self.assertNotEqual(new_id, 1)
        self.service.watcher.poll_once()
        self.fixture.replies.tick()
        self.assertEqual(self.fixture.generator.contexts, [])
        self.assertEqual(len(self.service.contacts()), 1)

    def test_removal_cancels_pending_work_but_retains_completed_and_uncertain_markers(self):
        self.service.pool.shutdown(wait=True)  # Model maintenance with the old desktop worker stopped.
        lead = self.service.lead(1)
        with closing(connect(self.fixture.path)) as db, db:
            for key, state, attempted in [('done','DISPATCHED',stamp()),('unknown','UNKNOWN',stamp()),('pending','QUEUED',None)]:
                db.execute('INSERT INTO web_sends(id,request_key,preview_hash,operation_id,lead_id,phone,company_name,viber_name,text,state,created_at,updated_at,attempted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (key,key,'hash',key,lead.id,lead.phone,lead.company_name,'Person','Hello',state,stamp(),stamp(),attempted))
            db.execute("INSERT INTO web_operations(id,kind,state,created_at,account_id) VALUES('pending','send','QUEUED',?,?)", (stamp(), self.service.account_id))
        self.service.pool.queue.enqueue(self.service.account_id, 'pending', 'send', phone=lead.phone, payload={'send_id':'pending'})
        report = self.remove()
        self.assertEqual(report['cancelled_operation_ids'], ['pending'])
        with closing(connect(self.fixture.path)) as db:
            states = {r['id']:r['state'] for r in db.execute('SELECT id,state FROM web_sends')}
            self.assertEqual(states, {'done':'DISPATCHED','unknown':'UNKNOWN','pending':'CANCELLED'})
            self.assertIsNotNone(db.execute("SELECT attempted_at FROM web_sends WHERE id='unknown'").fetchone()[0])
            self.assertEqual(db.execute("SELECT state FROM controller_jobs WHERE operation_id='pending'").fetchone()[0], 'INTERRUPTED')
        self.assertIsNone(self.service.pool.queue.claim(self.service.account_id, self.service.accounts.worker_id, self.service.accounts.epoch))
        self.assertEqual(self.fixture.desktop.sent, [])

    def test_folder_browser_endpoints_require_session(self):
        client = TestClient(create_app(self.service), base_url='http://127.0.0.1:4001')
        headers = {'Origin':'http://127.0.0.1:4001','X-Viber-Browser':'1'}
        self.assertEqual(client.post('/viber/api/contact-folders', json={'name':'Test'}, headers=headers).status_code, 403)
        headers['X-Viber-CSRF'] = client.post('/viber/api/session', json={}, headers=headers).json()['csrf']
        folder = client.post('/viber/api/contact-folders', json={'name':'Test'}, headers=headers)
        self.assertEqual(folder.status_code, 201)
        self.assertEqual(client.get('/viber/api/contact-folders', headers=headers).status_code, 200)
        response = client.post('/viber/api/contacts/import', json={'csv':'phone,company_name\n0640000000,Salon\n','folder_id':folder.json()['id']}, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
