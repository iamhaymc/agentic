"""Explicitly selected native sandbox tests; no inference or external network.

Run: uv run python -m unittest test.agentic_sandbox_e2e -v
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import app.agentic as A


@unittest.skipUnless(A.AppContainerSandbox.available(), "requires Windows AppContainer")
class AppContainerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "workspace"
        self.root.mkdir()
        self.secret = self.base / "private.txt"
        self.secret.write_text("private sentinel", encoding="utf-8")
        self.ws = A.Workspace(self.root, sandbox=A.AppContainerSandbox(), shell_timeout=20)

    def shell(self, command):
        return asyncio.run(self.ws.run_shell(command))

    def python(self, code):
        # cmd's executable discovery queries volume metadata unavailable in an
        # AppContainer. Quoting the absolute path requests exact execution.
        return self.shell(f'"{sys.executable}" ' + subprocess.list2cmdline(["-I", "-S", "-c", code]))

    def test_cmd_runs_and_preserves_exit_status(self):
        self.assertEqual(self.shell("echo sandbox-ready").strip(), "sandbox-ready")
        self.assertIn("[exit code 7]", self.shell("exit /b 7"))

    def test_workspace_read_write_and_outside_denial(self):
        output = self.shell(
            f'echo inside>result.txt & type result.txt & type "{self.secret}" '
            f'& echo escape>"{self.base / "escape.txt"}"'
        )
        self.assertIn("inside", output)
        self.assertEqual((self.root / "result.txt").read_text().strip(), "inside")
        self.assertNotIn("private sentinel", output)
        self.assertFalse((self.base / "escape.txt").exists())
        self.assertIn("[exit code", output)

    def test_python_runtime_environment_and_network_are_confined(self):
        with socket.socket() as tcp, socket.socket(type=socket.SOCK_DGRAM) as udp:
            tcp.bind(("127.0.0.1", 0))
            tcp.listen()
            udp.bind(("127.0.0.1", 0))
            # Prove the TCP fixture is reachable before asking the sandbox.
            with socket.create_connection(tcp.getsockname(), timeout=1):
                connection, _ = tcp.accept()
                connection.close()
            with unittest.mock.patch.dict(os.environ, {"AGENTIC_INFERENCE_KEY": "private sentinel"}):
                output = self.python(
                    "import os,socket; print('secret', os.getenv('AGENTIC_INFERENCE_KEY')); "
                    "print('cwd', os.getcwd()); print('home', os.environ['HOME']); "
                    f"s=socket.socket(); s.settimeout(1); print('connect', s.connect_ex({tcp.getsockname()!r})); "
                    "u=socket.socket(type=socket.SOCK_DGRAM); u.settimeout(1); "
                    f"print('send',u.sendto(b'forbidden',{udp.getsockname()!r}))"
                )
            tcp.settimeout(0.2)
            udp.settimeout(0.2)
            with self.assertRaises(TimeoutError):
                tcp.accept()
            with self.assertRaises(TimeoutError):
                udp.recvfrom(128)
        self.assertIn("secret None", output)
        self.assertIn(str(self.root), output)
        self.assertNotIn("private sentinel", output)
        self.assertNotIn("connect 0", output)

    def test_command_allowlist_runs_without_cmd(self):
        self.ws.shell_allow_commands = "python"
        self.assertIn("literal-ready", self.python("print('literal-ready')"))
        self.assertIn("Error:", self.shell("cmd /c echo forbidden"))

    def test_timeout_kills_confined_descendants(self):
        pid_file = self.root / "pid.txt"
        self.ws.shell_timeout = 2
        output = self.python(
            "import os,time; from pathlib import Path; "
            "Path('pid.txt').write_text(str(os.getpid())); time.sleep(30)"
        )
        self.assertIn("timed out", output)
        self.assertTrue(pid_file.exists(), output)
        native = A.windows_sandbox_module()
        api = native.Windows()
        api.kernel.OpenProcess.argtypes = [native.W.DWORD, native.W.BOOL, native.W.DWORD]
        api.kernel.OpenProcess.restype = native.W.HANDLE
        handle = api.kernel.OpenProcess(0x100000, False, int(pid_file.read_text()))
        if handle:
            try:
                self.assertEqual(api.kernel.WaitForSingleObject(handle, 1000), 0)
            finally:
                api.kernel.CloseHandle(handle)
        else:
            self.assertEqual(native.ctypes.get_last_error(), 87)  # No such process.

    def test_hardlinks_are_rejected_before_acl_setup(self):
        link = self.root / "linked.txt"
        os.link(self.secret, link)
        output = self.shell("echo unsafe")
        self.assertIn("hardlinks", output)
        self.assertNotIn("unsafe", output)
        self.assertEqual(self.secret.read_text(), "private sentinel")

    def test_direct_python_has_an_appcontainer_token(self):
        self.ws.shell_allow_commands = "python"
        output = self.python(
            "import ctypes; from ctypes import wintypes as W; "
            "a=ctypes.WinDLL('advapi32'); t=W.HANDLE(); "
            "assert a.OpenProcessToken(W.HANDLE(-1),8,ctypes.byref(t)); "
            "v=W.DWORD(); n=W.DWORD(); "
            "assert a.GetTokenInformation(t,29,ctypes.byref(v),4,ctypes.byref(n)); "
            "print('AppContainer token:',v.value)"
        )
        self.assertIn("AppContainer token: 1", output)

    def test_concurrent_commands_keep_independent_grants(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            outputs = list(executor.map(
                self.python,
                ["import time; time.sleep(0.3); print('first')",
                 "import time; time.sleep(0.3); print('second')"],
            ))
        self.assertIn("first", outputs[0])
        self.assertIn("second", outputs[1])

    def test_setup_cleanup_removes_grants_and_profile(self):
        native = A.windows_sandbox_module()
        with native.container(self.root) as sid:
            self.assertIn(sid, subprocess.check_output(
                ["icacls", str(self.root)], text=True,
            ))
        self.assertNotIn(sid, subprocess.check_output(["icacls", str(self.root)], text=True))


@unittest.skipUnless(A.LandlockSandbox.available(), "requires Linux Landlock")
class LandlockNetworkTest(unittest.TestCase):
    def test_socket_creation_is_denied_including_udp_and_local_ipc(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = A.Workspace(tmp, sandbox=A.LandlockSandbox(), shell_allow_commands="python3")
            for family, kind in (("AF_INET", "SOCK_STREAM"), ("AF_INET", "SOCK_DGRAM"),
                                 ("AF_INET6", "SOCK_DGRAM"), ("AF_UNIX", "SOCK_STREAM")):
                code = (
                    f"import socket; socket.socket(socket.{family},socket.{kind}); print('UNSAFE')"
                )
                command = f"python3 -c {shlex.quote(code)}"
                with self.subTest(family=family, kind=kind):
                    output = asyncio.run(ws.run_shell(command))
                    self.assertNotIn("UNSAFE", output)
                    self.assertIn("Operation not permitted", output)


@unittest.skipUnless(os.name == "posix", "requires a POSIX sandbox")
class PosixSandboxTest(unittest.TestCase):
    def setUp(self):
        self.backend = A.pick_sandbox()
        if self.backend is None:
            self.skipTest("no native sandbox available")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "workspace"
        self.root.mkdir()
        self.ws = A.Workspace(self.root, sandbox=self.backend, shell_timeout=10)

    def test_filesystem_boundary_is_inherited(self):
        secret = self.base / "secret.txt"
        secret.write_text("private sentinel")
        outside = self.base / "escape.txt"
        output = asyncio.run(self.ws.run_shell(
            f"echo inside > result.txt; cat result.txt; cat {shlex.quote(str(secret))}; "
            f"echo escape > {shlex.quote(str(outside))}"
        ))
        self.assertIn("inside", output)
        self.assertNotIn("private sentinel", output)
        self.assertFalse(outside.exists())

    def test_network_is_denied_by_default_and_available_only_when_enabled(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(0.2)
            command = (
                f"{shlex.quote(sys.executable)} -I -S -c "
                + shlex.quote(
                    "import socket; s=socket.socket(); s.settimeout(1); "
                    f"s.connect({listener.getsockname()!r}); print('connected')"
                )
            )
            denied = asyncio.run(self.ws.run_shell(command))
            self.assertNotIn("connected", denied)
            with self.assertRaises(TimeoutError):
                listener.accept()
            self.ws.shell_network = True
            allowed = asyncio.run(self.ws.run_shell(command))
            self.assertIn("connected", allowed)
            connection, _ = listener.accept()
            connection.close()


if __name__ == "__main__":
    unittest.main()
