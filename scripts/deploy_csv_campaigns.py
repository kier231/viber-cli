"""Install campaign/CSV queue changes after graceful shutdown, preserving owner flags."""
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


def main():
    target = settings(ROOT)['VIBER_DATABASE_URL']
    tables = ('auto_reply_settings', 'auto_reply_account_settings')
    with closing(connect(target)) as db:
        before = {table: [dict(r) for r in db.execute('SELECT * FROM ' + table)] for table in tables}
        worker = db.execute('SELECT * FROM account_workers WHERE stopped_at IS NULL ORDER BY heartbeat_at DESC LIMIT 1').fetchone()
        if not worker:
            raise ValueError('No live controller identity is available.')
        worker = dict(worker)
        if db.execute("SELECT 1 FROM web_sends WHERE state IN ('QUEUED','SUBMITTING')").fetchone():
            raise ValueError('Wait for active sends to finish before deployment.')
    process = psutil.Process(worker['process_id'])
    if abs(process.create_time() - float(worker['process_started'])) > .01:
        raise ValueError('Controller process identity changed.')
    if not any(v.casefold().replace('\\', '/') == 'c:/viber-cli/web.py' for v in process.cmdline()):
        raise ValueError('Unexpected controller process; replacement blocked.')
    origin = 'http://127.0.0.1:4001'
    with httpx.Client(base_url=origin, timeout=10, headers={'Origin': origin, 'X-Viber-Browser': '1',
                      'X-Viber-Account': worker['account_id']}) as client:
        session = client.post('/viber/api/session', json={}); session.raise_for_status()
        client.headers['X-Viber-CSRF'] = session.json()['csrf']
        response = client.post('/viber/api/shutdown', json={'confirmed': True}); response.raise_for_status()
    process.wait(timeout=90)
    backup = ROOT / 'backups' / ('csv-campaigns-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    backup.mkdir(parents=True)
    with closing(connect(target)) as db:
        if any([dict(r) for r in db.execute('SELECT * FROM ' + table)] != before[table] for table in tables):
            raise ValueError('Saved owner preferences changed; deployment stopped.')
        if db.execute("SELECT 1 FROM web_sends WHERE state='SUBMITTING'").fetchone():
            raise ValueError('An unfinished Send needs reconciliation.')
        state = {'settings': before, 'sends': [dict(r) for r in db.execute('SELECT id,state,attempted_at FROM web_sends ORDER BY id')],
                 'counts': {t: db.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0] for t in
                            ('leads', 'viber_messages', 'viber_conversations', 'contact_owners')}}
    (backup / 'state.json').write_text(json.dumps(state, indent=2), encoding='utf-8')
    files = ['app/csv_import.py', 'app/campaigns.py', 'app/job_queue.py', 'app/models.py', 'app/web_service.py', 'app/web_server.py',
             'web/viber.html', 'web/viber.js', 'README.md', 'scripts/deploy_csv_campaigns.py']
    installed = {}
    attempted = []
    try:
        for name in files:
            destination = ROOT / name
            if destination.exists():
                shutil.copy2(destination, backup / ('previous_' + name.replace('/', '_')))
            attempted.append(name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SOURCE / name, destination)
            digest = hashlib.sha256(destination.read_bytes()).hexdigest()
            if digest != hashlib.sha256((SOURCE / name).read_bytes()).hexdigest():
                raise ValueError('Installed file hash mismatch: ' + name)
            installed[name] = digest
    except Exception:
        for name in attempted:
            previous = backup / ('previous_' + name.replace('/', '_'))
            if previous.exists():
                shutil.copy2(previous, ROOT / name)
        raise
    (backup / 'installed.json').write_text(json.dumps(installed, indent=2), encoding='utf-8')
    print(json.dumps({'backup': str(backup), 'controller_stopped': True, 'preferences_preserved': True}), flush=True)


if __name__ == '__main__':
    main()
