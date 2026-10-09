"""Read-only discovery of this user's Viber database key in its own process.

Only SQL hexkey strings are collected. Nothing is injected, written, suspended,
logged, or saved; callers must validate every candidate against the database.
"""

import ctypes
from ctypes import wintypes
from pathlib import Path
import re
import sys
import time


class KeyDiscoveryError(RuntimeError):
    pass


_ASCII = re.compile(rb"PRAGMA\s+hexkey\s*=\s*'([0-9a-fA-F]{16,512})'", re.I)
_UTF16 = re.compile(
    rb"P\x00R\x00A\x00G\x00M\x00A\x00(?:\s\x00)+"
    rb"h\x00e\x00x\x00k\x00e\x00y\x00(?:\s\x00)*=\x00"
    rb"(?:\s\x00)*'\x00((?:[0-9a-fA-F]\x00){16,512})'\x00", re.I)


def keys_in_chunk(data):
    """Extract only well-formed key literals, never arbitrary message text."""
    result = {m[1].decode('ascii').lower() for m in _ASCII.finditer(data)}
    result.update(m[1].decode('utf-16-le').lower() for m in _UTF16.finditer(data))
    return {key for key in result if len(key) % 2 == 0}


def find_viber_process(executable):
    if sys.platform != 'win32' or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise KeyDiscoveryError('Database key discovery needs 64-bit Windows Python.')
    import psutil
    expected = Path(executable).resolve()
    current_user = psutil.Process().username().casefold()
    processes = []
    for process in psutil.process_iter(['pid', 'name', 'exe', 'username']):
        try:
            if (process.info['name'] or '').lower() == 'viber.exe' and process.info['exe']:
                if ((process.info['username'] or '').casefold() == current_user
                        and Path(process.info['exe']).resolve() == expected):
                    processes.append(process)
        except (psutil.Error, OSError):
            continue
    if len(processes) != 1:
        raise KeyDiscoveryError('Start exactly one Viber Desktop instance for this Windows user.')
    return processes[0]


def discover_keys(executable, *, timeout=15, max_bytes=768 * 1024 * 1024):
    return tuple(iter_keys(executable, timeout=timeout, max_bytes=max_bytes))


def iter_keys(executable, *, timeout=15, max_bytes=768 * 1024 * 1024):
    """Yield candidates immediately, allowing validation to stop the scan."""
    process = find_viber_process(executable)

    class MemoryInfo(ctypes.Structure):
        _fields_ = [('BaseAddress', ctypes.c_void_p), ('AllocationBase', ctypes.c_void_p),
                    ('AllocationProtect', wintypes.DWORD), ('PartitionId', wintypes.WORD),
                    ('RegionSize', ctypes.c_size_t), ('State', wintypes.DWORD),
                    ('Protect', wintypes.DWORD), ('Type', wintypes.DWORD)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                     ctypes.POINTER(MemoryInfo), ctypes.c_size_t]
    kernel.VirtualQueryEx.restype = ctypes.c_size_t
    kernel.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                        ctypes.c_void_p, ctypes.c_size_t,
                                        ctypes.POINTER(ctypes.c_size_t)]
    kernel.ReadProcessMemory.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x0400 | 0x0010, False, process.pid)
    if not handle:
        raise KeyDiscoveryError('Windows denied read access to your Viber process.')
    found, address, total = set(), 0, 0
    deadline = time.monotonic() + timeout
    try:
        info = MemoryInfo()
        while time.monotonic() < deadline and total < max_bytes:
            if not kernel.VirtualQueryEx(handle, address, ctypes.byref(info), ctypes.sizeof(info)):
                break
            base = info.BaseAddress or 0
            end = base + info.RegionSize
            if end <= address:
                break
            # Committed private readable pages only; skip executable images,
            # guarded pages and no-access mappings. Never request write rights.
            if info.State == 0x1000 and info.Type == 0x20000 and not info.Protect & 0x101:
                offset, tail = 0, b''
                while offset < info.RegionSize and time.monotonic() < deadline and total < max_bytes:
                    size = min(1024 * 1024, info.RegionSize - offset, max_bytes - total)
                    buffer = ctypes.create_string_buffer(size)
                    read = ctypes.c_size_t()
                    ok = kernel.ReadProcessMemory(handle, base + offset, buffer, size, ctypes.byref(read))
                    if ok and read.value:
                        chunk = tail + buffer.raw[:read.value]
                        for key in sorted(keys_in_chunk(chunk) - found):
                            found.add(key)
                            if len(found) > 32:
                                raise KeyDiscoveryError('Too many database key candidates; refusing ambiguous discovery.')
                            yield key
                        tail = chunk[-2048:]
                    else:
                        tail = b''
                    total += size
                    offset += size
            address = end
        if not found:
            raise KeyDiscoveryError('No database key was available in Viber memory. Restart Viber and retry.')
    finally:
        kernel.CloseHandle(handle)
