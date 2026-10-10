"""Deploy the dashboard contact update after a verified, graceful controller stop."""
from contextlib import closing
from datetime import datetime
import argparse
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

root = Path('C:/viber-cli')
origin = 'http://127.0.0.1:4001'
files = ['app/models.py', 'app/accounts.py', 'app/web_service.py',
         'web/viber.html', 'web/viber.js', 'TWO-INSTANCES.md']
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('mode', choices=['inspect', 'deploy', 'verify'])
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
    if args.mode == 'inspect':
        report = {'instances': inventory['instances'], 'counts': counts(), 'accounts': []}
        for account in ready:
            client.headers['X-Viber-Account'] = account
            report['accounts'].append({'id': account, 'pending': api('status')['pending'],
                                       'drafting': api('replies')['settings']['enabled']})
        print(json.dumps(report))
    elif args.mode == 'deploy':
        if not ready:
            raise ValueError('No verified account is available for graceful shutdown.')
        backup = root / 'backups' / ('dashboard-contacts-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
        backup.mkdir(parents=True)
        state = {'counts': counts(), 'send_records': send_records(), 'accounts': {}, 'files': files}
        with closing(connect(target)) as db:
            workers = [dict(r) for r in db.execute('SELECT process_id,process_started FROM account_workers WHERE stopped_at IS NULL')]
        if not workers or len({(w['process_id'], w['process_started']) for w in workers}) != 1:
            raise ValueError('Expected one verified local controller process.')
        process = psutil.Process(workers[0]['process_id'])
        if str(process.create_time()) != workers[0]['process_started'] or process.name().lower() not in ('python.exe', 'pythonw.exe'):
            raise ValueError('Controller process identity changed; stopping is blocked.')
        for name in files:
            (backup / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / name, backup / name)
        for account in ready:
            client.headers['X-Viber-Account'] = account
            reply_settings = api('replies')['settings']
            campaigns = [r['id'] for r in api('campaigns') if r['state'] == 'ACTIVE']
            state['accounts'][account] = {'settings': reply_settings, 'active_campaigns': campaigns}
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
                raise ValueError('Active operations did not finish. The old controller is still running; settings are saved in the backup.')
            time.sleep(.25)
        client.headers['X-Viber-Account'] = ready[0]
        api('shutdown', {'confirmed': True})
        process.wait(timeout=90)
        if send_records() != state['send_records']:
            raise ValueError('Send history changed during maintenance; inspect history before continuing.')
        for name in files:
            temporary = (root / name).with_suffix((root / name).suffix + '.contact-update')
            shutil.copy2(source / name, temporary)
            temporary.replace(root / name)
        print(json.dumps({'controller_stopped': True, 'installed': True, 'backup': str(backup), 'messages_sent': 0}))
    else:
        if not args.backup:
            raise ValueError('Pass the recorded deployment backup directory.')
        state = json.loads((args.backup / 'state.json').read_text(encoding='utf-8'))
        if counts() != state['counts']:
            raise ValueError('Ledger counts changed before verification; inspect the change.')
        if send_records() != state['send_records']:
            raise ValueError('Send history changed before verification; inspect the change.')
        for account, saved in state['accounts'].items():
            if account not in ready:
                raise ValueError('A previously connected account has not returned yet.')
            client.headers['X-Viber-Account'] = account
            assert api('replies')['settings']['enabled'] == saved['settings']['enabled']
        page = client.get('/viber').text
        assert 'Add a Viber contact' in page and 'Connect your authorized Android' not in page
        report = {'installed': True, 'accounts_ready': ready, 'counts_preserved': True, 'messages_sent': 0}
        (args.backup / 'validation.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report))
