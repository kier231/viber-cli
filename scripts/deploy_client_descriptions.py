"""Install tested client context/research without changing saved reply preferences."""
from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import sys

import httpx
import psutil

SOURCE = Path(__file__).resolve().parent.parent
ROOT = Path('C:/viber-cli')
sys.path.insert(0, str(SOURCE))
from app.runtime import settings
from app.storage import connect


def assert_idle(db):
    for table, states in [('web_operations', "'QUEUED','RUNNING'"),
                          ('controller_jobs', "'PENDING','RUNNING'"),
                          ('auto_reply_jobs', "'GENERATING','QUEUED','SUBMITTING','RETRY','REGENERATE','DRAFT'"),
                          ('web_sends', "'QUEUED','SUBMITTING'")]:
        if db.execute(f'SELECT 1 FROM {table} WHERE state IN ({states})').fetchone():
            raise ValueError('Wait for active/unsent work to drain before updating.')


def main():
    target = settings(ROOT)['VIBER_DATABASE_URL']
    tables = ('auto_reply_settings', 'auto_reply_account_settings')
    with closing(connect(target)) as db:
        assert_idle(db)
        before = {table: [dict(r) for r in db.execute('SELECT * FROM ' + table)] for table in tables}
        worker = dict(db.execute('SELECT * FROM account_workers WHERE account_id IN '
                                '(SELECT id FROM accounts WHERE enabled=1 AND paused=0) LIMIT 1').fetchone())
    process = psutil.Process(worker['process_id'])
    if abs(process.create_time() - float(worker['process_started'])) > .01:
        raise ValueError('Controller process identity changed.')
    if not any(v.casefold().replace('\\', '/') == 'c:/viber-cli/web.py' for v in process.cmdline()):
        raise ValueError('Unexpected controller process; replacement blocked.')
    backup = ROOT / 'backups' / ('client-descriptions-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    backup.mkdir(parents=True)
    origin = 'http://127.0.0.1:4001'
    with httpx.Client(base_url=origin, timeout=10, headers={'Origin': origin, 'X-Viber-Browser': '1',
                      'X-Viber-Account': worker['account_id']}) as client:
        session = client.post('/viber/api/session', json={}); session.raise_for_status()
        client.headers['X-Viber-CSRF'] = session.json()['csrf']
        result = client.post('/viber/api/shutdown', json={'confirmed': True}); result.raise_for_status()
    process.wait(timeout=45)
    with closing(connect(target)) as db:
        assert_idle(db)
        if any([dict(r) for r in db.execute('SELECT * FROM ' + table)] != before[table] for table in tables):
            raise ValueError('Owner preferences changed during shutdown; replacement blocked.')
        state = {'settings': before,
                 'sends': [dict(r) for r in db.execute('SELECT id,state,attempted_at FROM web_sends ORDER BY id')],
                 'counts': {table: db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] for table in
                            ('leads', 'viber_messages', 'viber_conversations', 'contact_owners')},
                 'sources': [dict(r) for r in db.execute('SELECT * FROM viber_sources')]}
    (backup / 'state.json').write_text(json.dumps(state, indent=2), encoding='utf-8')
    files = ['app/client_descriptions.py', 'app/web_service.py', 'app/web_server.py',
             'app/auto_replies.py', 'app/codex_replies.py', 'app/portfolio.py', 'web/viber.js',
             'LOCAL-CONTROLLER.md', 'scripts/deploy_client_descriptions.py', 'scripts/deploy_local.py',
             'scripts/deploy_reply_recovery.py', 'scripts/deploy_reply_voice.py', 'scripts/deploy_two_instances.py']
    installed = {}
    for name in files:
        destination = ROOT / name
        if destination.exists():
            shutil.copy2(destination, backup / ('previous_' + name.replace('/', '_')))
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SOURCE / name, destination)
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        if digest != hashlib.sha256((SOURCE / name).read_bytes()).hexdigest():
            raise ValueError('Installed hash mismatch: ' + name)
        installed[name] = digest
    (backup / 'installed.json').write_text(json.dumps(installed, indent=2), encoding='utf-8')
    print(json.dumps({'backup': str(backup), 'controller_stopped': True,
                      'preferences_preserved': True, 'messages_sent_by_deployment': 0}), flush=True)


if __name__ == '__main__':
    main()
