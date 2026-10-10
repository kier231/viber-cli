"""Deploy validated source after the old localhost controller is stopped."""
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess

source=Path(__file__).resolve().parent.parent
target=Path('C:/viber-cli')
backup=target/'backups'/'sqlite-cutover'
with socket.socket() as sock:
    if sock.connect_ex(('127.0.0.1',4001))==0:
        raise ValueError('The previous controller must be confirmed stopped before deployment.')
archive=backup/'previous-source.zip'
if archive.exists():raise ValueError('Previous-source archive exists; refusing to replace rollback source.')
subprocess.run(['git','-C',str(target),'archive','--format=zip','--output',str(archive),'HEAD'],check=True)
with sqlite3.connect(target/'leads.sqlite3') as old,sqlite3.connect(backup/'leads.sqlite3') as copy:
    old.backup(copy)
    if copy.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Backup integrity verification failed.')
files=['.gitignore','.env','compose.yaml','requirements-controller.txt','LOCAL-CONTROLLER.md','main.py','web.py',
       'app/accounts.py','app/job_queue.py','app/runtime.py','app/storage.py','app/controller.py','app/controller_cli.py',
       'app/auto_replies.py','app/codex_replies.py','app/reply_voice.py','app/reply_errors.py','app/portfolio.py','app/reply_feedback.py','data/portfolio.json','prompts/sajtolog-replies.txt','app/models.py','app/viber_inbox.py','app/viber_database.py',
       'app/viber_source_worker.py','app/viber_watcher.py','app/web_service.py','app/web_server.py','app/client_descriptions.py',
       'web/viber.js','web/viber.html','scripts/benchmark_replies.py','scripts/migrate_postgres.py',
       'scripts/pause_for_cutover.py','scripts/deploy_local.py','scripts/validate_rollout.py',
       'scripts/start_outreach_host.ps1','scripts/validate_reply_feedback.py','tests/test_reply_feedback.py','tests/test_controller.py']
for name in files:
    destination=target/name
    destination.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(source/name,destination)
print('Source deployed; previous source and verified SQLite backup retained.')
