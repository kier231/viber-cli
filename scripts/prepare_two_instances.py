"""Preserve and stop the old account, then create two network-isolated linked VMs."""
from contextlib import closing
from datetime import datetime
from pathlib import Path
import json
import os
import secrets
import subprocess
import sys
import time

import httpx
import psutil

ROOT = Path('C:/viber-cli')
sys.path.insert(0, str(ROOT))
from app.runtime import settings
from app.storage import connect

VBOX = Path(os.environ['ProgramFiles']) / 'Oracle/VirtualBox/VBoxManage.exe'
MANIFEST = ROOT / 'data/instances.json'


def vbox(*arguments):
    result = subprocess.run([str(VBOX), *arguments], capture_output=True, text=True, check=True)
    return result.stdout


def info(name):
    return dict(line.split('=', 1) for line in vbox('showvminfo', name, '--machinereadable').splitlines() if '=' in line)


def stop_offline_controller(backup, url):
    with closing(connect(url)) as db, db:
        worker = dict(db.execute("SELECT process_id,process_started FROM account_workers WHERE account_id='current'").fetchone())
        try:
            process = psutil.Process(worker['process_id'])
            if str(process.create_time()) == worker['process_started'] and process.is_running():
                raise SystemExit('The controller process is still running. Offline replacement is blocked.')
        except psutil.NoSuchProcess:
            pass
        for table, states in [('controller_jobs', "'RUNNING'"), ('web_sends', "'SUBMITTING'"), ('auto_reply_jobs', "'GENERATING','SUBMITTING'")]:
            if db.execute('SELECT 1 FROM ' + table + ' WHERE state IN (' + states + ') LIMIT 1').fetchone():
                raise SystemExit('An unfinished operation needs reconciliation before replacement.')
        old_settings = dict(db.execute('SELECT * FROM auto_reply_settings WHERE id=1').fetchone())
        campaigns = [dict(row) for row in db.execute('SELECT * FROM web_campaigns')]
        (backup / 'previous-settings.json').write_text(json.dumps({'reply_settings': old_settings, 'campaigns': campaigns}, indent=2), encoding='utf-8')
        counts = {table: db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] for table in ('leads', 'viber_messages', 'web_sends')}
        (backup / 'baseline.json').write_text(json.dumps({'counts': counts, 'worker': worker}), encoding='utf-8')
        db.execute('UPDATE auto_reply_settings SET enabled=0,revision=revision+1 WHERE id=1')
        db.execute("UPDATE web_campaigns SET state='PAUSED',revision=revision+1 WHERE state='ACTIVE'")
        db.execute("UPDATE accounts SET paused=1 WHERE id='current'")
        db.execute("UPDATE account_workers SET stopped_at=? WHERE account_id='current'", (datetime.now().astimezone().isoformat(),))
    print('Recorded old controller process is stopped; history backed up and outgoing work paused.', flush=True)


def main():
    if MANIFEST.exists():
        raise SystemExit('An instance manifest already exists. Resume the recorded setup instead of cloning again.')
    names = ('ViberWorker1', 'ViberWorker2')
    registered = vbox('list', 'vms')
    if any('"' + name + '"' in registered for name in names):
        raise SystemExit('A replacement VM name already exists. Inspect it before continuing.')
    backup = ROOT / 'backups' / ('two-instances-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    backup.mkdir(parents=True)
    url = settings(ROOT)['VIBER_DATABASE_URL']
    container = subprocess.check_output(['docker', 'compose', '--project-directory', str(ROOT), 'ps', '-q', 'postgres'], text=True).strip()
    with (backup / 'postgres.dump').open('wb') as output:
        subprocess.run(['docker', 'exec', container, 'pg_dump', '-U', 'viber', '-d', 'viber', '--schema=public', '-Fc'], stdout=output, check=True)
    origin = 'http://127.0.0.1:4001'
    if '--controller-already-stopped' in sys.argv:
        stop_offline_controller(backup, url)
    else:
        stop_running_controller(backup, url, origin)
    print('Old controller stopped; history backed up and outgoing work paused.', flush=True)
    with closing(connect(url)) as db, db:
        db.execute("UPDATE accounts SET enabled=0,paused=1 WHERE id='current'")
    if info('ViberWorker')['VMState'] == '"running"':
        vbox('controlvm', 'ViberWorker', 'acpipowerbutton')
    deadline = time.monotonic() + 150
    while info('ViberWorker')['VMState'] != '"poweroff"':
        if time.monotonic() > deadline:
            raise SystemExit('The old VM has not shut down. No clone was started.')
        time.sleep(2)
    print('Old Viber VM powered off and preserved.', flush=True)
    snapshot = 'Two-account-base-' + datetime.now().strftime('%Y%m%d-%H%M%S')
    vbox('snapshot', 'ViberWorker', 'take', snapshot, '--description', 'Preserved original account; base for two isolated replacements. Do not delete while linked clones use it.')
    manifest = {'version': 1, 'backup': str(backup), 'base_vm': 'ViberWorker', 'base_snapshot': snapshot, 'instances': []}
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    for number, name in enumerate(names, 1):
        vbox('clonevm', 'ViberWorker', '--snapshot', snapshot, '--options', 'link', '--name', name, '--register')
        vbox('modifyvm', name, '--memory', '2048', '--cpus', '2', '--cableconnected1', 'off')
        vbox('modifyvm', name, '--natpf1', 'delete', 'viberbridge')
        port = 4011 + number
        vbox('modifyvm', name, '--natpf1', f'viberbridge{number},tcp,127.0.0.1,{port},,4011')
        identity = info(name)
        private = ROOT / 'private' / f'instance-{number}.json'
        token = secrets.token_urlsafe(36)
        private.write_text(json.dumps({'url': f'http://127.0.0.1:{port}', 'token': token}), encoding='utf-8')
        slot = {'id': f'instance-{number}', 'label': f'Viber {number}', 'vm_name': name,
                'vm_uuid': identity['UUID'].strip('"'), 'hardware_uuid': identity['hardwareuuid'].strip('"'),
                'bridge_config': str(private), 'state': 'NETWORK_ISOLATED', 'phone': None}
        manifest['instances'].append(slot)
        MANIFEST.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        print(f'{name} created: 2 GB RAM, two CPUs, loopback port {port}, network disconnected.', flush=True)
    print('Two VMs prepared. Clear copied Viber identities before reconnecting their networks.', flush=True)


def stop_running_controller(backup, url, origin):
    with httpx.Client(base_url=origin, headers={'Origin': origin, 'X-Viber-Browser': '1'}, timeout=30) as client:
        response = client.post('/viber/api/session', json={}); response.raise_for_status()
        client.headers['X-Viber-CSRF'] = response.json()['csrf']
        reply = client.get('/viber/api/replies'); reply.raise_for_status()
        old_settings = reply.json()['settings']
        campaigns = client.get('/viber/api/campaigns'); campaigns.raise_for_status()
        original = {'reply_settings': old_settings, 'campaigns': campaigns.json()}
        (backup / 'previous-settings.json').write_text(json.dumps(original, indent=2), encoding='utf-8')
        with closing(connect(url)) as db, db:
            db.execute("UPDATE accounts SET paused=1 WHERE id='current'")
        response = client.post('/viber/api/replies', json={'enabled': False, 'instructions': old_settings['instructions']}); response.raise_for_status()
        for campaign in original['campaigns']:
            if campaign['state'] == 'ACTIVE':
                response = client.post('/viber/api/campaign-pause', json={'campaign_id': campaign['id']}); response.raise_for_status()
        deadline = time.monotonic() + 100
        while True:
            with closing(connect(url)) as db:
                active = any(db.execute('SELECT 1 FROM ' + table + ' WHERE state IN (' + states + ') LIMIT 1').fetchone()
                             for table, states in [('controller_jobs', "'RUNNING'"), ('web_sends', "'SUBMITTING'"), ('auto_reply_jobs', "'GENERATING','SUBMITTING'")])
                worker = dict(db.execute("SELECT process_id,process_started FROM account_workers WHERE account_id='current'").fetchone())
                counts = {table: db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] for table in ('leads', 'viber_messages', 'web_sends')}
            if not active:
                break
            if time.monotonic() > deadline:
                raise SystemExit('Active operations did not drain. Old VM remains running and paused.')
            time.sleep(.5)
        process = psutil.Process(worker['process_id'])
        if str(process.create_time()) != worker['process_started']:
            raise SystemExit('Controller identity changed; shutdown blocked.')
        (backup / 'baseline.json').write_text(json.dumps({'counts': counts, 'worker': worker}), encoding='utf-8')
        response = client.post('/viber/api/shutdown', json={'confirmed': True}); response.raise_for_status()
        process.wait(timeout=100)


if __name__ == '__main__':
    main()
