#!/usr/bin/env python3
"""Windows job-object guard: children die with the parent, and with free RAM.

`guard()` returns a JobGuard. Pass `creationflags=g.creationflags` to
`subprocess.run`/`Popen` and call `g.adopt(proc)`; every such child is put in a
job object created with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, so when this
process exits -- cleanly, killed, or crashed -- the OS terminates them too.

A background thread polls available physical memory and closes the job if it
falls below `min_free_gb`. Vivado grows for minutes before it fails, so waiting
for a MemoryError means the whole batch dies with it.

No-ops on non-Windows.
"""

import ctypes
import ctypes.wintypes as wt
import struct
import subprocess
import sys
import threading

_IS_WIN = sys.platform == "win32"

JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
CREATE_SUSPENDED = 0x00000004
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wt.DWORD),
        ("dwMemoryLoad", wt.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def avail_gb():
    """Free physical memory in GiB, or a large number off Windows."""
    if not _IS_WIN:
        return 1e9
    m = _MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(m)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return m.ullAvailPhys / (1 << 30)


class JobGuard:
    # Each driver watches GLOBAL memory but kills only ITS OWN children, so one
    # dip makes every driver panic: at 24 GB free it cost two probes mid-place.
    def __init__(self, min_free_gb=6.0, poll_s=5.0, sustain=6):
        self.min_free_gb = min_free_gb
        self.poll_s = poll_s
        self.sustain = sustain
        self.tripped = False
        self._job = None
        self._stop = threading.Event()
        if not _IS_WIN:
            return
        k32 = ctypes.windll.kernel32
        self._job = k32.CreateJobObjectW(None, None)
        info = (ctypes.c_byte * 144)()
        # LimitFlags is at offset 16 inside BASIC_LIMIT_INFORMATION.
        ctypes.memset(info, 0, 144)
        struct.pack_into("<I", info, 16, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
        k32.SetInformationJobObject(
            self._job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), 144
        )
        t = threading.Thread(target=self._watch, daemon=True)
        t.start()

    @property
    def creationflags(self):
        return CREATE_SUSPENDED if _IS_WIN else 0

    def adopt(self, proc):
        """Put `proc` in the job, then let it run. Safe to call repeatedly."""
        if not _IS_WIN or self._job is None:
            return
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, proc.pid)
        if h:
            k32.AssignProcessToJobObject(self._job, h)
            k32.CloseHandle(h)

    def _watch(self):
        low = 0
        while not self._stop.wait(self.poll_s):
            if avail_gb() >= self.min_free_gb:
                low = 0
                continue
            # Placement peaks are transient; only a breach that PERSISTS is an
            # emergency worth throwing away hours of work for.
            low += 1
            if low < self.sustain:
                continue
            self.tripped = True
            sys.stderr.write(
                f"jobguard: {avail_gb():.1f} GiB free stayed under "
                f"{self.min_free_gb} for {self.sustain * self.poll_s:.0f}s "
                f"-- killing the batch\n"
            )
            self.close()
            return

    def close(self):
        self._stop.set()
        if _IS_WIN and self._job is not None:
            ctypes.windll.kernel32.CloseHandle(self._job)
            self._job = None


def resume(proc):
    """Resume a CREATE_SUSPENDED process. No-op off Windows."""
    if not _IS_WIN:
        return
    k32 = ctypes.windll.kernel32
    THREAD_SUSPEND_RESUME = 0x0002
    # subprocess does not expose the thread handle, so walk the snapshot.
    TH32CS_SNAPTHREAD = 0x00000004

    class THREADENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD),
            ("cntUsage", wt.DWORD),
            ("th32ThreadID", wt.DWORD),
            ("th32OwnerProcessID", wt.DWORD),
            ("tpBasePri", ctypes.c_long),
            ("tpDeltaPri", ctypes.c_long),
            ("dwFlags", wt.DWORD),
        ]

    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    te = THREADENTRY32()
    te.dwSize = ctypes.sizeof(te)
    ok = k32.Thread32First(snap, ctypes.byref(te))
    while ok:
        if te.th32OwnerProcessID == proc.pid:
            th = k32.OpenThread(THREAD_SUSPEND_RESUME, False, te.th32ThreadID)
            if th:
                k32.ResumeThread(th)
                k32.CloseHandle(th)
        ok = k32.Thread32Next(snap, ctypes.byref(te))
    k32.CloseHandle(snap)


def run_guarded(guard, cmd, **kw):
    """subprocess.run, but the child joins the job before it executes."""
    kw.pop("creationflags", None)
    p = subprocess.Popen(cmd, creationflags=guard.creationflags, **kw)
    guard.adopt(p)
    resume(p)
    out, err = p.communicate()
    return p.returncode, out, err
