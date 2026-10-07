"""ADB Contacts Provider adapter. Fails closed when shell access is denied."""

import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import time
import uuid


class AndroidContactError(RuntimeError):
    pass


def _find_adb() -> str:
    configured = os.environ.get("VIBER_CLI_ADB")
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return str(candidate)
        raise AndroidContactError(f"VIBER_CLI_ADB does not point to a file: {configured}")

    on_path = shutil.which("adb")
    if on_path:
        return on_path

    roots = [Path(r"C:\adb\platform-tools"), Path(r"C:\platform-tools")]
    for variable in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(variable):
            roots.append(Path(os.environ[variable]) / "platform-tools")
    if os.environ.get("LOCALAPPDATA"):
        roots.append(Path(os.environ["LOCALAPPDATA"]) / "Android" / "Sdk" / "platform-tools")
    for root in roots:
        candidate = root / "adb.exe"
        if candidate.is_file():
            return str(candidate)
    raise AndroidContactError(
        "ADB was not found. Install Android Platform Tools or set VIBER_CLI_ADB "
        "to the full path of adb.exe.")


def _run_adb(adb: str, serial: str | None, args: list[str]) -> str:
    command = [adb]
    if serial:
        command.extend(["-s", serial])
    command.extend(args)
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=25)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AndroidContactError(f"ADB failed: {exc}") from exc
    output = (result.stdout + "\n" + result.stderr).strip()
    if result.returncode or re.search(r"(?:Permission Denial|SecurityException|Exception|Error while|not found)", output, re.I):
        raise AndroidContactError(f"ADB command failed: {output or result.returncode}")
    return output


def _one_device(adb: str) -> str:
    output = _run_adb(adb, None, ["devices", "-l"])
    devices = []
    unusable = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] in {
            "device", "offline", "unauthorized", "recovery", "sideload", "bootloader"
        }:
            (devices if parts[1] == "device" else unusable).append(parts[0])
    if len(devices) != 1 or unusable:
        raise AndroidContactError(
            f"Expected exactly one authorized Android device; found {len(devices)} usable"
            + (f" and {len(unusable)} unauthorized/offline" if unusable else "") + ".")
    return devices[0]


def _shell(adb: str, serial: str, words: list[str]) -> str:
    # A single POSIX-quoted command protects contact names from remote shell parsing.
    return _run_adb(adb, serial, ["shell", shlex.join(words)])


def _raw_id_for_marker(adb: str, serial: str, marker: str) -> int | None:
    output = _shell(adb, serial, [
        "content", "query", "--uri", "content://com.android.contacts/raw_contacts",
        "--projection", "_id:sync1", "--where", f"sync1='{marker}'"])
    rows = [line for line in output.splitlines() if line.startswith("Row:")]
    if not rows:
        return None
    if len(rows) != 1:
        raise AndroidContactError("Android returned multiple raw contacts for one marker.")
    match = re.fullmatch(r"Row:\s*\d+\s+_id=(\d+),\s*sync1=" + re.escape(marker), rows[0])
    if not match:
        raise AndroidContactError("Android returned an unexpected raw-contact query result.")
    return int(match.group(1))


def _has_data_row(rows: list[str], mime: str, value: str, raw_id: int) -> bool:
    for row in rows:
        match = re.fullmatch(
            r"\s*Row:\s*\d+\s+mimetype=(.*?),\s*data1=(.*),\s*raw_contact_id=(\d+)\s*",
            row)
        if match and (match.group(1), match.group(2), int(match.group(3))) == (mime, value, raw_id):
            return True
    return False


def add_android_contact(name: str, phone: str) -> None:
    """Insert name and number via ADB and read both rows back before success.

    Some Android builds deny Contacts Provider access to the ADB shell. This
    function reports that failure; it never treats opening an edit screen as
    proof that a contact was saved.
    """
    adb = _find_adb()
    serial = _one_device(adb)
    marker = "viber_cli_" + uuid.uuid4().hex
    if _raw_id_for_marker(adb, serial, marker) is not None:
        raise AndroidContactError("A contact with the generated marker already exists.")
    raw_id = None
    try:
        # Android's `content insert` normally prints nothing, even on success.
        _shell(adb, serial, [
            "content", "insert", "--uri", "content://com.android.contacts/raw_contacts",
            "--bind", "account_type:s:", "--bind", "account_name:s:",
            "--bind", f"sync1:s:{marker}"])
        for _ in range(5):
            raw_id = _raw_id_for_marker(adb, serial, marker)
            if raw_id is not None:
                break
            time.sleep(.2)
        if raw_id is None:
            raise AndroidContactError(
                "Android did not expose the inserted raw contact; "
                f"an empty contact with marker {marker} may remain.")
        for mime, value in (("vnd.android.cursor.item/name", name),
                            ("vnd.android.cursor.item/phone_v2", phone)):
            _shell(adb, serial, [
                "content", "insert", "--uri", "content://com.android.contacts/data",
                "--bind", f"raw_contact_id:i:{raw_id}",
                "--bind", f"mimetype:s:{mime}", "--bind", f"data1:s:{value}"])
        output = _shell(adb, serial, [
            "content", "query", "--uri", "content://com.android.contacts/data",
            "--projection", "mimetype:data1:raw_contact_id",
            "--where", f"raw_contact_id={raw_id}"])
        rows = [line for line in output.splitlines() if line.lstrip().startswith("Row:")]
        if not _has_data_row(rows, "vnd.android.cursor.item/name", name, raw_id):
            raise AndroidContactError("Android contact name could not be verified.")
        if not _has_data_row(rows, "vnd.android.cursor.item/phone_v2", phone, raw_id):
            raise AndroidContactError("Android contact phone could not be verified.")
        if _raw_id_for_marker(adb, serial, marker) != raw_id:
            raise AndroidContactError("Android raw contact disappeared before verification.")
    except Exception as exc:
        if raw_id is not None:
            try:
                _shell(adb, serial, ["content", "delete", "--uri",
                     f"content://com.android.contacts/raw_contacts/{raw_id}"])
                if _raw_id_for_marker(adb, serial, marker) is not None:
                    raise AndroidContactError("Android still exposes the contact after cleanup.")
            except AndroidContactError as cleanup_exc:
                raise AndroidContactError(
                    f"Contact creation failed; cleanup of raw contact {raw_id} also failed: "
                    f"{cleanup_exc}") from exc
        if isinstance(exc, AndroidContactError):
            raise
        raise AndroidContactError(str(exc)) from exc
