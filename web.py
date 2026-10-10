"""Start the combined EmailOutreach / ViberOutreach localhost app."""

import argparse
import os
from pathlib import Path
import sys

from app.web_server import LocalServer
from app.web_service import WebService
from app.vm_bridge import load_vm_config, VmBridge, VmViberClient, VmWorkerSource
from app.storage import postgres
from app.runtime import settings


def lock_database(path):
    """Do not interrupt another server's jobs when a second instance starts."""
    if postgres(path):
        import psycopg
        lock = psycopg.connect(path,autocommit=True)
        if not lock.execute("SELECT pg_try_advisory_lock(hashtext('viber-controller-lifetime'))").fetchone()[0]:
            lock.close()
            raise ValueError('The previous controller is still running; worker replacement is blocked.')
        return lock
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(str(path) + ".web.lock", "a+b")
    if not lock.tell():
        lock.write(b"0")
        lock.flush()
    lock.seek(0)
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        raise ValueError("Another localhost app is already using this contact database.") from None
    return lock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=4001)
    parser.add_argument("--email-port", type=int, default=4000)
    parser.add_argument("--db", default=os.environ.get("VIBER_CLI_DB", settings().get('VIBER_DATABASE_URL',str(Path(__file__).resolve().parent / "leads.sqlite3"))))
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.email_port <= 65535:
        parser.error("Ports must be between 1 and 65535.")
    if args.port == args.email_port:
        parser.error("Choose a different port from the existing email service.")
    try:
        database_lock = lock_database(args.db)
    except ValueError as exc:
        parser.exit(1, f"{exc}\n")
    vm_config = load_vm_config()
    if postgres(args.db):
        # Bind before constructing executors or activating any persisted jobs.
        import socket
        import uvicorn
        from app.controller import create_app
        manifest_path = Path(__file__).resolve().parent / 'data' / 'instances.json'
        if not vm_config and not manifest_path.is_file():
            parser.exit(1,'The managed controller requires the configured current ViberWorker VM.\n')
        sock = socket.socket()
        sock.bind(('127.0.0.1',args.port))
        if manifest_path.is_file():
            from app.instances import Instances
            service = Instances(args.db,manifest_path)
        else:
            bridge = VmBridge(vm_config)
            service = WebService(args.db,client_factory=lambda:VmViberClient(bridge),source_factory=lambda:VmWorkerSource(bridge))
        controller=uvicorn.Server(uvicorn.Config(create_app(service,args.port,args.email_port),host='127.0.0.1',port=args.port,access_log=False,log_level='warning'))
        service.request_shutdown=lambda:setattr(controller,'should_exit',True)
        try:
            controller.run(sockets=[sock])
        finally:
            sock.close()
            database_lock.close()
        return
    if vm_config:
        bridge = VmBridge(vm_config)
        service = WebService(args.db,
                             client_factory=lambda: VmViberClient(bridge),
                             source_factory=lambda: VmWorkerSource(bridge))
    else:
        service = WebService(args.db)
    try:
        server = LocalServer(args.port, args.email_port, service)
    except OSError as exc:
        service.close()
        parser.exit(1, f"Could not start localhost app: {exc}\n")
    # Start only after binding the port, so a failed launch never runs saved jobs.
    service.start_scheduler()
    service.watcher.start()
    service.replies.start()
    print(f"Outreach app: http://127.0.0.1:{args.port}/", flush=True)
    print("Click the title to switch between EmailOutreach and ViberOutreach.", flush=True)
    if vm_config:
        print("Viber runs in the dedicated ViberWorker virtual machine.", flush=True)
    else:
        print("Keep the email service running. Keep Viber restored behind your other windows.", flush=True)
    try:
        server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt:
        print("\nFinishing any active desktop operation before stopping.", flush=True)
    finally:
        server.server_close()
        database_lock.close()


if __name__ == "__main__":
    main()
