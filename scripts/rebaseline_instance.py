"""Owner-confirmed native history reset, preserving archived history and sends.

Run deploy once with --confirmed-reset, then restart the existing launcher and
run verify with the returned backup path. Never use this to retry a send.
"""
from contextlib import closing
from datetime import datetime, timezone
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time
import uuid

import httpx
import psutil

source = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(source))
from app.runtime import settings
from app.storage import connect
from app.vm_bridge import VmBridge, load_vm_config

root = Path('C:/viber-cli')
manifest_path = root / 'data/instances.json'
origin = 'http://127.0.0.1:4001'
files = ['app/instances.py', 'app/vm_bridge.py', 'app/controller.py',
         'app/web_service.py', 'app/auto_replies.py',
         'scripts/update_automatic_delivery.py', 'scripts/rebaseline_instance.py']
preserved_tables = ('leads', 'web_sends', 'viber_messages', 'viber_sources',
                    'contact_owners', 'auto_reply_settings')
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('mode', choices=('deploy', 'verify'))
parser.add_argument('--account', choices=('instance-1', 'instance-2'), default='instance-1')
parser.add_argument('--confirmed-reset', action='store_true')
parser.add_argument('--backup', type=Path)
args = parser.parse_args()
target = settings(root)['VIBER_DATABASE_URL']


def records(db, table):
    return [dict(row) for row in db.execute('SELECT * FROM ' + table)]


def same_rows(before, after):
    encode = lambda row: json.dumps(row, sort_keys=True, ensure_ascii=True)
    return sorted(map(encode, before)) == sorted(map(encode, after))


def no_active_work():
    with closing(connect(target)) as db:
        for table, states in [('web_operations', "'QUEUED','RUNNING'"),
                              ('auto_reply_jobs', "'GENERATING','QUEUED','SUBMITTING'"),
                              ('controller_jobs', "'PENDING','RUNNING'"),
                              ('web_sends', "'QUEUED','SUBMITTING'")]:
            if db.execute('SELECT 1 FROM '+table+' WHERE state IN ('+states+') LIMIT 1').fetchone():
                raise ValueError('Active or pending work must drain before rebaselining: '+table)


with httpx.Client(base_url=origin, timeout=20,
                  headers={'Origin': origin, 'X-Viber-Browser': '1'}) as client:
    response = client.post('/viber/api/session', json={})
    response.raise_for_status()
    client.headers['X-Viber-CSRF'] = response.json()['csrf']

    def api(path, payload=None):
        response = client.get('/viber/api/'+path) if payload is None else client.post('/viber/api/'+path,json=payload)
        response.raise_for_status()
        return response.json()

    inventory = api('instances')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    slot = next(s for s in manifest['instances'] if s['id'] == args.account)
    if args.mode == 'deploy':
        if not args.confirmed_reset:
            raise ValueError('Explicit owner confirmation of a history reset is required.')
        if any(s['state'] == 'READY' for s in inventory['instances']):
            raise ValueError('Use graceful account maintenance while a verified service is available.')
        no_active_work()
        bridge = VmBridge(load_vm_config(slot['bridge_config']))
        health = bridge.call('/health', timeout=3)
        if health.get('instance_id') != args.account or health.get('status') != 'ready':
            raise ValueError('The selected native VM identity must be healthy.')
        now_ms = int(time.time()*1000)
        snapshot = bridge.call('/source/read', {'phones': [], 'checkpoints': {},
            'include_new_senders': True, 'incoming_since_ms': now_ms}, timeout=15)
        with closing(connect(target)) as db:
            account = dict(db.execute('SELECT * FROM accounts WHERE id=?',(args.account,)).fetchone())
            old_source = dict(db.execute('SELECT * FROM viber_sources WHERE source_id=?',(account['source_id'],)).fetchone())
        if snapshot.get('account_phone') != account['phone'] or snapshot.get('source_id') != slot.get('native_source_id', account['source_id']):
            raise ValueError('The native number or profile does not match the saved account.')
        if snapshot.get('max_event_id', old_source['checkpoint']) >= old_source['checkpoint']:
            raise ValueError('No backwards native checkpoint was verified; reset is blocked.')
        listeners = {c.pid for c in psutil.net_connections(kind='tcp')
                     if c.laddr.port == 4001 and c.status == psutil.CONN_LISTEN}
        if len(listeners) != 1:
            raise ValueError('Expected exactly one controller listener.')
        process = psutil.Process(listeners.pop())
        started, argv = process.create_time(), process.cmdline()
        if process.name().lower() != 'python.exe' or len(argv) != 2 or \
                Path(argv[0]).resolve() != (root/'.venv/Scripts/python.exe').resolve() or \
                Path(argv[1]).resolve() != (root/'web.py').resolve():
            raise ValueError('Controller process identity could not be verified.')
        backup = root/'backups'/('history-reset-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
        backup.mkdir(parents=True)
        shutil.copy2(manifest_path, backup/'instances.json')
        for name in files:
            if not (source/name).is_file():
                raise ValueError('Missing recovery deployment file: '+name)
            (backup/name).parent.mkdir(parents=True,exist_ok=True)
            if (root/name).is_file():
                shutil.copy2(root/name,backup/name)
        # The old controller predates pending-account shutdown support. It has
        # no bound services or queued work. Confirm its exact identity again
        # immediately before stopping this idle process, then await its exit.
        no_active_work()
        if process.create_time() != started or process.cmdline() != argv:
            raise ValueError('Controller process changed; shutdown is blocked.')
        response = client.post('/viber/api/shutdown',json={'confirmed':True})
        if response.status_code == 409:
            process.terminate()
        else:
            response.raise_for_status()
        process.wait(timeout=45)
        no_active_work()
        epoch = hashlib.sha256((snapshot['source_id']+str(uuid.uuid4())).encode()).hexdigest()
        stamp = datetime.now(timezone.utc).isoformat(timespec='microseconds')
        with closing(connect(target)) as db:
            state = {'account': account, 'native_source_id': snapshot['source_id'],
                     'source_id': epoch, 'baseline_ms': now_ms, 'files': files,
                     'controller': {'pid': process.pid, 'started': started},
                     'preserved': {table: records(db,table) for table in preserved_tables},
                     'conversations': records(db,'viber_conversations'),
                     'reply_settings': records(db,'auto_reply_account_settings')}
        (backup/'state.json').write_text(json.dumps(state,indent=2,ensure_ascii=True),encoding='utf-8')
        for name in files:
            temporary = (root/name).with_suffix((root/name).suffix+'.baseline-update')
            shutil.copy2(source/name,temporary)
            temporary.replace(root/name)
        with closing(connect(target)) as db,db:
            current = db.execute('SELECT source_id FROM accounts WHERE id=?',(args.account,)).fetchone()[0]
            if current != account['source_id']:
                raise ValueError('Account identity changed during maintenance.')
            db.execute('UPDATE accounts SET source_id=?,monitoring_since_ms=? WHERE id=?',(epoch,now_ms,args.account))
            db.execute('INSERT INTO account_sources VALUES(?,?)',(epoch,args.account))
            db.execute('UPDATE viber_conversations SET active=0 WHERE source_id IN (SELECT source_id FROM account_sources WHERE account_id=?)',(args.account,))
            cutoff = db.execute('SELECT COALESCE(MAX(rowid),0) FROM viber_messages').fetchone()[0]
            db.execute('UPDATE auto_reply_account_settings SET since_ms=?,since_rowid=?,revision=revision+1 WHERE id=?',(now_ms,cutoff,args.account))
            db.execute('UPDATE account_workers SET stopped_at=? WHERE account_id=?',(stamp,args.account))
            db.execute('INSERT INTO web_events(created_at,kind,detail,account_id) VALUES(?,?,?,?)',
                       (stamp,'HISTORY_BASELINE','Owner confirmed native history reset. Saved history retained; fresh detection baseline created.',args.account))
        slot.update(native_source_id=snapshot['source_id'],source_id=epoch)
        temporary = manifest_path.with_suffix('.baseline-update')
        temporary.write_text(json.dumps(manifest,indent=2),encoding='utf-8')
        temporary.replace(manifest_path)
        print(json.dumps({'installed':True,'controller_stopped':True,'backup':str(backup),
                          'saved_history_preserved':True,'messages_sent':0}))
    else:
        if not args.backup:
            raise ValueError('Pass the recorded recovery backup directory.')
        state = json.loads((args.backup/'state.json').read_text(encoding='utf-8'))
        if not any(s['id'] == args.account and s['state'] == 'READY' for s in inventory['instances']):
            raise ValueError('The rebaselined account has not become ready.')
        for name in state['files']:
            if (source/name).read_bytes() != (root/name).read_bytes():
                raise ValueError('Installed file differs: '+name)
        with closing(connect(target)) as db:
            for table, before in state['preserved'].items():
                after = records(db,table)
                if table in ('viber_sources','viber_messages'):
                    after = [r for r in after if r['source_id'] != state['source_id']]
                if not same_rows(before,after):
                    raise ValueError('Archived records changed: '+table)
            for before in state['reply_settings']:
                after = dict(db.execute('SELECT * FROM auto_reply_account_settings WHERE id=?',(before['id'],)).fetchone())
                for key in ('enabled','instructions','require_approval'):
                    if before[key] != after[key]:
                        raise ValueError('Saved reply preferences changed: '+key)
            if db.execute("SELECT 1 FROM auto_reply_jobs WHERE source_id=?",(state['source_id'],)).fetchone():
                raise ValueError('Unexpected automatic reply after baseline; inspect before continuing.')
            old = db.execute("SELECT enabled,paused FROM accounts WHERE id='current'").fetchone()
            if tuple(old) != (0,1):
                raise ValueError('Archived account state changed.')
        client.headers['X-Viber-Account'] = args.account
        reply = api('replies')['settings']
        inbox = api('database/status')
        if not reply['enabled'] or reply['delivery_mode'] != 'automatic' or reply['error'] or inbox['state'] != 'WATCHING' or inbox['error']:
            raise ValueError('Replies or inbox are not healthy after rebaseline.')
        report = {'installed':True,'saved_history_preserved':True,'send_records_preserved':True,
                  'automatic_replies_enabled':True,'inbox_state':inbox['state'],'messages_sent':0}
        (args.backup/'validation.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(report))
