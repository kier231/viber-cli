"""Save live health and isolated PostgreSQL wake-up timing without real sends."""
import json
import os
from pathlib import Path
import sys
import time
import httpx
sys.path[:0]=[str(Path(__file__).resolve().parent.parent),str(Path(__file__).resolve().parent.parent/'tests')]
from app.runtime import settings
from test_controller import DraftTests

os.environ['VIBER_TEST_POSTGRES_URL']=settings()['VIBER_DATABASE_URL']
test=DraftTests()
test.setUp()
try:
    test.replies.settle_seconds=1
    test.replies.start()
    test.incoming()
    committed=time.perf_counter()
    while not test.generator.contexts and time.perf_counter()-committed<3:
        time.sleep(.005)
    elapsed=time.perf_counter()-committed
    assert test.generator.contexts and elapsed<2.5,'Idle draft wake-up exceeded the 1.5-second budget after settling.'
    test.wait_state('DRAFT')
    assert not test.desktop.sent
finally:
    test.tearDown()
origin='http://127.0.0.1:4001'
with httpx.Client(base_url=origin,headers={'Origin':origin,'X-Viber-Browser':'1'},timeout=20) as c:
    r=c.post('/viber/api/session',json={});r.raise_for_status()
    c.headers['X-Viber-CSRF']=r.json()['csrf']
    status=c.get('/viber/api/status').json()
    inbox=c.get('/viber/api/database/status').json()
    replies=c.get('/viber/api/replies').json()
    assert inbox['state']=='WATCHING' and inbox['poll_seconds']==1
    assert sum(a['enabled'] for a in status['accounts'])==1
    assert replies['settings']['review_required'] and replies['settings']['generation_slots']==2
    assert not any(j['state'] in ('GENERATING','DRAFT','QUEUED','SUBMITTING') for j in replies['jobs'])
    email=c.get('/').status_code
benchmark=json.loads(Path('benchmark-results/expanded.json').read_text())
assert benchmark['passed']
report={'enabled_real_accounts':1,'inbox_healthy':True,'contacts':status['contacts'],
    'history_messages':inbox['messages'],'review_required':True,'generation_slots':2,
    'isolated_ingestion_to_generation_seconds':round(elapsed,3),
    'after_one_second_settle_seconds':round(max(0,elapsed-1),3),
    'email_dashboard_status':email,'imported_history_produced_drafts':False,
    'benchmark':benchmark,'simulated_account_workers':10,'simulated_jobs':1000}
destination=Path('C:/viber-cli/backups/sqlite-cutover/validation-report.json')
destination.write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in report.items() if k!='benchmark'}))
