"""Two local VM slots, activated only after their new native number is verified."""
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from types import SimpleNamespace

from app.storage import connect
from app.vm_bridge import load_vm_config, VmBridge, VmViberClient, VmWorkerSource
from app.web_service import WebService


class Instances:
    multi_account = True

    def __init__(self, target, manifest_path):
        self.target = target
        self.manifest_path = Path(manifest_path)
        manifest = json.loads(self.manifest_path.read_text(encoding='utf-8-sig'))
        slots = manifest.get('instances', [])
        if len(slots) != 2 or {s['id'] for s in slots} != {'instance-1','instance-2'}:
            raise ValueError('Expected the two configured replacement instances.')
        self.slots = {s['id']: dict(s) for s in slots}
        self.services = {}
        self.lock = threading.RLock()
        self.pending = SimpleNamespace(lock=self.lock)
        self.stop = threading.Event()
        self.generation_limit = threading.BoundedSemaphore(2)
        self.thread = None
        self.request_shutdown = None

    def selected(self, account_id=None):
        account_id = account_id or 'instance-1'
        with self.lock:
            if account_id not in self.slots:
                raise PermissionError('Choose one of the two new Viber instances.')
            return self.services.get(account_id)

    def status(self):
        with self.lock:
            return {'instances': [
                {key: slot.get(key) for key in ('id','label','vm_name','state','phone','error')}
                for slot in self.slots.values()], 'old_account': 'DISABLED',
                'old_contacts': 'IGNORED',
                'review_required': any(service.replies.settings()['review_required'] for service in self.services.values()),
                'manual_review_required': True}

    def start(self):
        self.thread = threading.Thread(target=self._loop,name='viber-instance-registration',daemon=True)
        self.thread.start()

    def _activate(self, slot):
        bridge = VmBridge(load_vm_config(slot['bridge_config']))
        health = bridge.call('/health', timeout=3)
        if health.get('instance_id') != slot['id']:
            raise PermissionError('The VM agent identity does not match this slot.')
        if health.get('status') != 'ready':
            with self.lock:
                slot.update(state='BLOCKED' if health.get('status') == 'error' else 'AWAITING_NUMBER', error=health.get('error'))
            return
        # Account binding requires a complete native identity. Preserve owned
        # conversations and the saved incoming cutoff during restarts: an
        # empty activation snapshot would deactivate them and baseline replies
        # that arrived while offline. Brand-new accounts start from now.
        with closing(connect(self.target)) as db:
            account = db.execute('SELECT monitoring_since_ms FROM accounts WHERE id=?',(slot['id'],)).fetchone()
            phones = sorted(r[0] for r in db.execute('SELECT phone FROM contact_owners WHERE account_id=?',(slot['id'],)))
        since_ms = account[0] if account else int(datetime.now(timezone.utc).timestamp()*1000)
        worker_source = VmWorkerSource(bridge,slot.get('native_source_id'),slot.get('source_id'))
        snapshot = worker_source.read({'phones': phones, 'checkpoints': {},
            'include_new_senders': True,
            'incoming_since_ms': since_ms, 'force_refresh': True}, timeout=15)
        service = WebService(self.target, client_factory=lambda: VmViberClient(bridge),
                             source_factory=lambda: VmWorkerSource(bridge,slot.get('native_source_id'),slot.get('source_id')), account_id=slot['id'],
                             identity_snapshot=snapshot, generation_limit=self.generation_limit)
        try:
            service.accounts.source(snapshot)
            service.watcher.inbox.ingest(snapshot)
            service.request_shutdown = self.request_shutdown
            service.start_scheduler()
            service.watcher.start()
            service.replies.start()
        except BaseException:
            service.close()
            raise
        with self.lock:
            self.services[slot['id']] = service
            slot.update(state='READY', phone=snapshot['account_phone'], error=None)

    def _loop(self):
        while not self.stop.is_set():
            for slot in self.slots.values():
                if self.stop.is_set():
                    break
                if slot['id'] in self.services:
                    continue
                try:
                    self._activate(slot)
                except Exception as exc:
                    with self.lock:
                        slot.update(state='AWAITING_NUMBER' if not isinstance(exc,PermissionError) else 'BLOCKED', error=str(exc))
            self.stop.wait(3)

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=45)
            if self.thread.is_alive():
                raise RuntimeError('Instance registration is still active; replacement is blocked.')
        for service in list(self.services.values()):
            service.close()


class AccountServer:
    """Request-local service selection with one shared browser session ledger."""
    def __init__(self, server, service):
        self.base = server
        self.service = service
        self.email_port = server.email_port
        self.server_address = server.server_address
        self.session_lock = server.session_lock

    @property
    def sessions(self):
        return self.base.sessions

    @sessions.setter
    def sessions(self, value):
        self.base.sessions = value
