"""Start the combined EmailOutreach / ViberOutreach localhost app."""

import argparse
import os
from pathlib import Path
import sys

from app.web_server import LocalServer
from app.web_service import WebService


def lock_database(path):
    """Do not interrupt another server's jobs when a second instance starts."""
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
    parser.add_argument("--db", default=os.environ.get("VIBER_CLI_DB", str(Path(__file__).resolve().parent / "leads.sqlite3")))
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.email_port <= 65535:
        parser.error("Ports must be between 1 and 65535.")
    if args.port == args.email_port:
        parser.error("Choose a different port from the existing email service.")
    try:
        database_lock = lock_database(args.db)
    except ValueError as exc:
        parser.exit(1, f"{exc}\n")
    service = WebService(args.db)
    try:
        server = LocalServer(args.port, args.email_port, service)
    except OSError as exc:
        service.close()
        parser.exit(1, f"Could not start localhost app: {exc}\n")
    # Start only after binding the port, so a failed launch never runs saved jobs.
    service.start_scheduler()
    service.watcher.start()
    print(f"Outreach app: http://127.0.0.1:{args.port}/", flush=True)
    print("Click the title to switch between EmailOutreach and ViberOutreach.", flush=True)
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
