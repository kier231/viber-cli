"""Install automatic Codex delivery after a graceful controller shutdown."""
from contextlib import closing
from datetime import datetime
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time

import httpx
import psutil

source = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(source))
from app.runtime import settings
from app.storage import connect
from app.viber_inbox import utc_now

root = Path('C:/viber-cli')
origin = 'http://127.0.0.1:4001'
files = ['app/auto_replies.py', 'app/web_server.py', 'app/instances.py', 'app/web_service.py',
         'app/vm_bridge.py', 'app/controller.py',
         'web/viber.html', 'web/viber.js', 'prompts/sajtolog-replies.txt',
         'TWO-INSTANCES.md', 'LOCAL-CONTROLLER.md', 'AGENTS.md',
         'scripts/update_automatic_delivery.py', 'scripts/update_dashboard_contacts.py']
tables = ('auto_reply_settings', 'auto_reply_account_settings')
old_sentence = 'Dashboard approval remains required.'
new_sentence = 'Verified replies send automatically.'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('mode', choices=['deploy', 'verify'])
parser.add_argument('--backup', type=Path)
args = parser.parse_args()
target = settings(root)['VIBER_DATABASE_URL']


def counts():
    with closing(connect(target)) as db:
        return {table: db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                for table in ('leads', 'web_sends')}


def send_records():
    with closing(connect(target)) as db:
        return [dict(row) for row in db.execute('SELECT id,state,updated_at FROM web_sends ORDER BY id')]


def settings_rows():
    with closing(connect(target)) as db:
        return {table: [dict(row) for row in db.execute('SELECT * FROM ' + table)] for table in tables}


with httpx.Client(base_url=origin, timeout=20, headers={'Origin': origin, 'X-Viber-Browser': '1'}) as client:
    response = client.post('/viber/api/session', json={})
    response.raise_for_status()
    client.headers['X-Viber-CSRF'] = response.json()['csrf']

    def api(path, payload=None):
        response = client.get('/viber/api/' + path) if payload is None else client.post('/viber/api/' + path, json=payload)
        response.raise_for_status()
        return response.json()

    inventory = api('instances')
    ready = [slot['id'] for slot in inventory['instances'] if slot['state'] == 'READY']
    if args.mode == 'deploy':
        if not ready:
            raise ValueError('A verified account is needed for graceful shutdown.')
        previous = settings_rows()
        for rows in previous.values():
            for row in rows:
                if len(row['instructions'].replace(old_sentence, new_sentence).strip()) > 8000:
                    raise ValueError('Updated instructions exceed the dashboard limit.')
        for name in files:
            if not (source / name).is_file():
                raise ValueError('Missing deployment file: ' + name)
        backup = root / 'backups' / ('automatic-delivery-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
        backup.mkdir(parents=True)
        state = {'counts': counts(), 'send_records': send_records(), 'accounts': {},
                 'settings_rows': previous, 'files': files}
        with closing(connect(target)) as db:
            workers = [dict(r) for r in db.execute('SELECT process_id,process_started FROM account_workers WHERE stopped_at IS NULL')]
        if not workers or len({(w['process_id'], w['process_started']) for w in workers}) != 1:
            raise ValueError('Expected one verified local controller process.')
        process = psutil.Process(workers[0]['process_id'])
        if str(process.create_time()) != workers[0]['process_started'] or process.name().lower() not in ('python.exe', 'pythonw.exe'):
            raise ValueError('Controller process identity changed; stopping is blocked.')
        for name in files:
            (backup / name).parent.mkdir(parents=True, exist_ok=True)
            if (root / name).is_file():
                shutil.copy2(root / name, backup / name)
        for account in ready:
            client.headers['X-Viber-Account'] = account
            state['accounts'][account] = {'settings': api('replies')['settings']}
        (backup / 'state.json').write_text(json.dumps(state, indent=2), encoding='utf-8')
        # Preserve persisted enablement. Graceful shutdown stops runtime work.
        deadline = time.monotonic() + 60
        while True:
            with closing(connect(target)) as db:
                active = any(db.execute('SELECT 1 FROM ' + table + ' WHERE state IN (' + states + ') LIMIT 1').fetchone()
                    for table, states in [('web_operations', "'QUEUED','RUNNING'"),
                                          ('auto_reply_jobs', "'GENERATING','QUEUED','SUBMITTING'"),
                                          ('controller_jobs', "'RUNNING'"), ('web_sends', "'QUEUED','SUBMITTING'")])
            if not active:
                break
            if time.monotonic() >= deadline:
                raise ValueError('Work did not drain. The old controller is still running; settings are backed up.')
            time.sleep(.25)
        client.headers['X-Viber-Account'] = ready[0]
        api('shutdown', {'confirmed': True})
        process.wait(timeout=60)
        if send_records() != state['send_records']:
            raise ValueError('Send history changed; inspect before continuing.')
        for name in files:
            temporary = (root / name).with_suffix((root / name).suffix + '.automatic-update')
            shutil.copy2(source / name, temporary)
            temporary.replace(root / name)
        # Change only delivery mode and the old approval sentence. Owner rules,
        # enablement flags and incoming-message cutoffs are preserved.
        with closing(connect(target)) as db, db:
            for table in tables:
                if 'require_approval' not in {r[1] for r in db.execute('PRAGMA table_info(' + table + ')')}:
                    db.execute('ALTER TABLE ' + table + ' ADD COLUMN require_approval INTEGER NOT NULL DEFAULT 1')
                for row in db.execute('SELECT * FROM ' + table):
                    instructions = row['instructions'].replace(old_sentence, new_sentence)
                    if row['require_approval'] or instructions != row['instructions']:
                        db.execute('UPDATE ' + table + ' SET require_approval=0,instructions=?,revision=revision+1 WHERE id=?',
                                   (instructions, row['id']))
            db.execute("UPDATE auto_reply_jobs SET state='STALE',reason='Delivery mode changed. Generate a fresh reply.',updated_at=? WHERE state='DRAFT'", (utc_now(),))
        print(json.dumps({'controller_stopped': True, 'installed': True, 'backup': str(backup), 'messages_sent': 0}))
    else:
        if not args.backup:
            raise ValueError('Pass the recorded deployment backup directory.')
        state = json.loads((args.backup / 'state.json').read_text(encoding='utf-8'))
        if counts() != state['counts'] or send_records() != state['send_records']:
            raise ValueError('Ledger or send history changed; inspect before finishing verification.')
        for name in files:
            if hashlib.sha256((source / name).read_bytes()).digest() != hashlib.sha256((root / name).read_bytes()).digest():
                raise ValueError('Installed file differs: ' + name)
        current = settings_rows()
        for table, rows in state['settings_rows'].items():
            for old in rows:
                row = next(r for r in current[table] if r['id'] == old['id'])
                assert row['require_approval'] == 0
                assert row['instructions'] == old['instructions'].replace(old_sentence, new_sentence)
                assert (row['since_rowid'], row['since_ms']) == (old['since_rowid'], old['since_ms'])
        report = {'installed': True, 'counts_preserved': True, 'messages_sent': 0, 'accounts': []}
        for account, saved in state['accounts'].items():
            if account not in ready:
                raise ValueError('A previously connected account has not returned yet.')
            client.headers['X-Viber-Account'] = account
            reply = api('replies')['settings']
            assert reply['review_required'] is False and reply['delivery_mode'] == 'automatic'
            assert reply['generation_slots'] == 2
            assert reply['enabled'] == saved['settings']['enabled']
            report['accounts'].append({'id': account, 'enabled': reply['enabled'], 'delivery_mode': reply['delivery_mode']})
        page = client.get('/viber').text
        assert 'id="reply-delivery"' in page and 'Each draft requires your approval' not in page
        assert inventory['old_account'] == 'DISABLED'
        (args.backup / 'validation.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report))
