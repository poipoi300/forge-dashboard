"""Windows Job Objects through ctypes, so a process tree can be owned and killed as one.

A process assigned to a job with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE stays a
member for life, and so does everything it spawns afterwards. Children are
created suspended and resumed only after assignment, so nothing escapes the
job between CreateProcess and AssignProcessToJobObject. `TerminateJobObject`
then kills every member in one kernel call, and closing the last handle kills
them too, which is what makes a crashed dashboard leave no orphans.
"""

from __future__ import annotations

import sys

# Lets mypy skip this module when checking for other platforms.
assert sys.platform == "win32", "winjob is Windows-only"

import ctypes  # noqa: E402 - imported after the platform guard on purpose
from ctypes import wintypes  # noqa: E402

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_MAX_LISTED_PIDS = 4096


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = (
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_void_p),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    )


class _IoCounters(ctypes.Structure):
    _fields_ = tuple(
        (name, ctypes.c_uint64)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    )


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = (
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    )


class _ProcessIdList(ctypes.Structure):
    _fields_ = (
        ("NumberOfAssignedProcesses", wintypes.DWORD),
        ("NumberOfProcessIdsInList", wintypes.DWORD),
        ("ProcessIdList", ctypes.c_size_t * _MAX_LISTED_PIDS),
    )


def _kernel32() -> ctypes.WinDLL:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    kernel32.QueryInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    return kernel32


class JobObject:
    """A kill-on-close Job Object that owns one process tree."""

    def __init__(self) -> None:
        self._kernel32 = _kernel32()
        handle = self._kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = _ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
            handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            self._kernel32.CloseHandle(handle)
            raise error
        self._handle: int | None = handle

    def assign(self, pid: int) -> None:
        """Put `pid` (created suspended) into the job."""
        process = self._kernel32.OpenProcess(_PROCESS_TERMINATE | _PROCESS_SET_QUOTA, False, pid)
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self._kernel32.AssignProcessToJobObject(self._require_handle(), process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self._kernel32.CloseHandle(process)

    def pids(self) -> set[int]:
        """Every live process in the job, including re-parented grandchildren."""
        listing = _ProcessIdList()
        if not self._kernel32.QueryInformationJobObject(
            self._require_handle(),
            _JOB_OBJECT_BASIC_PROCESS_ID_LIST,
            ctypes.byref(listing),
            ctypes.sizeof(listing),
            None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return {int(listing.ProcessIdList[index]) for index in range(listing.NumberOfProcessIdsInList)}

    def terminate(self, exit_code: int = 1) -> None:
        """Kill every process in the job at once."""
        if self._handle is not None and not self._kernel32.TerminateJobObject(self._handle, exit_code):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        """Release the handle; with kill-on-close this also kills any survivors."""
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None

    def _require_handle(self) -> int:
        if self._handle is None:
            raise RuntimeError("job object already closed")
        return self._handle
