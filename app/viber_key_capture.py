"""Capture Viber's database key at startup inside the dedicated worker VM."""

from pathlib import Path
import re
import subprocess
import threading
import time


class KeyCaptureError(RuntimeError):
    pass


_SCRIPT = r'''
const symbols = [
  "?exec@QSqlQuery@@QEAA_NAEBVQString@@@Z",
  "?exec@QSqlDatabase@@QEBA?AVQSqlQuery@@AEBVQString@@@Z"
];
let installed = false;
function inspectSql(args) {
  try {
    const value = args[1];
    const data = value.add(Process.pointerSize).readPointer();
    const length = value.add(Process.pointerSize * 2).readS64().toNumber();
    if (length < 16 || length > 600 || data.isNull()) return;
    const sql = data.readUtf16String(length);
    const match = /^\s*PRAGMA\s+hexkey\s*=\s*'([0-9a-f]{16,512})'/i.exec(sql);
    if (match) send({type: "database-key", key: match[1].toLowerCase()});
  } catch (_) {}
}
Process.attachModuleObserver({
  onAdded(module) {
    if (installed || module.name.toLowerCase() !== "qt6sql.dll") return;
    installed = true;
    let count = 0;
    for (const symbol of symbols) {
      const target = module.findExportByName(symbol);
      if (target !== null) {
        Interceptor.attach(target, {onEnter: inspectSql});
        count++;
      }
    }
    send({type: "hooks-ready", count});
  }
});
'''


def valid_key(value):
    return (isinstance(value, str) and len(value) % 2 == 0
            and re.fullmatch(r'[0-9a-f]{16,512}', value) is not None)


def start_viber_and_capture_key(executable, *, timeout=45):
    """Start Viber suspended, capture its SQL key, and leave Viber running."""
    path = Path(executable).resolve()
    if not path.is_file():
        raise KeyCaptureError('Viber Desktop is not installed in the worker VM.')
    try:
        import frida
    except ImportError:
        raise KeyCaptureError('Install requirements-vm.txt in the worker VM.') from None

    device = frida.get_local_device()
    key = None
    hooks_ready = False
    event = threading.Event()

    def receive(message, _data):
        nonlocal key, hooks_ready
        payload = message.get('payload') if message.get('type') == 'send' else None
        if not isinstance(payload, dict):
            return
        if payload.get('type') == 'hooks-ready':
            hooks_ready = bool(payload.get('count'))
        elif payload.get('type') == 'database-key' and valid_key(payload.get('key')):
            key = payload['key']
            event.set()

    pid = None
    session = None
    keep_session = False
    try:
        pid = device.spawn([str(path)])
        session = device.attach(pid)
        script = session.create_script(_SCRIPT)
        script.on('message', receive)
        script.load()
        device.resume(pid)
        if not event.wait(timeout):
            detail = 'Qt SQL hooks were unavailable.' if not hooks_ready else 'Viber did not open its message database.'
            raise KeyCaptureError(f'Database key capture timed out. {detail}')
        keep_session = True
    except KeyCaptureError:
        if pid is not None:
            try:
                device.kill(pid)
            except Exception:
                pass
        raise
    except Exception:
        if pid is not None:
            try:
                device.kill(pid)
            except Exception:
                pass
        raise KeyCaptureError('Viber could not be started under the VM key capture worker.') from None
    finally:
        if session is not None and not keep_session:
            try:
                session.detach()
            except Exception:
                pass

    # Some Viber builds replace or exit their bootstrap process. The key is
    # already captured, so start the normal desktop process if none survived.
    import psutil
    expected = path
    current_user = psutil.Process().username().casefold()

    def matching_processes():
        matches = []
        for process in psutil.process_iter(['name', 'exe', 'username']):
            try:
                if ((process.info['name'] or '').lower() == 'viber.exe'
                        and (process.info['username'] or '').casefold() == current_user
                        and process.info['exe']
                        and Path(process.info['exe']).resolve() == expected):
                    matches.append(process)
            except (psutil.Error, OSError):
                continue
        return matches

    deadline = time.monotonic() + 15
    launched = False
    while time.monotonic() < deadline:
        matches = matching_processes()
        if len(matches) == 1:
            return key, session
        if not matches and not launched:
            subprocess.Popen([str(path)], close_fds=True)
            launched = True
        time.sleep(.2)
    try:
        session.detach()
    except Exception:
        pass
    raise KeyCaptureError('Viber did not settle on one desktop process after key capture.')
