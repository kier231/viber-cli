"""Drain the controller, preserve preferences and install the tested recovery code."""
from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import sys

import httpx
import psutil

SOURCE=Path(__file__).resolve().parent.parent
ROOT=Path('C:/viber-cli')
sys.path.insert(0,str(ROOT))
from app.runtime import settings
from app.storage import connect

backup=ROOT/'backups'/('reply-recovery-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
backup.mkdir()
with closing(connect(settings(ROOT)['VIBER_DATABASE_URL'])) as db:
    for table,states in [('web_operations',"'QUEUED','RUNNING'"),
                         ('controller_jobs',"'PENDING','RUNNING'"),
                         ('auto_reply_jobs',"'GENERATING','QUEUED','SUBMITTING'"),
                         ('web_sends',"'QUEUED','SUBMITTING'")]:
        if db.execute(f'SELECT 1 FROM {table} WHERE state IN ({states})').fetchone():
            raise RuntimeError('Finish active operations before maintenance.')
    worker=dict(db.execute("SELECT * FROM account_workers WHERE account_id='instance-1'").fetchone())
    before={table:[dict(r) for r in db.execute('SELECT * FROM '+table)] for table in
            ('auto_reply_account_settings','auto_reply_settings','accounts','contact_owners','leads',
             'viber_sources','viber_conversations','viber_messages','auto_reply_jobs','auto_reply_events',
             'web_sends','controller_jobs','web_operations')}
(backup/'state.json').write_text(json.dumps(before,ensure_ascii=True,indent=2),encoding='utf-8')
process=psutil.Process(worker['process_id'])
if abs(process.create_time()-float(worker['process_started']))>.01:
    raise RuntimeError('Controller identity changed.')
if not any(v.casefold().replace('\\','/')=='c:/viber-cli/web.py' for v in process.cmdline()):
    raise RuntimeError('Unexpected controller process; replacement blocked.')
origin='http://127.0.0.1:4001'
with httpx.Client(base_url=origin,timeout=10,headers={'Origin':origin,'X-Viber-Browser':'1','X-Viber-Account':'instance-1'}) as client:
    session=client.post('/viber/api/session',json={});session.raise_for_status()
    client.headers['X-Viber-CSRF']=session.json()['csrf']
    result=client.post('/viber/api/shutdown',json={'confirmed':True});result.raise_for_status()
try:
    process.wait(timeout=45)
except psutil.TimeoutExpired:
    raise RuntimeError('Old controller is still running. Replacement blocked.') from None
files=['app/auto_replies.py','app/reply_errors.py','app/codex_replies.py','app/reply_voice.py','app/web_service.py','app/client_descriptions.py',
       'app/job_queue.py','app/viber_watcher.py','app/vm_bridge.py','app/viber_background.py',
       'app/vm_agent.py','web/viber.js']
hashes={}
for relative in files:
    destination=ROOT/relative
    if destination.exists():
        old=backup/('previous_'+relative.replace('/','_'))
        shutil.copy2(destination,old)
    shutil.copy2(SOURCE/relative,destination)
    digest=hashlib.sha256(destination.read_bytes()).hexdigest()
    if digest!=hashlib.sha256((SOURCE/relative).read_bytes()).hexdigest():
        raise RuntimeError('Installed source did not match.')
    hashes[relative]=digest
with closing(connect(settings(ROOT)['VIBER_DATABASE_URL'])) as db:
    for table in ('auto_reply_account_settings','auto_reply_settings'):
        if [dict(r) for r in db.execute('SELECT * FROM '+table)]!=before[table]:
            raise RuntimeError('Owner preferences changed during maintenance.')
(backup/'installed.json').write_text(json.dumps(hashes,indent=2),encoding='utf-8')
print(json.dumps({'backup':str(backup),'controller_stopped':True,'preferences_preserved':True}),flush=True)
