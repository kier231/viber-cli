"""Maintenance and verification for the manually invoked VM fix installer."""
from contextlib import closing
import json
from pathlib import Path
import sys
import time
import httpx
import psutil

root = Path('C:/viber-cli')
sys.path.insert(0, str(root))
from app.runtime import settings
from app.storage import connect
from app.vm_bridge import VmBridge, load_vm_config

state_file = Path(__file__).resolve().parent / 'profile-fix-state.json'
mode = sys.argv[1]
origin = 'http://127.0.0.1:4001'

if mode == 'preflight':
    config = load_vm_config()
    if not config:
        raise SystemExit('Saved VM bridge configuration is missing.')
    try:
        if VmBridge(config).call('/health', timeout=10).get('status') != 'ready':
            raise RuntimeError('The existing VM agent is not ready.')
    except Exception:
        if '--resume-failed-maintenance' not in sys.argv[2:] or not state_file.is_file():
            raise SystemExit('The existing VM agent is unavailable. Recovery requires a recorded interrupted installation.')
        baseline = json.loads(state_file.read_text(encoding='utf-8'))
        with closing(connect(settings(root)['VIBER_DATABASE_URL'])) as db:
            if db.execute('SELECT COUNT(*) FROM web_sends').fetchone()[0] != baseline['counts']['web_sends']:
                raise SystemExit('Send history changed since the interrupted installation. Inspect it before recovery.')
        print('Recorded interrupted installation verified. The guest installer must confirm the old agent has exited before restarting.')
    else:
        print('Saved loopback bridge configuration and existing VM agent health verified.')
    raise SystemExit(0)

if mode == 'health':
    deadline = time.monotonic() + 90
    bridge = VmBridge(load_vm_config())
    while time.monotonic() < deadline:
        try:
            inspection = bridge.call('/desktop/inspect', timeout=10)
            assert not inspection['draft_pending'], 'A VM draft is pending.'
            print('Updated VM agent is ready.')
            break
        except Exception:
            time.sleep(1)
    else:
        raise SystemExit('Updated VM agent did not become ready within 90 seconds.')
    raise SystemExit(0)

url = settings(root)['VIBER_DATABASE_URL']
with httpx.Client(base_url=origin, headers={'Origin': origin, 'X-Viber-Browser': '1'}, timeout=20) as client:
    r = client.post('/viber/api/session', json={}); r.raise_for_status()
    client.headers['X-Viber-CSRF'] = r.json()['csrf']
    if mode == 'stop':
        with closing(connect(url)) as db:
            for table, states in [('web_operations', "'QUEUED','RUNNING'"),
                                  ('auto_reply_jobs', "'GENERATING','QUEUED','SUBMITTING'"),
                                  ('controller_jobs', "'RUNNING'"), ('web_sends', "'SUBMITTING'")]:
                assert not db.execute('SELECT 1 FROM ' + table + ' WHERE state IN (' + states + ')').fetchone(), 'Wait for active work to finish before installing.'
            worker = dict(db.execute("SELECT process_id,process_started FROM account_workers WHERE account_id='current'").fetchone())
            state = {'counts': {t: db.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0]
                                for t in ('leads','viber_messages','web_sends')}, **worker}
        process = psutil.Process(worker['process_id'])
        assert str(process.create_time()) == worker['process_started']
        state_file.write_text(json.dumps(state), encoding='utf-8')
        r = client.post('/viber/api/shutdown', json={'confirmed': True}); r.raise_for_status()
        process.wait(timeout=90)
        print('Controller stopped; no active sends or generations.')
    elif mode == 'verify':
        state = json.loads(state_file.read_text(encoding='utf-8'))
        with closing(connect(url)) as db:
            assert db.execute('SELECT COUNT(*) FROM web_sends').fetchone()[0] == state['counts']['web_sends'], 'Send count changed during maintenance; inspect history.'
        r = client.post('/viber/api/prepare', json={'lead_id': 1, 'text': 'Provera pregleda poruke.'}); r.raise_for_status()
        operation = r.json()['operation_id']
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            r = client.get('/viber/api/operations/' + operation); r.raise_for_status()
            result = r.json()
            if result['state'] == 'SUCCEEDED': break
            if result['state'] in ('FAILED','INTERRUPTED'): raise RuntimeError(result['error'])
            time.sleep(.2)
        else: raise RuntimeError('Review verification timed out.')
        with closing(connect(url)) as db:
            assert db.execute('SELECT COUNT(*) FROM web_sends').fetchone()[0] == state['counts']['web_sends']
        report = {'review_verified': True, 'messages_sent': 0}
        state_file.with_name('profile-fix-validation.json').write_text(json.dumps(report), encoding='utf-8')
        print('Message review verified. No message was sent. Reload the dashboard.')
    else:
        raise SystemExit('Unknown maintenance mode.')
