from contextlib import closing
import copy
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from app.models import LeadStore
from app.viber_database import DatabaseReadError, international_phone, read_snapshot, validate_schema
from app.viber_inbox import InboxStore
from app.viber_memory import keys_in_chunk
from app.viber_watcher import DatabaseWatcher


def message(event_id, direction='INCOMING', body='Same text', **overrides):
    return {'event_id': event_id, 'chat_id': 10, 'sender_id': 2 if direction == 'INCOMING' else 1,
            'direction': direction, 'timestamp_ms': int(time.time() * 1000), 'token': str(1000 + event_id),
            'sort_order': event_id, 'message_type': 1, 'body': body, 'client_flag': 0,
            'sender_verified': True, **overrides}


def snapshot(*messages, source='source-a', maximum=None):
    return {'source_id': source, 'account_phone': '+381641111111',
            'max_event_id': maximum if maximum is not None else max((m['event_id'] for m in messages), default=0),
            'chats': [{'chat_id': 10, 'peer_id': 2, 'phone': '+381641234567', 'viber_name': 'Person'}],
            'messages': list(messages)}


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'ledger.db'
        self.leads = LeadStore(self.path)
        self.leads.create_with_android('+381641234567', 'Business', lambda *_: None)
        self.inbox = InboxStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def records(self):
        with closing(self.inbox.connect()) as db:
            return {r['event_id']: dict(r) for r in db.execute('SELECT * FROM viber_messages')}

    def baseline(self):
        self.first = message(1, timestamp_ms=int(time.time() * 1000) - 10000)
        self.inbox.ingest(snapshot(self.first))

    def test_history_is_baselined_and_identical_incoming_is_distinct_from_outgoing(self):
        self.baseline()
        outgoing = message(2, 'OUTGOING')
        incoming = message(3)
        repeated = message(4)
        result = self.inbox.ingest(snapshot(self.first, outgoing, incoming, repeated))
        self.assertEqual(result['new_incoming'], 2)
        rows = self.records()
        self.assertEqual([rows[i]['detection'] for i in range(1, 5)],
                         ['BASELINE', 'OUTGOING', 'NEW_INCOMING', 'NEW_INCOMING'])
        # IDs persist across process restarts; no text-based deduplication.
        restarted = InboxStore(self.path)
        self.assertEqual(restarted.ingest(snapshot(self.first, outgoing, incoming, repeated))['imported'], 0)
        self.assertEqual(restarted.checkpoints()['source-a'], 4)

    def test_restart_catches_offline_reply_and_manual_outgoing(self):
        self.baseline()
        restarted = InboxStore(self.path)
        self.assertEqual(restarted.ingest(snapshot(self.first, message(2), message(3, 'OUTGOING')))['new_incoming'], 1)

    def test_edits_deletions_and_manual_send_invalidate_context_revision(self):
        self.baseline()
        original = message(2)
        self.inbox.ingest(snapshot(self.first, original))
        revision = self.inbox.conversation('source-a', 10)['conversation']['revision']
        edited = {**original, 'body': 'Edited'}
        self.assertEqual(self.inbox.ingest(snapshot(self.first, edited))['new_incoming'], 0)
        self.assertEqual(self.records()[2]['detection'], 'EDITED')
        self.assertGreater(self.inbox.conversation('source-a', 10)['conversation']['revision'], revision)
        self.inbox.ingest(snapshot(edited, maximum=2))
        self.assertEqual(self.records()[1]['deleted'], 1)
        self.assertEqual(len(self.inbox.conversation('source-a', 10)['messages']), 1)

    def test_wrong_sender_unknown_direction_reaction_and_media_do_not_trigger(self):
        self.baseline()
        candidates = [message(2, sender_verified=False), message(3, 'UNKNOWN'),
                      message(4, message_type=0), message(5, client_flag=256),
                      message(6, message_type=2), message(7, message_type=72),
                      message(8, timestamp_ms=int(time.time() * 1000) + 3600_000)]
        self.assertEqual(self.inbox.ingest(snapshot(self.first, *candidates))['new_incoming'], 0)
        self.assertEqual(self.records()[2]['detection'], 'AMBIGUOUS')
        self.assertEqual(self.records()[6]['detection'], 'REVIEW')

    def test_history_backfill_with_new_id_is_not_a_reply(self):
        self.baseline()
        self.inbox.ingest(snapshot(self.first, message(2, timestamp_ms=self.first['timestamp_ms'])))
        self.assertEqual(self.records()[2]['detection'], 'HISTORICAL')

    def test_incoming_link_preview_is_text_once_and_still_respects_baselines_and_sender(self):
        first = message(1, body='old.example', message_type=9, timestamp_ms=int(time.time()*1000)-10000)
        self.inbox.ingest(snapshot(first))
        incoming = message(2, body='bridgesolver.com', message_type=9)
        invalid = [message(3, message_type=9, sender_verified=False),
                   message(4, message_type=9, client_flag=256),
                   message(5, message_type=9, body=' ')]
        self.assertEqual(self.inbox.ingest(snapshot(first, incoming, *invalid))['new_incoming'], 1)
        self.assertEqual(self.records()[1]['detection'], 'BASELINE')
        self.assertEqual(self.records()[2]['detection'], 'NEW_INCOMING')
        self.assertEqual(self.records()[3]['detection'], 'AMBIGUOUS')
        self.assertEqual(self.records()[4]['detection'], 'IGNORED')
        self.assertEqual(self.records()[5]['detection'], 'REVIEW')
        self.assertEqual(self.inbox.ingest(snapshot(first, incoming, *invalid))['new_incoming'], 0)
        edited = {**incoming, 'body': 'other.example'}
        self.assertEqual(self.inbox.ingest(snapshot(first, edited, *invalid))['new_incoming'], 0)
        self.assertEqual(self.records()[2]['detection'], 'EDITED')

    def test_monitoring_disabled_and_reenabled_never_replays_old_candidates(self):
        self.baseline()
        second = message(2)
        self.inbox.monitor('source-a', 10, False)
        self.inbox.ingest(snapshot(self.first, second))
        self.assertEqual(self.records()[2]['detection'], 'IGNORED')
        self.inbox.monitor('source-a', 10, True)
        self.assertEqual(self.inbox.ingest(snapshot(self.first, second, message(3)))['new_incoming'], 1)

    def test_reused_identity_or_reset_rolls_back_and_keeps_checkpoint(self):
        self.baseline()
        with self.assertRaises(DatabaseReadError):
            self.inbox.ingest(snapshot({**self.first, 'token': 'reused'}, message(2)))
        self.assertEqual(self.inbox.checkpoints()['source-a'], 1)
        self.assertEqual(len(self.records()), 1)
        with self.assertRaises(DatabaseReadError):
            self.inbox.ingest(snapshot(maximum=0))
        self.assertEqual(self.inbox.status()['conversations'], 1)

    def test_pending_outgoing_token_becomes_final_without_blocking_incoming_reply(self):
        pending = message(1,'OUTGOING',token='0',sort_order=0)
        self.inbox.ingest(snapshot(pending))
        confirmed = {**pending,'token':'987654321','sort_order':987654321,
                     'timestamp_ms':pending['timestamp_ms']+1500}
        incoming = message(2,body='jesmo',timestamp_ms=confirmed['timestamp_ms']+1000)
        self.assertEqual(self.inbox.ingest(snapshot(confirmed,incoming))['new_incoming'],1)
        self.assertEqual(self.records()[1]['token'],'987654321')
        self.assertEqual(self.records()[2]['detection'],'NEW_INCOMING')
        self.assertEqual(self.inbox.ingest(snapshot(confirmed,incoming))['updated'],0)

    def test_token_assignment_exception_does_not_accept_reused_outbound_identity(self):
        pending = message(1,'OUTGOING',token='0',sort_order=0)
        self.inbox.ingest(snapshot(pending))
        for change in ({'body':'Another message'}, {'chat_id':11}, {'sender_id':3},
                       {'direction':'INCOMING'}, {'message_type':2},
                       {'sender_verified':False}, {'timestamp_ms':pending['timestamp_ms']+300_001}):
            with self.subTest(change=change), self.assertRaises(DatabaseReadError):
                self.inbox.ingest(snapshot({**pending,'token':'987654321',**change}))
        confirmed = {**pending,'token':'987654321','sort_order':987654321}
        self.inbox.ingest(snapshot(confirmed))
        with self.assertRaises(DatabaseReadError):
            self.inbox.ingest(snapshot({**confirmed,'token':'another-final-token'}))
        self.assertEqual(self.records()[1]['token'],'987654321')

    def test_pending_outgoing_can_inherit_previous_nonzero_sort_order(self):
        pending = message(1,'OUTGOING',token='0',sort_order=6289758325868380042)
        self.inbox.ingest(snapshot(pending))
        confirmed = {**pending,'token':'6289764640162607856','sort_order':6289764640162607856,
                     'timestamp_ms':pending['timestamp_ms']+1687}
        incoming = message(2,body='zar vec neznas cime se bavim ?',
                           timestamp_ms=confirmed['timestamp_ms']+1000)
        self.assertEqual(self.inbox.ingest(snapshot(confirmed,incoming))['new_incoming'],1)
        self.assertEqual(self.records()[1]['token'],'6289764640162607856')
        self.assertEqual(self.records()[2]['detection'],'NEW_INCOMING')

    def test_pending_link_message_ack_does_not_pause_incoming_detection(self):
        pending = message(1,'OUTGOING',body='Primer: https://example.com/',
                          token='0',sort_order=6289789730128182515,message_type=9)
        self.inbox.ingest(snapshot(pending))
        confirmed = {**pending,'token':'6289789887024485707',
                     'sort_order':6289789887024485707,'timestamp_ms':pending['timestamp_ms']+506}
        incoming = message(2,body='Koliko bi koštao takav sajt?',timestamp_ms=confirmed['timestamp_ms']+1000)
        for change in ({'body':'A different link: https://other.example/'},
                       {'sender_id':3}, {'message_type':1}):
            with self.subTest(change=change), self.assertRaises(DatabaseReadError):
                self.inbox.ingest(snapshot({**confirmed,**change},incoming))
        self.assertEqual(self.inbox.ingest(snapshot(confirmed,incoming))['new_incoming'],1)
        self.assertEqual(self.records()[1]['token'],confirmed['token'])
        self.assertEqual(self.records()[2]['detection'],'NEW_INCOMING')
        self.assertEqual(self.inbox.ingest(snapshot(confirmed,incoming))['updated'],0)
        with self.assertRaises(DatabaseReadError):
            self.inbox.ingest(snapshot({**confirmed,'token':'different-final-token'},incoming))

    def test_new_profile_gets_its_own_baseline_and_names_are_not_changed(self):
        self.baseline()
        self.inbox.ingest(snapshot(message(1), source='source-b'))
        self.assertEqual(self.inbox.status()['new_incoming'], 0)
        self.assertEqual(self.inbox.status()['conversations'], 1)
        self.assertEqual(self.inbox.conversations()[0]['company_name'], 'Business')
        lead = self.leads.all()[0]
        self.assertEqual(lead.company_name, 'Business')
        self.assertEqual(lead.contact_name, 'Business | SJT-1')

    def test_new_conversation_is_baselined_even_after_global_checkpoint(self):
        self.baseline()
        data = snapshot(self.first, message(2, chat_id=20))
        data['chats'].append({**data['chats'][0], 'chat_id': 20})
        self.inbox.ingest(data)
        self.assertEqual(self.records()[2]['detection'], 'BASELINE')

    def test_fast_reply_after_app_outreach_survives_first_conversation_baseline(self):
        now = int(time.time() * 1000)
        attempted = datetime.fromtimestamp((now - 10_000) / 1000, timezone.utc).isoformat()
        with closing(self.inbox.connect()) as db, db:
            db.execute('''CREATE TABLE web_sends (
                id TEXT, request_key TEXT, phone TEXT, state TEXT,
                attempted_at TEXT, updated_at TEXT, created_at TEXT)''')
            db.execute("INSERT INTO web_sends VALUES('send-1','manual-1',?,'DISPATCHED',?,?,?)",
                       ('+381641234567', attempted, attempted, attempted))
        outgoing = message(1, 'OUTGOING', timestamp_ms=now - 8_000)
        incoming = message(2, timestamp_ms=now - 5_000, body='Quick reply')

        result = self.inbox.ingest(snapshot(outgoing, incoming))

        self.assertEqual(result['new_incoming'], 1)
        self.assertEqual(self.records()[1]['detection'], 'BASELINE')
        self.assertEqual(self.records()[2]['detection'], 'NEW_INCOMING')

    def test_old_outreach_does_not_reclassify_first_conversation_history(self):
        now = int(time.time() * 1000)
        attempted = datetime.fromtimestamp((now - 600_000) / 1000, timezone.utc).isoformat()
        with closing(self.inbox.connect()) as db, db:
            db.execute('''CREATE TABLE web_sends (
                id TEXT, request_key TEXT, phone TEXT, state TEXT,
                attempted_at TEXT, updated_at TEXT, created_at TEXT)''')
            db.execute("INSERT INTO web_sends VALUES('send-1','manual-1',?,'DISPATCHED',?,?,?)",
                       ('+381641234567', attempted, attempted, attempted))

        result = self.inbox.ingest(snapshot(message(1, timestamp_ms=now - 5_000)))

        self.assertEqual(result['new_incoming'], 0)
        self.assertEqual(self.records()[1]['detection'], 'BASELINE')

    def test_watcher_recovers_from_source_failure_without_advancing_checkpoint(self):
        class Source:
            calls = 0
            def read(self, request):
                if request.get('force_refresh') is not True:
                    raise AssertionError('A watcher freshness check must bypass the reader cache.')
                Source.calls += 1
                if Source.calls == 2:
                    raise DatabaseReadError('Unavailable')
                return snapshot(message(1, timestamp_ms=1)) if Source.calls == 1 else snapshot(message(1, timestamp_ms=1), message(2))
            def close(self):
                pass
        watcher = DatabaseWatcher(self.leads, Source)
        try:
            watcher.poll_once()
            self.assertIsNone(watcher.poll_once())
            self.assertEqual(watcher.status()['state'], 'BLOCKED')
            self.assertEqual(watcher.inbox.checkpoints()['source-a'], 1)
            self.assertEqual(watcher.poll_once()['new_incoming'], 1)
            self.assertFalse(watcher.status()['automatic_replies'])
        finally:
            watcher.close()

    def test_identical_timestamps_do_not_hide_messages(self):
        self.baseline()
        messages = [self.first] + [message(i) for i in range(2, 105)]
        self.inbox.ingest(snapshot(*messages))
        newest = self.inbox.conversation('source-a', 10)
        # New arrivals between pages cannot shift the older-history cursor.
        self.inbox.ingest(snapshot(*messages, message(105)))
        older = self.inbox.conversation('source-a', 10, before=newest['older_before'])
        self.assertEqual(len(newest['messages']) + len(older['messages']), 104)
        self.assertEqual(len({m['event_id'] for m in newest['messages'] + older['messages']}), 104)


class SourceQueryTests(unittest.TestCase):
    def test_reader_cache_expires_when_qt_data_version_does_not_change(self):
        from app.viber_source_worker import QtViberSource
        reader = QtViberSource.__new__(QtViberSource)
        reader.path,reader.identity,reader.last_version = Path('unused'),'native',None
        reader.viber_process = Mock()
        reader.viber_process.is_running.return_value = True
        reader.query = lambda sql: [{'data_version':7}] if sql=='PRAGMA data_version' else []
        request = {'phones':[],'checkpoints':{}}
        with patch('app.viber_source_worker.source_identity',return_value='native'), \
             patch.object(reader,'_storage_version',return_value=('unchanged-files',)), \
             patch('app.viber_source_worker.time.monotonic',side_effect=[10,10.1,11.01]), \
             patch('app.viber_source_worker.read_snapshot',side_effect=[{'max_event_id':3},{'max_event_id':4}]) as scan:
            self.assertEqual(reader.read(request)['max_event_id'],3)
            self.assertTrue(reader.read(request)['unchanged'])
            self.assertEqual(reader.read(request)['max_event_id'],4)
            self.assertEqual(scan.call_count,2)

    def test_reader_immediately_scans_changed_wal_despite_unchanged_data_version(self):
        from app.viber_source_worker import QtViberSource
        reader = QtViberSource.__new__(QtViberSource)
        reader.path,reader.identity,reader.last_version = Path('unused'),'native',None
        reader.viber_process = Mock()
        reader.viber_process.is_running.return_value = True
        reader.query = lambda sql: [{'data_version':7}] if sql=='PRAGMA data_version' else []
        with patch('app.viber_source_worker.source_identity',return_value='native'), \
             patch.object(reader,'_storage_version',side_effect=[('old-wal',),('new-wal',)]), \
             patch('app.viber_source_worker.time.monotonic',side_effect=[10,10.1]), \
             patch('app.viber_source_worker.read_snapshot',side_effect=[{'max_event_id':3},{'max_event_id':4}]):
            self.assertEqual(reader.read({'phones':[]})['max_event_id'],3)
            self.assertEqual(reader.read({'phones':[]})['max_event_id'],4)

    def test_account_activation_requests_complete_identity_even_with_fresh_cache(self):
        from app.viber_source_worker import QtViberSource
        reader = QtViberSource.__new__(QtViberSource)
        reader.path,reader.identity,reader.last_version = Path('unused'),'native',None
        reader.viber_process = Mock()
        reader.viber_process.is_running.return_value = True
        reader.query = lambda sql: [{'data_version':7}] if sql=='PRAGMA data_version' else []
        with patch('app.viber_source_worker.source_identity',return_value='native'), \
             patch.object(reader,'_storage_version',return_value=('same-files',)), \
             patch('app.viber_source_worker.time.monotonic',side_effect=[10,10.1]), \
             patch('app.viber_source_worker.read_snapshot',return_value={'account_phone':'+381641111111','max_event_id':4}) as scan:
            self.assertEqual(reader.read({'phones':[]})['max_event_id'],4)
            self.assertEqual(reader.read({'phones':[],'force_refresh':True})['account_phone'],'+381641111111')
            self.assertEqual(scan.call_count,2)

    def test_direct_phone_membership_and_group_exclusion(self):
        with closing(sqlite3.connect(':memory:')) as db:
            db.row_factory = sqlite3.Row
            db.executescript('''
                CREATE TABLE Contact(ContactID,Name,ClientName,Number);
                CREATE TABLE ChatInfo(ChatID,Name,Flags,PGType,PGUri);
                CREATE TABLE ChatRelation(ChatID,ContactID);
                CREATE TABLE Events(EventID,TimeStamp,Direction,ChatID,ContactID,Token,SortOrder);
                CREATE TABLE Messages(EventID,Type,Body,ClientFlag);
                INSERT INTO Contact VALUES(1,'Self','Self','381641111111'),(2,'Business','Person','381641234567');
                INSERT INTO ChatInfo VALUES(10,NULL,0,255,NULL),(20,'Group',4,255,NULL),(30,NULL,4,255,NULL);
                INSERT INTO ChatRelation VALUES(10,1),(10,2),(20,1),(20,2),(30,1),(30,2);
                INSERT INTO Events VALUES(1,1000,0,10,2,123,1),(2,2000,1,10,1,124,2),(3,3000,0,20,2,125,3);
                INSERT INTO Messages VALUES(1,1,'Same',0),(2,1,'Same',0),(3,1,'Group',0);
            ''')
            query = lambda sql, parameters=(): [dict(row) for row in db.execute(sql, parameters)]
            validate_schema(query)
            result = read_snapshot(query, source_id='test', own_phone='381641111111', phones=['+381641234567'])
            self.assertEqual([c['chat_id'] for c in result['chats']], [10])
            self.assertEqual([m['direction'] for m in result['messages']], ['INCOMING', 'OUTGOING'])
            self.assertTrue(all(m['sender_verified'] for m in result['messages']))
            # A first incoming link must discover a verified direct sender too.
            db.execute("UPDATE Messages SET Type=9,Body='bridgesolver.com' WHERE EventID=1")
            discovered = read_snapshot(query, source_id='test', own_phone='381641111111', phones=[],
                                       include_new_senders=True, incoming_since_ms=900)
            self.assertEqual([c['chat_id'] for c in discovered['chats']], [10])
            self.assertEqual(discovered['messages'][0]['message_type'], 9)
            for change in ("UPDATE Messages SET ClientFlag=256 WHERE EventID=1",
                           "UPDATE Messages SET ClientFlag=0,Body='' WHERE EventID=1",
                           "UPDATE Messages SET Type=2,Body='Photo' WHERE EventID=1"):
                db.execute(change)
                blocked = read_snapshot(query, source_id='test', own_phone='381641111111', phones=[],
                                        include_new_senders=True, incoming_since_ms=900)
                self.assertEqual(blocked['chats'], [])
            db.execute('ALTER TABLE Messages RENAME COLUMN ClientFlag TO Changed')
            with self.assertRaises(DatabaseReadError):
                validate_schema(query)

    def test_key_extraction_is_limited_to_valid_sql_literals(self):
        key = 'ab' * 32
        sql = "PRAGMA hexkey='" + key + "'"
        self.assertEqual(keys_in_chunk(sql.encode() + sql.encode('utf-16-le')), {key})
        self.assertEqual(keys_in_chunk(key.encode() + b"PRAGMA hexkey='abc'"), set())
        self.assertIsNone(international_phone('++381641234567'))


if __name__ == '__main__':
    unittest.main()
