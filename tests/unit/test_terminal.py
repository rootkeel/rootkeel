# SPDX-License-Identifier: AGPL-3.0-only
"""Only owner-level token operations and the session list cross the loopback helper boundary."""
import os
import secrets
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import ClientSession, UnixConnector, web
from aiohttp.test_utils import TestClient, TestServer
from rootkeel import peer, terminal
from rootkeel.terminal import application, command, token_command


class TerminalTests(unittest.IsolatedAsyncioTestCase):
    async def test_helper_accepts_only_token_and_session_list_operations(self):
        app = web.Application()
        app.router.add_post("/token", token_command)
        async with TestClient(TestServer(app)) as client:
            with patch("rootkeel.terminal.command", AsyncMock(return_value="token_1: token\n")) as command:
                for args in (["zellij", "web", "--create-token"], ["zellij", "web", "--revoke-token", "token_1"],
                             ["zellij", "web", "--list-tokens"], ["zellij", "web", "--revoke-all-tokens"],
                             ["zellij", "list-sessions", "--short", "--no-formatting"]):
                    result = await client.post("/token", json=args)
                    self.assertEqual(result.status, 200)
                    self.assertEqual(await result.text(), "token_1: token\n")
                    command.assert_awaited_with(*args)
                calls = command.await_count
                for args in (None, {}, ["sh", "-c", "cat /credentials/machine.json"],
                             ["zellij", "web", "--revoke-token", "../bad"], ["zellij", "web", "--create-token", "extra"],
                             ["zellij", "web", "--revoke-all-tokens", "token_1"], ["zellij", "list-sessions"],
                             ["zellij", "list-sessions", "--short", "--no-formatting", "extra"]):
                    result = await client.post("/token", json=args)
                    self.assertEqual(result.status, 400)
                self.assertEqual(command.await_count, calls)

    async def test_no_sessions_lists_nothing_but_other_failures_fail(self):
        # Zellij lists no sessions by exiting 1 with no output.
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "zellij"
            fake.write_text("#!/bin/sh\nexit 1\n")
            fake.chmod(0o755)
            with patch.dict(os.environ, {"PATH": directory + os.pathsep + os.environ["PATH"]}):
                self.assertEqual(await command("zellij", "list-sessions", "--short", "--no-formatting"), "")
                with self.assertRaises(web.HTTPServiceUnavailable):
                    await command("zellij", "web", "--list-tokens")


# Zellij 0.45.1 as the helper's start() sees it, keeping its sessions and web server in files: starting either again
# fails as Zellij does ("Session already exists" exits 1, "Address in use" 2), web --status exits 0 either way, and a
# port another program holds ("taken") fails to start the web server. Every call is logged.
FAKE_ZELLIJ = """#!/bin/sh
state=$(dirname "$0")
echo "$*" >> "$state/calls"
case "$*" in
  --version) echo "zellij 0.45.1" ;;
  "list-sessions --short --no-formatting") [ -s "$state/sessions" ] || exit 1; cat "$state/sessions" ;;
  *" attach --create-background "*)
    name=${*##* }
    if grep -qx "$name" "$state/sessions" 2>/dev/null; then echo "Session already exists" >&2; exit 1; fi
    echo "$name" >> "$state/sessions" ;;
  *" web --status"*)
    if [ -e "$state/web" ]; then echo "Web server online with version: 0.45.1. Checked: http://127.0.0.1:8082"
    else echo "Web server is offline, checked: http://127.0.0.1:8082"; fi ;;
  *" web --daemonize "*)
    if [ -e "$state/web" ] || [ -e "$state/taken" ]; then echo "Address in use" >&2; exit 2; fi
    touch "$state/web" ;;
  *) exit 3 ;;
esac
"""


class StartTests(unittest.IsolatedAsyncioTestCase):
    """start() against a stand-in Zellij 0.45.1: it starts only what is not already running."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name)
        (self.state / "zellij").write_text(FAKE_ZELLIJ)
        (self.state / "zellij").chmod(0o755)
        environment = patch.dict(os.environ, {"PATH": directory.name + os.pathsep + os.environ["PATH"],
                                              "RT_SESSION": "main", "RT_ZELLIJ_CONFIG": "/config"})
        environment.start()
        self.addCleanup(environment.stop)

    async def start(self):
        (self.state / "calls").write_text("")
        await terminal.start(None)
        return [call.split(" web ")[-1] if " web " in call else call.split()[-2:]
                for call in (self.state / "calls").read_text().splitlines()
                if "attach" in call or " web --daemonize" in call]

    def sessions(self):
        return (self.state / "sessions").read_text().split()

    async def test_start_reuses_a_session_and_web_server_that_outlived_the_helper(self):
        # The first start (a boot): the session and the web server.
        self.assertEqual(await self.start(), [["--create-background", "main"],
                                              "--daemonize --ip 127.0.0.1 --port 8082"])
        # The unit restarts (needrestart, after a library upgrade), KillMode=process: both still run, and are kept.
        self.assertEqual(await self.start(), [])
        self.assertEqual(self.sessions(), ["main"])
        self.assertTrue((self.state / "web").exists())
        # Whichever one has gone is started again, alone.
        (self.state / "web").unlink()
        self.assertEqual(await self.start(), ["--daemonize --ip 127.0.0.1 --port 8082"])
        (self.state / "sessions").write_text("other\n")
        self.assertEqual(await self.start(), [["--create-background", "main"]])
        self.assertEqual(self.sessions(), ["other", "main"])

    async def test_a_web_server_that_cannot_start_still_fails(self):
        # Another program on 8082: the helper fails to start, as before, rather than run without its web server.
        (self.state / "taken").touch()
        with self.assertRaises(web.HTTPServiceUnavailable):
            await terminal.start(None)


@unittest.skipUnless(sys.platform == "linux", "abstract sockets and SO_PEERCRED are Linux's")
class AbstractSocketTests(unittest.IsolatedAsyncioTestCase):
    """A helper on an abstract socket, which any process in the network namespace can connect to: only the agent's uid passes."""
    async def ask(self, agent_uid):
        name = "@rt-test/" + secrets.token_hex(8)
        runner = web.AppRunner(application(agent_uid))
        await runner.setup()
        try:
            await web.UnixSite(runner, peer.address(name)).start()
            async with ClientSession(connector=UnixConnector(path=peer.address(name))) as client:
                with patch("rootkeel.terminal.command", AsyncMock(return_value="token_1: token\n")) as zellij:
                    async with client.post("http://localhost/token", json=["zellij", "web", "--create-token"]) as response:
                        result = response.status, await response.text()
                    async with client.get("http://localhost/ready") as response:
                        return result + (response.status, zellij.await_count)
        finally:
            await runner.cleanup()

    async def test_only_the_agent_uid_is_admitted(self):
        self.assertEqual(await self.ask(os.getuid()), (200, "token_1: token\n", 200, 1))
        # Any other uid (the shell's own) is refused before anything runs.
        self.assertEqual(await self.ask(os.getuid() + 1), (403, "Only the agent may use the terminal helper", 403, 0))

    async def test_abstract_socket_without_the_agent_uid_does_not_start(self):
        with patch.dict(os.environ, {"RT_TERMINAL_SOCKET": "@rt-test/" + secrets.token_hex(8)}), \
                patch.object(terminal, "start", AsyncMock()) as start:
            os.environ.pop("RT_TERMINAL_PEER_UID", None)
            mask = os.umask(0o022)
            try:
                with self.assertRaisesRegex(ValueError, "RT_TERMINAL_PEER_UID"):
                    await terminal.main()
            finally:
                os.umask(mask)  # main() sets its own
            start.assert_not_awaited()
