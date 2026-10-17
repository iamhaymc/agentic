"""Stdlib-only Windows AppContainer setup and isolated child launcher.

ACL/profile ownership stays in the host process; the launcher owns a job so
terminating it also terminates every confined descendant.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from ctypes import wintypes as W
from pathlib import Path
from typing import Any, Generator


def canonical_path(path: Path | str) -> Path:
    resolved = Path(path).resolve()
    value = str(resolved)
    if value.startswith("\\\\?\\UNC\\"):
        return Path("\\\\" + value[8:])
    if value.startswith("\\\\?\\") and len(value) > 5 and value[5] == ":":
        return Path(value[4:])
    return resolved


class Trustee(ctypes.Structure):
    _fields_ = [
        ("multiple", ctypes.c_void_p), ("operation", W.DWORD),
        ("form", W.DWORD), ("kind", W.DWORD), ("name", ctypes.c_void_p),
    ]


class Access(ctypes.Structure):
    _fields_ = [
        ("permissions", W.DWORD), ("mode", W.DWORD),
        ("inheritance", W.DWORD), ("trustee", Trustee),
    ]


class SecurityDescriptor(ctypes.Structure):
    _fields_ = [
        ("revision", W.BYTE), ("reserved", W.BYTE), ("control", W.WORD),
        ("owner", ctypes.c_void_p), ("group", ctypes.c_void_p),
        ("sacl", ctypes.c_void_p), ("dacl", ctypes.c_void_p),
    ]


class SidAttributes(ctypes.Structure):
    _fields_ = [("sid", ctypes.c_void_p), ("attributes", W.DWORD)]


class Capabilities(ctypes.Structure):
    _fields_ = [
        ("sid", ctypes.c_void_p), ("capabilities", ctypes.POINTER(SidAttributes)),
        ("count", W.DWORD), ("reserved", W.DWORD),
    ]


class Startup(ctypes.Structure):
    _fields_ = [
        ("cb", W.DWORD), ("reserved", W.LPWSTR), ("desktop", W.LPWSTR),
        ("title", W.LPWSTR), ("x", W.DWORD), ("y", W.DWORD),
        ("width", W.DWORD), ("height", W.DWORD), ("chars_x", W.DWORD),
        ("chars_y", W.DWORD), ("fill", W.DWORD), ("flags", W.DWORD),
        ("show", W.WORD), ("reserved_size", W.WORD), ("reserved_data", ctypes.c_void_p),
        ("stdin", W.HANDLE), ("stdout", W.HANDLE), ("stderr", W.HANDLE),
    ]


class StartupEx(ctypes.Structure):
    _fields_ = [("startup", Startup), ("attributes", ctypes.c_void_p)]


class ProcessInfo(ctypes.Structure):
    _fields_ = [
        ("process", W.HANDLE), ("thread", W.HANDLE),
        ("pid", W.DWORD), ("tid", W.DWORD),
    ]


class BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
        ("flags", W.DWORD), ("min_working_set", ctypes.c_size_t),
        ("max_working_set", ctypes.c_size_t), ("active_processes", W.DWORD),
        ("affinity", ctypes.c_size_t), ("priority", W.DWORD), ("scheduling", W.DWORD),
    ]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", BasicLimits), ("io", ctypes.c_uint64 * 6),
        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
    ]


class Windows:
    def __init__(self) -> None:
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.security = ctypes.WinDLL("advapi32", use_last_error=True)
        self.profiles = ctypes.WinDLL("userenv", use_last_error=True)
        self.shell = ctypes.WinDLL("shell32", use_last_error=True)
        pointer = ctypes.c_void_p
        out = ctypes.POINTER(pointer)

        def bind(dll: Any, name: str, result: Any, *args: Any) -> None:
            fn = getattr(dll, name)
            fn.restype, fn.argtypes = result, list(args)

        bind(self.kernel, "LocalFree", pointer, pointer)
        bind(self.kernel, "CloseHandle", W.BOOL, W.HANDLE)
        bind(self.kernel, "CreateMutexW", W.HANDLE, pointer, W.BOOL, W.LPCWSTR)
        bind(self.kernel, "ReleaseMutex", W.BOOL, W.HANDLE)
        bind(self.kernel, "GetStdHandle", W.HANDLE, W.DWORD)
        bind(self.kernel, "CreateFileW", W.HANDLE, W.LPCWSTR, W.DWORD,
             W.DWORD, pointer, W.DWORD, W.DWORD, W.HANDLE)
        bind(self.kernel, "CreateJobObjectW", W.HANDLE, pointer, W.LPCWSTR)
        bind(self.kernel, "SetInformationJobObject", W.BOOL, W.HANDLE, ctypes.c_int, pointer, W.DWORD)
        bind(self.kernel, "AssignProcessToJobObject", W.BOOL, W.HANDLE, W.HANDLE)
        bind(self.kernel, "TerminateProcess", W.BOOL, W.HANDLE, W.UINT)
        bind(self.kernel, "ResumeThread", W.DWORD, W.HANDLE)
        bind(self.kernel, "WaitForSingleObject", W.DWORD, W.HANDLE, W.DWORD)
        bind(self.kernel, "GetExitCodeProcess", W.BOOL, W.HANDLE, ctypes.POINTER(W.DWORD))
        bind(self.kernel, "InitializeProcThreadAttributeList", W.BOOL,
             pointer, W.DWORD, W.DWORD, ctypes.POINTER(ctypes.c_size_t))
        bind(self.kernel, "UpdateProcThreadAttribute", W.BOOL,
             pointer, W.DWORD, ctypes.c_size_t, pointer, ctypes.c_size_t, pointer, pointer)
        bind(self.kernel, "DeleteProcThreadAttributeList", None, pointer)
        bind(self.kernel, "CreateProcessW", W.BOOL,
             W.LPCWSTR, W.LPWSTR, pointer, pointer, W.BOOL, W.DWORD,
             pointer, W.LPCWSTR, ctypes.POINTER(StartupEx), ctypes.POINTER(ProcessInfo))
        bind(self.security, "ConvertSidToStringSidW", W.BOOL, pointer, out)
        bind(self.security, "ConvertStringSidToSidW", W.BOOL, W.LPCWSTR, out)
        bind(self.security, "FreeSid", pointer, pointer)
        bind(self.security, "GetNamedSecurityInfoW", W.DWORD,
             W.LPCWSTR, W.DWORD, W.DWORD, out, out, out, out, out)
        bind(self.security, "SetNamedSecurityInfoW", W.DWORD,
             W.LPCWSTR, W.DWORD, W.DWORD, pointer, pointer, pointer, pointer)
        bind(self.security, "SetEntriesInAclW", W.DWORD,
             W.ULONG, ctypes.POINTER(Access), pointer, out)
        bind(self.security, "InitializeSecurityDescriptor", W.BOOL, pointer, W.DWORD)
        bind(self.security, "SetSecurityDescriptorDacl", W.BOOL, pointer, W.BOOL, pointer, W.BOOL)
        bind(self.security, "SetFileSecurityW", W.BOOL, W.LPCWSTR, W.DWORD, pointer)
        bind(self.profiles, "CreateAppContainerProfile", ctypes.c_long,
             W.LPCWSTR, W.LPCWSTR, W.LPCWSTR, pointer, W.DWORD, out)
        bind(self.profiles, "DeleteAppContainerProfile", ctypes.c_long, W.LPCWSTR)
        bind(self.shell, "CommandLineToArgvW", ctypes.POINTER(W.LPWSTR),
             W.LPCWSTR, ctypes.POINTER(ctypes.c_int))

    @staticmethod
    def check(ok: Any, operation: str) -> None:
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error(), operation)

    @staticmethod
    def status(code: int, operation: str) -> None:
        if code:
            raise OSError(code, f"{operation}: {ctypes.FormatError(code)}")

    def acl(
        self, path: Path, sid: ctypes.c_void_p, mask: int, *,
        revoke: bool = False, inherit: bool = True, propagate: bool = True,
    ) -> None:
        key = hashlib.sha256(str(canonical_path(path)).casefold().encode()).hexdigest()
        mutex = self.kernel.CreateMutexW(None, False, "Local\\agentic.acl." + key)
        self.check(mutex, "create sandbox ACL mutex")
        acquired = False
        try:
            status = self.kernel.WaitForSingleObject(mutex, 30_000)
            if status == 0x102:
                raise TimeoutError(f"sandbox ACL lock timed out: {path}")
            if status not in (0, 0x80):
                self.check(False, f"acquire sandbox ACL mutex {path}")
            acquired = True
            if status == 0x80:
                print(f"[sandbox] recovered abandoned ACL mutex: {path}", file=sys.stderr)
            self._acl(path, sid, mask, revoke=revoke, inherit=inherit, propagate=propagate)
        finally:
            try:
                if acquired:
                    self.check(self.kernel.ReleaseMutex(mutex), "release sandbox ACL mutex")
            finally:
                self.check(self.kernel.CloseHandle(mutex), "close sandbox ACL mutex")

    def _acl(
        self, path: Path, sid: ctypes.c_void_p, mask: int, *,
        revoke: bool, inherit: bool, propagate: bool,
    ) -> None:
        old, descriptor, updated = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        self.status(self.security.GetNamedSecurityInfoW(
            str(path), 1, 4, None, None, ctypes.byref(old), None, ctypes.byref(descriptor),
        ), f"read sandbox ACL {path}")
        try:
            entry = Access(mask, 4 if revoke else 1, 3 if inherit and path.is_dir() else 0,
                           Trustee(None, 0, 0, 0, sid))
            self.status(self.security.SetEntriesInAclW(
                1, ctypes.byref(entry), old, ctypes.byref(updated),
            ), f"build sandbox ACL {path}")
            try:
                if propagate:
                    self.status(self.security.SetNamedSecurityInfoW(
                        str(path), 1, 4, None, None, updated, None,
                    ), f"apply sandbox ACL {path}")
                else:
                    # SetNamedSecurityInfo propagates even unrelated inherited
                    # entries. Ancestor traversal must never walk a host tree.
                    descriptor_out = SecurityDescriptor()
                    self.check(self.security.InitializeSecurityDescriptor(
                        ctypes.byref(descriptor_out), 1,
                    ), "initialize ACL descriptor")
                    self.check(self.security.SetSecurityDescriptorDacl(
                        ctypes.byref(descriptor_out), True, updated, False,
                    ), "set ACL descriptor")
                    self.check(self.security.SetFileSecurityW(
                        str(path), 4, ctypes.byref(descriptor_out),
                    ), f"apply traversal ACL {path}")
            finally:
                self.kernel.LocalFree(updated)
        finally:
            self.kernel.LocalFree(descriptor)

    def can_grant(self, path: Path) -> bool:
        handle = self.kernel.CreateFileW(str(path), 0x40000, 7, None, 3, 0x02000000, None)
        if handle == ctypes.c_void_p(-1).value:
            if ctypes.get_last_error() == 5:
                return False
            self.check(False, f"check ACL access {path}")
        self.check(self.kernel.CloseHandle(handle), f"close ACL access probe {path}")
        return True


def command_words(command: str) -> list[str]:
    if not command.strip():
        return []
    api = Windows()
    count = ctypes.c_int()
    words = api.shell.CommandLineToArgvW(command.strip(), ctypes.byref(count))
    api.check(words, "parse command arguments")
    try:
        return [words[i] for i in range(count.value)]
    finally:
        api.kernel.LocalFree(words)


@contextlib.contextmanager
def container(root: Path) -> Generator[str, None, None]:
    """Grant a fresh SID only this workspace and the running Python runtime."""
    api = Windows()
    name = "agentic." + uuid.uuid4().hex
    sid = ctypes.c_void_p()
    api.status(api.profiles.CreateAppContainerProfile(
        name, name, "Agentic confined shell", None, 0, ctypes.byref(sid),
    ), "create AppContainer profile")
    granted: list[tuple[Path, bool]] = []
    try:
        text = ctypes.c_void_p()
        api.check(api.security.ConvertSidToStringSidW(sid, ctypes.byref(text)), "format AppContainer SID")
        try:
            value = ctypes.wstring_at(text)
        finally:
            api.kernel.LocalFree(text)
        runtime = {canonical_path(sys.base_prefix), canonical_path(sys.executable).parent}
        venv = Path(sys.prefix) / "pyvenv.cfg"
        if venv.exists():
            runtime.add(canonical_path(venv))
        parents = {parent for path in (*runtime, root) for parent in path.parents}
        for path in sorted(parents - runtime - {root}):
            if not api.can_grant(path):
                continue  # OS-owned directories retain their existing AppContainer ACL.
            granted.append((path, False))
            api.acl(path, sid, 0x1200A9, inherit=False, propagate=False)
        for path in sorted(runtime):
            if path == root or path.is_relative_to(root):
                continue
            if root.is_relative_to(path):
                raise OSError("AppContainer workspace must not be inside the Python runtime")
            if not api.can_grant(path):
                continue
            granted.append((path, True))
            api.acl(path, sid, 0x1200A9)  # FILE_GENERIC_READ | FILE_GENERIC_EXECUTE
        granted.append((root, True))
        api.acl(root, sid, 0x1F01FF)  # FILE_ALL_ACCESS, inherited by workspace children
        yield value
    finally:
        errors: list[str] = []
        for path, propagate in reversed(granted):
            try:
                api.acl(path, sid, 0, revoke=True, propagate=propagate)
            except OSError as exc:
                errors.append(str(exc))
        api.security.FreeSid(sid)
        code = api.profiles.DeleteAppContainerProfile(name)
        if code:
            errors.append(f"delete AppContainer profile: {code}")
        if errors:
            raise OSError("sandbox cleanup failed: " + "; ".join(errors))


def launch(policy: dict[str, Any]) -> int:
    """Create the confined process suspended, assign its job, then resume it."""
    api = Windows()
    sid = ctypes.c_void_p()
    api.check(api.security.ConvertStringSidToSidW(policy["sid"], ctypes.byref(sid)), "parse AppContainer SID")
    job = None
    info = ProcessInfo()
    attributes = None
    initialized = False
    capability_sids: list[ctypes.c_void_p] = []
    try:
        job = api.kernel.CreateJobObjectW(None, None)
        api.check(job, "create sandbox job")
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        api.check(api.kernel.SetInformationJobObject(
            job, 9, ctypes.byref(limits), ctypes.sizeof(limits),
        ), "configure sandbox job")
        capabilities = Capabilities(sid, None, 0, 0)
        if policy["network"]:
            for value in ("S-1-15-3-1", "S-1-15-3-2", "S-1-15-3-3"):
                capability = ctypes.c_void_p()
                api.check(api.security.ConvertStringSidToSidW(
                    value, ctypes.byref(capability),
                ), "parse network capability")
                capability_sids.append(capability)
            entries = (SidAttributes * len(capability_sids))(
                *(SidAttributes(value, 4) for value in capability_sids),
            )
            capabilities.capabilities = entries
            capabilities.count = len(entries)
        size = ctypes.c_size_t()
        api.kernel.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        if not size.value:
            api.check(False, "size process attributes")
        attributes = ctypes.create_string_buffer(size.value)
        api.check(api.kernel.InitializeProcThreadAttributeList(
            attributes, 1, 0, ctypes.byref(size),
        ), "initialize process attributes")
        initialized = True
        api.check(api.kernel.UpdateProcThreadAttribute(
            attributes, 0, 0x20009, ctypes.byref(capabilities),
            ctypes.sizeof(capabilities), None, None,
        ), "set AppContainer capabilities")
        startup = StartupEx()
        startup.startup.cb = ctypes.sizeof(startup)
        startup.startup.flags = 0x100  # STARTF_USESTDHANDLES
        startup.startup.stdin = api.kernel.GetStdHandle(W.DWORD(-10).value)
        startup.startup.stdout = api.kernel.GetStdHandle(W.DWORD(-11).value)
        startup.startup.stderr = api.kernel.GetStdHandle(W.DWORD(-12).value)
        startup.attributes = ctypes.cast(attributes, ctypes.c_void_p)
        argv = policy["argv"]
        executable = shutil.which(argv[0])
        if executable is None:
            raise OSError(f"command not found: {argv[0]}")
        if Path(executable).suffix.lower() in (".cmd", ".bat"):
            raise OSError("AppContainer direct execution requires an executable, not a batch script")
        command_line = subprocess.list2cmdline(argv)
        if "command" in policy:
            # cmd.exe parses its /c tail itself, not with CRT argv quoting.
            command_line += ' "' + policy["command"] + '"'
        command = ctypes.create_unicode_buffer(command_line)
        environment = ctypes.create_unicode_buffer(
            "\0".join(f"{key}={value}" for key, value in sorted(os.environ.items())) + "\0\0",
        )
        api.check(api.kernel.CreateProcessW(
            executable, command, None, None, True,
            0x80000 | 0x400 | 0x4 | 0x08000000,
            environment, policy["cwd"], ctypes.byref(startup), ctypes.byref(info),
        ), "create AppContainer process")
        api.check(api.kernel.AssignProcessToJobObject(job, info.process), "assign sandbox job")
        if api.kernel.ResumeThread(info.thread) == 0xFFFFFFFF:
            api.check(False, "resume sandbox process")
        if api.kernel.WaitForSingleObject(info.process, 0xFFFFFFFF) != 0:
            api.check(False, "wait for sandbox process")
        code = W.DWORD()
        api.check(api.kernel.GetExitCodeProcess(info.process, ctypes.byref(code)), "read sandbox exit code")
        return code.value
    finally:
        # If setup failed after CreateProcess, even the suspended child must die.
        if info.process:
            if api.kernel.WaitForSingleObject(info.process, 0) == 0x102:
                api.check(api.kernel.TerminateProcess(info.process, 126), "terminate sandbox process")
            api.kernel.CloseHandle(info.thread)
            api.kernel.CloseHandle(info.process)
        if job:
            api.kernel.CloseHandle(job)
        if initialized:
            api.kernel.DeleteProcThreadAttributeList(attributes)
        for capability in capability_sids:
            api.kernel.LocalFree(capability)
        api.kernel.LocalFree(sid)


if __name__ == "__main__":
    try:
        sys.exit(launch(json.loads(sys.argv[1])))
    except OSError as exc:
        print(f"sandbox: {exc}", file=sys.stderr)
        sys.exit(126)
