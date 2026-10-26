# SPDX-License-Identifier: AGPL-3.0-only
"""Execute DAC, procfs and ptrace access as the real agent and shell UIDs.

Run as root in an ephemeral Linux container; normal unprivileged discovery skips
this suite. No host sysctl or service is changed.
"""
import ctypes
import json
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path

from rootkeel.agent import frpc_config, protect_process


@unittest.skipUnless(sys.platform == "linux" and os.geteuid() == 0, "requires root in disposable Linux container")
class ProcessIsolationTests(unittest.TestCase):
    def test_shell_cannot_read_agent_credentials_or_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o755)
            private = root / "private"
            private.mkdir(mode=0o700)
            os.chown(private, 1001, 1001)
            config = {"id": "m_test", "credential": "private-machine-credential",
                      "relay_host": "relay.example.org", "domain": "m_test.rt.internal"}
            credential = private / "machine.json"
            credential.write_text(json.dumps(config))
            credential.chmod(0o600)
            os.chown(credential, 1001, 1001)
            reader, writer = os.pipe()
            agent = os.fork()
            if agent == 0:
                try:
                    os.close(reader)
                    os.setgroups([])
                    os.setgid(1001)
                    os.setuid(1001)
                    protect_process()
                    loaded = json.loads(credential.read_text())
                    path = private / "frpc.toml"
                    path.write_text(frpc_config(loaded))
                    path.chmod(0o600)
                    os.write(writer, b"ready")
                    os.close(writer)
                    signal.pause()
                finally:
                    os._exit(1)
            os.close(writer)
            try:
                self.assertEqual(os.read(reader, 5), b"ready")
                shell = os.fork()
                if shell == 0:
                    try:
                        os.setgroups([])
                        os.setgid(1000)
                        os.setuid(1000)
                        for path in (credential, private / "frpc.toml", Path(f"/proc/{agent}/mem"),
                                     Path(f"/proc/{agent}/environ"), Path(f"/proc/{agent}/root") / credential.relative_to("/")):
                            try:
                                path.read_bytes()
                            except PermissionError:
                                continue
                            os._exit(2)
                        libc = ctypes.CDLL(None, use_errno=True)
                        if libc.ptrace(16, agent, 0, 0) != -1 or ctypes.get_errno() != 1:  # PTRACE_ATTACH / EPERM
                            os._exit(3)
                        os._exit(0)
                    except BaseException:
                        os._exit(4)
                _, status = os.waitpid(shell, 0)
                self.assertEqual(os.waitstatus_to_exitcode(status), 0, "shell obtained access to the agent")
            finally:
                os.close(reader)
                os.kill(agent, signal.SIGKILL)
                os.waitpid(agent, 0)

    def test_abstract_sockets_admit_only_the_other_account(self):
        """A terminal helper on an abstract socket, which any uid in the network namespace can
        connect to: the helper (the shell's, 1000) admits only the agent (1001). Nothing else
        passes, not even the helper's own uid."""
        import asyncio
        import http.client
        import secrets
        import socket
        from aiohttp import web
        from rootkeel import peer, terminal

        class Abstract(http.client.HTTPConnection):
            def __init__(self, name):
                super().__init__("helper", timeout=10)
                self.name_ = name

            def connect(self):
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.connect(peer.address(self.name_))

        def status_as(uid, name, path):
            child = os.fork()
            if child == 0:
                try:
                    os.setgroups([])
                    os.setgid(uid)
                    os.setuid(uid)
                    connection = Abstract(name)
                    connection.request("GET", path)
                    os._exit({200: 0, 403: 11}.get(connection.getresponse().status, 12))
                except BaseException:
                    os._exit(13)
            _, status = os.waitpid(child, 0)
            return os.waitstatus_to_exitcode(status)

        def serve(uid, start):
            """Runs start(), then waits forever, as uid in a child; returns its pid once it listens."""
            reader, writer = os.pipe()
            child = os.fork()
            if child == 0:
                try:
                    os.close(reader)
                    os.setgroups([])
                    os.setgid(uid)
                    os.setuid(uid)

                    async def run():
                        await start()
                        os.write(writer, b"ready")
                        await asyncio.Event().wait()
                    asyncio.run(run())
                finally:
                    os._exit(1)
            os.close(writer)
            try:
                self.assertEqual(os.read(reader, 5), b"ready")
            except BaseException:
                os.kill(child, signal.SIGKILL)
                os.waitpid(child, 0)
                raise
            finally:
                os.close(reader)
            return child

        helper_name = "@rt-test/" + secrets.token_hex(8)

        async def helper():
            runner = web.AppRunner(terminal.application(agent_uid=1001))
            await runner.setup()
            await web.UnixSite(runner, peer.address(helper_name)).start()

        servers = []
        try:
            servers.append(serve(1000, helper))
            self.assertEqual(status_as(1001, helper_name, "/ready"), 0, "the agent was refused the terminal helper")
            self.assertEqual(status_as(1000, helper_name, "/ready"), 11, "the shell was admitted to the agent-only helper")
            self.assertEqual(status_as(1002, helper_name, "/ready"), 11, "another account reached the terminal helper")
        finally:
            for pid in servers:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
