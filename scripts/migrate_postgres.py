"""Import an offline SQLite backup and set a fresh reply cutover boundary."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from app.runtime import settings
from app.storage import connect, import_sqlite


def migrate(source,report,enabled=False):
    target=settings()['VIBER_DATABASE_URL']
    counts=import_sqlite(source,target)
    with closing(sqlite3.connect(source)) as old,closing(connect(target)) as db,db:
        old_checkpoints=dict(old.execute('SELECT source_id,checkpoint FROM viber_sources'))
        new_checkpoints=dict(db.execute('SELECT source_id,checkpoint FROM viber_sources'))
        if old_checkpoints!=new_checkpoints:raise ValueError('Checkpoint verification failed.')
        old_sends=dict(old.execute('SELECT id,state FROM web_sends'))
        new_sends=dict(db.execute('SELECT id,state FROM web_sends'))
        if old_sends!=new_sends:raise ValueError('Send-state verification failed.')
        cutoff=db.execute('SELECT COALESCE(MAX(rowid),0) FROM viber_messages').fetchone()[0]
        db.execute('UPDATE auto_reply_settings SET enabled=?,revision=revision+1,since_rowid=?,since_ms=? WHERE id=1',(int(enabled),cutoff,int(time.time()*1000)))
    report.parent.mkdir(parents=True,exist_ok=True)
    report.write_text(json.dumps({'tables':counts,'checkpoints_verified':True,'send_states_verified':True,'reply_cutoff_rowid':cutoff,'new_drafts_enabled':enabled},indent=2),encoding='utf-8')
    print(json.dumps({'tables_verified':len(counts),'records':sum(counts.values()),'checkpoints_verified':True,'send_states_verified':True}))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('source',type=Path)
    p.add_argument('--report',type=Path,default=Path('backups/migration-report.json'))
    p.add_argument('--enable-drafts',action='store_true')
    a=p.parse_args()
    migrate(a.source,a.report,a.enable_drafts)
