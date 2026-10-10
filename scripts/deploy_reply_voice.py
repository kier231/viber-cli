"""Install a tested voice update while preserving automatic delivery and history."""
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
sys.path.insert(0,str(SOURCE))
from app.runtime import settings
from app.storage import connect
from app.viber_inbox import utc_now


def main():
    target=settings(ROOT)['VIBER_DATABASE_URL']
    expected=(ROOT/'prompts/sajtolog-replies.txt').read_text(encoding='utf-8').strip()
    instructions=(SOURCE/'prompts/sajtolog-replies.txt').read_text(encoding='utf-8').strip()
    if not 0<len(instructions)<=8000:
        raise ValueError('Prompt exceeds dashboard limit.')
    tables=('auto_reply_settings','auto_reply_account_settings')
    with closing(connect(target)) as db:
        before={table:[dict(r) for r in db.execute('SELECT * FROM '+table)] for table in tables}
        if any(r['instructions']!=expected for rows in before.values() for r in rows):
            raise ValueError('Custom owner instructions changed; merge them before deploying.')
        for table,states in [('web_operations',"'QUEUED','RUNNING'"),
                             ('controller_jobs',"'PENDING','RUNNING'"),
                             ('auto_reply_jobs',"'GENERATING','QUEUED','SUBMITTING','RETRY','REGENERATE','DRAFT'"),
                             ('web_sends',"'QUEUED','SUBMITTING'")]:
            if db.execute(f'SELECT 1 FROM {table} WHERE state IN ({states})').fetchone():
                raise ValueError('Drain active/unsent work before updating instructions.')
        worker=dict(db.execute('SELECT * FROM account_workers WHERE account_id IN '
                              '(SELECT id FROM accounts WHERE enabled=1 AND paused=0) LIMIT 1').fetchone())
        sends=[dict(r) for r in db.execute('SELECT id,state,attempted_at FROM web_sends ORDER BY id')]
        counts={t:db.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in
                ('leads','viber_messages','viber_conversations','contact_owners')}
    process=psutil.Process(worker['process_id'])
    if abs(process.create_time()-float(worker['process_started']))>.01:
        raise ValueError('Controller process identity changed.')
    if not any(v.casefold().replace('\\','/')=='c:/viber-cli/web.py' for v in process.cmdline()):
        raise ValueError('Unexpected controller process; replacement blocked.')
    backup=ROOT/'backups'/('reply-voice-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
    backup.mkdir(parents=True)
    (backup/'state.json').write_text(json.dumps({'settings':before,'sends':sends,'counts':counts},indent=2),encoding='utf-8')
    origin='http://127.0.0.1:4001'
    with httpx.Client(base_url=origin,timeout=10,headers={'Origin':origin,'X-Viber-Browser':'1',
                     'X-Viber-Account':worker['account_id']}) as client:
        session=client.post('/viber/api/session',json={});session.raise_for_status()
        client.headers['X-Viber-CSRF']=session.json()['csrf']
        result=client.post('/viber/api/shutdown',json={'confirmed':True});result.raise_for_status()
    process.wait(timeout=45)
    with closing(connect(target)) as db:
        if any([dict(r) for r in db.execute('SELECT * FROM '+table)]!=before[table] for table in tables):
            raise ValueError('Owner settings changed during shutdown; do not overwrite them.')
        if db.execute("SELECT 1 FROM auto_reply_jobs WHERE state IN ('GENERATING','QUEUED','SUBMITTING','RETRY','REGENERATE','DRAFT')").fetchone():
            raise ValueError('New work arrived during shutdown. Restart and drain before retrying.')
    files=['app/codex_replies.py','app/reply_voice.py','app/auto_replies.py','app/viber_inbox.py',
           'prompts/sajtolog-replies.txt','prompts/sajtolog-replies.example.txt','.gitignore',
           'LOCAL-CONTROLLER.md','REPLY-VOICE.md','scripts/evaluate_reply_voice.py',
           'scripts/deploy_reply_voice.py']
    installed={}
    for name in files:
        destination=ROOT/name
        if destination.exists():
            shutil.copy2(destination,backup/('previous_'+name.replace('/','_')))
        destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(SOURCE/name,destination)
        digest=hashlib.sha256(destination.read_bytes()).hexdigest()
        if digest!=hashlib.sha256((SOURCE/name).read_bytes()).hexdigest():
            raise ValueError('Installed file hash did not match: '+name)
        installed[name]=digest
    with closing(connect(target)) as db,db:
        for table,rows in before.items():
            for row in rows:
                changed=db.execute('UPDATE '+table+' SET instructions=?,revision=revision+1 WHERE id=? AND revision=?',
                                   (instructions,row['id'],row['revision'])).rowcount
                if changed!=1:
                    raise ValueError('Instruction revision changed; deployment cannot proceed.')
        for table,rows in before.items():
            for old in rows:
                new=dict(db.execute('SELECT * FROM '+table+' WHERE id=?',(old['id'],)).fetchone())
                assert all(old[k]==new[k] for k in old if k not in ('instructions','revision'))
    (backup/'installed.json').write_text(json.dumps(installed,indent=2),encoding='utf-8')
    print(json.dumps({'backup':str(backup),'controller_stopped':True,'automatic_preferences_preserved':True,
                     'detection_checkpoints_preserved':True,'messages_sent_by_deployment':0}),flush=True)


if __name__=='__main__':
    main()
