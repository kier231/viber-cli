"""Install the verified two-slot controller while its predecessor is stopped."""
from pathlib import Path
import json
import shutil
import socket

source = Path(__file__).resolve().parent.parent
target = Path('C:/viber-cli')
manifest = json.loads((target/'data/instances.json').read_text(encoding='utf-8-sig'))
if len(manifest['instances']) != 2 or any(s['state'] != 'AWAITING_NUMBER' for s in manifest['instances']):
    raise ValueError('Finish preparing both fresh profiles before deploying.')
with socket.socket() as sock:
    if sock.connect_ex(('127.0.0.1',4001)) == 0:
        raise ValueError('Stop the previous controller before replacing its files.')
backup = Path(manifest['backup'])/'previous-controller'
files = ['web.py','app/accounts.py','app/auto_replies.py','app/campaigns.py','app/controller.py',
         'app/instances.py','app/reply_feedback.py','app/web_service.py','app/web_server.py',
         'app/vm_agent.py','app/viber_inbox.py','web/viber.html','web/viber.js','scripts/start_viber_vm.ps1',
         'scripts/start_outreach_host.ps1','scripts/start_two_instances_host.ps1']
for name in files:
    old = target/name
    if old.is_file() and not (backup/name).exists():
        (backup/name).parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(old,backup/name)
    old.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(source/name,old)
print('Two-slot controller installed. Previous controller and database backup retained.')
