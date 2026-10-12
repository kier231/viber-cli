"""Owner-requested contact reset and folder rollout; preserve reply preferences."""
from collections import defaultdict
import argparse
from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import httpx
import psutil
import psycopg

SOURCE = Path(__file__).resolve().parent.parent
ROOT = Path('C:/viber-cli')
sys.path.insert(0, str(SOURCE))
from app.runtime import settings
from app.storage import connect
from app.models import LeadStore
from app.contact_folders import ContactFolders, remove_contacts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--clear-contacts', action='store_true', help='Explicitly perform the owner-authorized contact reset.')
    if not parser.parse_args().clear_contacts:
        parser.error('Explicit --clear-contacts is required. Use deploy_csv_campaigns.py for updates that preserve contacts.')
    target = settings(ROOT)['VIBER_DATABASE_URL']
    tables = ('auto_reply_settings', 'auto_reply_account_settings')
    with closing(connect(target)) as db:
        preferences = {table: [dict(r) for r in db.execute('SELECT * FROM ' + table)] for table in tables}
        worker = dict(db.execute('SELECT * FROM account_workers WHERE stopped_at IS NULL ORDER BY heartbeat_at DESC LIMIT 1').fetchone())
    process = psutil.Process(worker['process_id'])
    if abs(process.create_time() - float(worker['process_started'])) > .01:
        raise ValueError('The previous controller identity changed.')
    if not any(v.casefold().replace('\\', '/') == 'c:/viber-cli/web.py' for v in process.cmdline()):
        raise ValueError('Unexpected controller process; reset blocked.')
    origin = 'http://127.0.0.1:4001'
    with httpx.Client(base_url=origin, timeout=10, headers={'Origin':origin, 'X-Viber-Browser':'1', 'X-Viber-Account':worker['account_id']}) as client:
        session = client.post('/viber/api/session', json={}); session.raise_for_status()
        client.headers['X-Viber-CSRF'] = session.json()['csrf']
        response = client.post('/viber/api/shutdown', json={'confirmed':True}); response.raise_for_status()
    process.wait(timeout=90)
    guard = psycopg.connect(target, autocommit=True)
    if not guard.execute("SELECT pg_try_advisory_lock(hashtext('viber-controller-lifetime'))").fetchone()[0]:
        guard.close()
        raise ValueError('A replacement controller is already running; reset blocked.')
    try:
        backup = ROOT / 'backups' / ('contact-folders-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
        backup.mkdir(parents=True)
        store = LeadStore(target)
        with closing(connect(target)) as db:
            account_ids = [r[0] for r in db.execute('SELECT id FROM accounts')]
        for account_id in account_ids:
            ContactFolders(SimpleNamespace(store=store, account_id=account_id))
        with closing(connect(target)) as db:
            if any([dict(r) for r in db.execute('SELECT * FROM ' + table)] != preferences[table] for table in tables):
                raise ValueError('Saved owner preferences changed; reset blocked.')
            if db.execute("SELECT 1 FROM controller_jobs WHERE state='RUNNING'").fetchone() or db.execute("SELECT 1 FROM web_sends WHERE state='SUBMITTING'").fetchone():
                raise ValueError('A worker has an unfinished action. Reset blocked.')
            before = {table: [dict(r) for r in db.execute('SELECT * FROM ' + table)] for table in
                      ('leads', 'contact_descriptions', 'contact_outreach', 'contact_folder_members', 'contact_owners', 'contact_removals', 'web_campaigns', 'controller_jobs', 'auto_reply_jobs')}
            before['preferences'] = preferences
            before['sends'] = [dict(r) for r in db.execute('SELECT id,state,attempted_at FROM web_sends ORDER BY id')]
            before['message_count'] = db.execute('SELECT COUNT(*) FROM viber_messages').fetchone()[0]
        (backup / 'before.json').write_text(json.dumps(before, indent=2), encoding='utf-8')
        files = ['app/contact_folders.py','app/models.py','app/accounts.py','app/web_service.py','app/web_server.py',
                 'app/csv_import.py','app/campaigns.py','web/viber.html','web/viber.js','README.md',
                 'scripts/deploy_contact_folders.py','scripts/deploy_csv_campaigns.py']
        installed = {}
        for name in files:
            destination = ROOT / name
            if destination.exists():
                shutil.copy2(destination, backup / ('previous_' + name.replace('/', '_')))
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SOURCE / name, destination)
            digest = hashlib.sha256(destination.read_bytes()).hexdigest()
            if digest != hashlib.sha256((SOURCE / name).read_bytes()).hexdigest():
                raise ValueError('Installed file hash mismatch: ' + name)
            installed[name] = digest
        (backup / 'installed.json').write_text(json.dumps(installed, indent=2), encoding='utf-8')
        with closing(connect(target)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            owned = {r['phone']:r['account_id'] for r in db.execute('SELECT phone,account_id FROM contact_owners')}
            groups = defaultdict(list)
            for row in db.execute('SELECT * FROM leads'):
                groups[owned.get(row['phone'], 'current')].append(row)
            deleted = sum(remove_contacts(db, rows, account_id)['deleted'] for account_id, rows in groups.items())
            assert db.execute('SELECT COUNT(*) FROM leads').fetchone()[0] == 0
            for row in before['sends']:
                if row['attempted_at'] is not None or row['state'] in ('DISPATCHED','UNKNOWN'):
                    assert dict(db.execute('SELECT id,state,attempted_at FROM web_sends WHERE id=?', (row['id'],)).fetchone()) == row
            assert db.execute('SELECT COUNT(*) FROM viber_messages').fetchone()[0] == before['message_count']
            for table, values in preferences.items():
                assert [dict(r) for r in db.execute('SELECT * FROM ' + table)] == values
            db.execute('INSERT INTO web_events(created_at,kind,detail,account_id) VALUES(?,?,?,?)',
                       (datetime.now().astimezone().isoformat(), 'CONTACTS_CLEARED', f'Owner removed {deleted} contact records. Audit history retained.', worker['account_id']))
        report = {'backup':str(backup),'deleted_contacts':deleted,'contacts_remaining':0,'preferences_preserved':True,
                  'message_history_preserved':True,'durable_send_markers_preserved':True,'controller_stopped':True}
        (backup / 'result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report), flush=True)
    finally:
        guard.close()


if __name__ == '__main__':
    main()
