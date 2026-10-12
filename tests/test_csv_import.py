from contextlib import closing
import csv
import io
import unittest
import uuid
import time
from unittest.mock import Mock

from app.csv_import import parse_csv
from app.storage import connect
from app.accounts import stamp
from app.controller import create_app
from fastapi.testclient import TestClient
import test_controller


class CsvTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_controller.DraftTests()
        self.fixture.setUp()
        self.service = self.fixture.service

    def tearDown(self):
        self.fixture.tearDown()

    def text(self, count=1, delimiter=','):
        output = io.StringIO(newline='')
        writer = csv.writer(output, delimiter=delimiter)
        writer.writerow(['business', 'public_mobile', 'sales_pitch_sr', 'codex_agent_note_sr'])
        for i in range(count):
            writer.writerow([f'Čačak salon {i}', f'064{i:07d}', f'Zdravo, bavim se izradom sajtova. Predlog {i}?', 'Salon, radi subotom.\nPitaj za boje.'])
        return '\ufeff' + output.getvalue()

    def test_1000_imported_leads_can_be_reviewed_and_scheduled_with_individual_pitches(self):
        before = self.service.replies.settings()
        result = self.service.csv_import.import_leads(self.text(1000))
        self.assertEqual(result['imported'], 1000)
        self.assertEqual(self.service.replies.settings(), before)
        contacts = {r['id']: r for r in self.service.contacts()}
        first = contacts[result['lead_ids'][0]]
        self.assertEqual(first['phone'], '+381640000000')
        self.assertIsNone(first['viber_name'])
        self.assertIn('Pitaj za boje.', first['client_description'])
        self.assertEqual(first['client_description_revision'], 1)
        draft = self.service.campaigns.create({
            'request_key': str(uuid.uuid4()), 'name': 'CSV outreach', 'lead_ids': result['lead_ids'],
            'text': '', 'schedule': {'mode': 'delay', 'minutes': 60}, 'interval_minutes': 5,
            'daily_cap': 100, 'window_start': '09:00', 'window_end': '17:00'})
        self.assertEqual(draft['counts'], {'DRAFT': 1000})
        self.assertIn('Predlog 0?', draft['recipients'][0]['text'])
        self.assertIn('Predlog 999?', draft['recipients'][-1]['text'])
        review = self.service.campaigns.review(draft['id'])
        active = self.service.campaigns.activate({'preview_token': review['token'], 'confirmed': True, 'request_key': str(uuid.uuid4())})
        self.assertEqual(active['counts'], {'SCHEDULED': 1000})
        with closing(connect(self.service.store.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM contact_owners WHERE account_id=?', (self.service.account_id,)).fetchone()[0], len(contacts))
            self.assertEqual(db.execute('SELECT count(*) FROM web_sends WHERE attempted_at IS NOT NULL').fetchone()[0], 0)

    def test_semicolon_tab_quoted_fields_and_bom(self):
        for separator in (';', '\t', ','):
            with self.subTest(separator=separator):
                row = parse_csv(self.text(delimiter=separator))[0]
                self.assertEqual(row['company'], 'Čačak salon 0')
                self.assertIn('\n', row['description'])

    def test_invalid_late_row_rolls_back_entire_file(self):
        before = self.service.contacts()
        with self.assertRaisesRegex(ValueError, 'CSV line 4'):
            self.service.csv_import.import_leads(self.text() + 'Bad,not-a-number,Hi,Note\r\n')
        self.assertEqual(self.service.contacts(), before)

    def test_repeat_and_duplicate_import_preserve_existing_context(self):
        source = 'phone,company_name,opis_klijenta\n0640000000,Original,Original note\n+381640000000,Duplicate,New note\n'
        first = self.service.csv_import.import_leads(source)
        second = self.service.csv_import.import_leads(source.replace('Original', 'Changed'))
        self.assertEqual((first['imported'], first['duplicates_skipped']), (1, 1))
        self.assertEqual((second['imported'], second['duplicates_skipped']), (0, 2))
        lead = self.service.lead(first['lead_ids'][0])
        self.assertEqual(lead.company_name, 'Original')
        with closing(connect(self.service.store.path)) as db:
            self.assertEqual(self.service.client_descriptions.get(db, lead.id)['description'], 'Original note')

    def test_other_account_numbers_are_skipped_and_never_reassigned(self):
        with closing(connect(self.service.store.path)) as db, db:
            db.execute('INSERT INTO accounts(id,phone,enabled,created_at) VALUES(?,?,1,?)', ('other', '+381641111112', stamp()))
            db.execute('INSERT INTO contact_owners VALUES(?,?,?)', ('+381640000000', 'other', stamp()))
        result = self.service.csv_import.import_leads(self.text(2))
        self.assertEqual((result['imported'], result['other_account_skipped']), (1, 1))
        with closing(connect(self.service.store.path)) as db:
            self.assertEqual(db.execute('SELECT account_id FROM contact_owners WHERE phone=?', ('+381640000000',)).fetchone()[0], 'other')

    def test_header_and_message_errors_cannot_import(self):
        for text in ('', 'phone,name\n0640000000,Salon\n', 'phone,phone,company_name\n0,0,Salon\n',
                     'phone,company_name,message\n0640000000,Salon,"Hi\nthere"\n',
                     'phone,company_name\n0640000000,"Unclosed'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.service.csv_import.import_leads(text)

    def test_browser_endpoint_requires_session_and_accepts_excel_csv(self):
        client = TestClient(create_app(self.service), base_url='http://127.0.0.1:4001')
        headers = {'Origin': 'http://127.0.0.1:4001', 'X-Viber-Browser': '1'}
        self.assertEqual(client.post('/viber/api/contacts/import', json={'csv': self.text()}, headers=headers).status_code, 403)
        session = client.post('/viber/api/session', json={}, headers=headers)
        headers['X-Viber-CSRF'] = session.json()['csrf']
        response = client.post('/viber/api/contacts/import', json={'csv': self.text(delimiter=';')}, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['imported'], 1)
        self.assertEqual(client.post('/viber/api/contacts/import', json={'csv': self.text()}, headers=headers).json()['duplicates_skipped'], 1)

    def test_csv_larger_than_old_request_limit_imports_through_browser_endpoint(self):
        text = 'phone,company_name,opis_klijenta\n' + ''.join(f'064{i:07d},Salon {i},' + 'a' * 4000 + '\n' for i in range(300))
        self.assertGreater(len(text.encode()), 1048576)
        client = TestClient(create_app(self.service), base_url='http://127.0.0.1:4001')
        headers = {'Origin': 'http://127.0.0.1:4001', 'X-Viber-Browser': '1'}
        headers['X-Viber-CSRF'] = client.post('/viber/api/session', json={}, headers=headers).json()['csrf']
        response = client.post('/viber/api/contacts/import', json={'csv': text}, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['imported'], 300)

    def test_imported_outreach_still_opens_exact_number_and_verifies_before_send(self):
        result = self.service.csv_import.import_leads(self.text())
        self.assertEqual(self.fixture.desktop.sent, [])
        self.fixture.desktop.open_phone = Mock(wraps=self.fixture.desktop.open_phone)
        draft = self.service.campaigns.create({
            'request_key': str(uuid.uuid4()), 'name': 'Synthetic only', 'lead_ids': result['lead_ids'],
            'text': '', 'schedule': {'mode': 'delay', 'minutes': 60}, 'interval_minutes': 5,
            'daily_cap': 100, 'window_start': '00:00', 'window_end': '23:59'})
        review = self.service.campaigns.review(draft['id'])
        self.service.campaigns.activate({'preview_token': review['token'], 'confirmed': True, 'request_key': str(uuid.uuid4())})
        with closing(connect(self.service.store.path)) as db, db:
            db.execute('UPDATE web_sends SET scheduled_at=? WHERE campaign_id=?', (stamp(-1), draft['id']))
        task = self.service._dispatch_due()
        self.assertIsNotNone(task)
        deadline = time.monotonic() + 10
        while self.service.pending and time.monotonic() < deadline:
            time.sleep(.01)
        operation = self.service.operation(task['operation_id'])
        self.assertEqual(operation['state'], 'SUCCEEDED', operation)
        self.assertEqual(operation['result']['state'], 'DISPATCHED')
        self.fixture.desktop.open_phone.assert_called_once_with('+381640000000')
        self.assertEqual(self.fixture.desktop.sent, ['Zdravo, bavim se izradom sajtova. Predlog 0?'])
