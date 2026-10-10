"""Background database polling, isolated from the desktop send worker."""

import json
from pathlib import Path
import queue
import subprocess
import sys
import threading

from app.viber_database import DatabaseReadError
from app.viber_inbox import InboxStore, utc_now


class WorkerSource:
    def __init__(self, database_key=None):
        self.database_key = database_key
        self.process = subprocess.Popen(
            [sys.executable, '-u', '-m', 'app.viber_source_worker'],
            cwd=Path(__file__).resolve().parent.parent,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding='utf-8',
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
        self.responses = queue.Queue()
        self.reader = threading.Thread(target=self._receive, name='viber-db-pipe', daemon=True)
        self.reader.start()

    def _receive(self):
        try:
            for line in self.process.stdout:
                try:
                    response = json.loads(line)
                except (ValueError, TypeError):
                    response = {'ok': False, 'error': 'Viber database worker returned an invalid response.'}
                self.responses.put(response)
        finally:
            self.responses.put({'ok': False, 'error': 'Viber database worker stopped.'})

    def read(self, request):
        try:
            payload = dict(request)
            if self.database_key is not None:
                payload['_database_key'] = self.database_key
            self.process.stdin.write(json.dumps(payload) + '\n')
            self.process.stdin.flush()
            response = self.responses.get(timeout=40)
        except (BrokenPipeError, OSError, queue.Empty):
            raise DatabaseReadError('Viber database worker is unavailable. Detection is paused.') from None
        if not response.get('ok'):
            raise DatabaseReadError(response.get('error', 'Viber database read failed.'))
        self.database_key = None
        return response['result']

    def close(self):
        if self.process.poll() is None:
            # This is our private reader, never the user's Viber process.
            self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.reader.join(timeout=2)
        for pipe in (self.process.stdin, self.process.stdout):
            pipe.close()


class DatabaseWatcher:
    def __init__(self, lead_store, source_factory=WorkerSource, interval=1):
        self.leads = lead_store
        self.inbox = InboxStore(lead_store.path)
        self.source_factory = source_factory
        self.interval = interval
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.poll_lock = threading.Lock()
        self.thread = None
        self.source = None
        self.state = 'STOPPED'
        self.error = None
        self.last_attempt = None
        self.accounts = None
        self.on_ingest = None

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.stop_event.clear()
            self.state, self.error = 'CONNECTING', None
            self.thread = threading.Thread(target=self._loop, name='viber-database', daemon=True)
            self.thread.start()

    def status(self):
        with self.lock:
            return {**self.inbox.status(), 'state': self.state, 'error': self.error,
                    'last_attempt': self.last_attempt, 'poll_seconds': self.interval}

    def poll_once(self):
        # The sender also requests a fresh read immediately before dispatch.
        with self.poll_lock:
            return self._poll_once()

    def _poll_once(self):
        with self.lock:
            self.last_attempt = utc_now()
        try:
            if self.source is None:
                self.source = self.source_factory()
            # A watcher poll is a freshness boundary, including the checks
            # before sending. Do not let another bridge caller's cache replace it.
            request = {'phones': [lead.phone for lead in self.leads.all()],
                       'checkpoints': self.inbox.checkpoints(), 'force_refresh': True}
            if self.accounts:
                request['phones']=sorted(self.accounts.phones())
                current=next(a for a in self.accounts.list() if a['id']==self.accounts.account_id)
                request.update(include_new_senders=True,incoming_since_ms=current['monitoring_since_ms'])
            snapshot = self.source.read(request)
            if self.accounts:
                self.accounts.source(snapshot)
            counts = self.inbox.ingest(snapshot)
            with self.lock:
                self.state, self.error = 'WATCHING', None
            if self.on_ingest:
                self.on_ingest()
            return counts
        except Exception as exc:
            if self.source:
                try:
                    self.source.close()
                except Exception:
                    # Retain the old handle if shutdown could not be confirmed;
                    # never create a replacement reader alongside it.
                    pass
                else:
                    self.source = None
            with self.lock:
                self.state = 'BLOCKED'
                self.error = str(exc) if isinstance(exc, DatabaseReadError) else 'Message detection failed. No reply was sent.'
            return None

    def _loop(self):
        failures = 0
        while not self.stop_event.is_set():
            result = self.poll_once()
            failures = 0 if result is not None else failures+1
            if self.stop_event.wait(self.interval if result is not None else min(15,2**min(failures,4))):
                break
        if self.source:
            self.source.close()
            self.source = None
        with self.lock:
            self.state = 'STOPPED'

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=45)
        elif self.source:
            self.source.close()
            self.source = None
        with self.lock:
            self.state = 'STOPPED'
