"""Pause new real work, drain active operations, and save rollback settings."""
from pathlib import Path
import json
import sqlite3
import sys
import time
import httpx

root=Path('C:/viber-cli')
backup=root/'backups'/'sqlite-cutover'
backup.mkdir(parents=True,exist_ok=True)
origin='http://127.0.0.1:4001'
with httpx.Client(base_url=origin,timeout=100,headers={'Origin':origin,'X-Viber-Browser':'1'}) as c:
    r=c.post('/viber/api/session',json={});r.raise_for_status()
    c.headers['X-Viber-CSRF']=r.json()['csrf']
    def api(path,payload=None):
        r=c.get('/viber/api/'+path) if payload is None else c.post('/viber/api/'+path,json=payload)
        r.raise_for_status();return r.json()
    saved=api('replies')['settings']
    campaigns=api('campaigns')
    original={'reply_settings':saved,'active_campaigns':[r['id'] for r in campaigns if r['state']=='ACTIVE']}
    path=backup/'previous-settings.json'
    if path.exists():raise ValueError('Cutover backup already exists; do not overwrite rollback settings.')
    path.write_text(json.dumps(original,indent=2),encoding='utf-8')
    api('replies',{'enabled':False,'instructions':saved['instructions']})
    for campaign in original['active_campaigns']:api('campaign-pause',{'campaign_id':campaign})
    deadline=time.monotonic()+200
    while time.monotonic()<deadline:
        if api('status')['pending']==0 and not any(j['state'] in ('GENERATING','QUEUED','SUBMITTING') for j in api('replies')['jobs']):break
        time.sleep(.5)
    else:raise ValueError('Active operations did not drain; the previous process must remain running.')
with sqlite3.connect(root/'leads.sqlite3') as db:
    if db.execute("SELECT 1 FROM web_sends WHERE state IN ('QUEUED','SUBMITTING')").fetchone():
        raise ValueError('Send remains active; stopping is blocked.')
    with sqlite3.connect(backup/'leads-before-stop.sqlite3') as copy:db.backup(copy)
print(json.dumps({'paused':True,'active_operations_drained':True,'backup_created':True,'previous_drafting_enabled':saved['enabled']}))
