# SPDX-License-Identifier: AGPL-3.0-only
"""The agent's API: Zellij tokens per grant, session names, and the frpc target it dials."""
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from rootkeel.agent import Agent


class FakeZellij:
    """Zellij's web token store, as reached through the terminal helper."""
    def __init__(self):
        self.valid, self.minted = {}, 0

    async def command(self, *args):
        assert args[:2] == ("zellij", "web"), args
        if args[2] == "--create-token":
            self.minted += 1
            name = f"token_{len(self.valid) + 1}"
            self.valid[name] = f"secret-{self.minted}"
            return f"{name}: {self.valid[name]}\n"
        if args[2] == "--list-tokens":
            return "".join(f"{name}: created at now\n" for name in self.valid)
        if args[2] == "--revoke-all-tokens":
            self.valid.clear()
            return "Revoked all tokens\n"
        if args[2] == "--revoke-token" and args[3] in self.valid:
            del self.valid[args[3]]
            return "Successfully revoked token.\n"
        raise web.HTTPServiceUnavailable(text="Zellij command failed")


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.agent = Agent({"credential": "m" * 40})
        app = web.Application(middlewares=[self.agent.authenticate])
        app.router.add_post("/_agent/token", self.agent.token)
        app.router.add_post("/_agent/revoke", self.agent.revoke)
        app.router.add_post("/_agent/sessions", self.agent.sessions)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.headers = {"Authorization": "Bearer " + "m" * 40}

    async def asyncTearDown(self):
        await self.client.close()

    async def test_idempotent_token_issue_and_revoke(self):
        gid = "g_" + "a" * 24
        with patch("rootkeel.agent.command", AsyncMock(return_value="token_1: private-token\n")) as command:
            first = await self.client.post("/_agent/token", headers=self.headers, json={"grant_id": gid})
            second = await self.client.post("/_agent/token", headers=self.headers, json={"grant_id": gid})
            self.assertEqual(await first.json(), await second.json())
            self.assertEqual(command.await_count, 1)
            result = await self.client.post("/_agent/revoke", headers=self.headers,
                                            json={"grant_id": gid, "name": "token_1"})
            self.assertEqual(result.status, 200)
            command.assert_awaited_with("zellij", "web", "--revoke-token", "token_1")

    async def test_sessions_lists_zellij_session_names(self):
        with patch("rootkeel.agent.command", AsyncMock(return_value="main\nscratch\n../x\n")) as command:
            result = await self.client.post("/_agent/sessions", headers=self.headers, json={})
            self.assertEqual(await result.json(), {"sessions": ["main", "scratch"]})
            command.assert_awaited_once_with("zellij", "list-sessions", "--short", "--no-formatting")
            self.assertEqual((await self.client.post("/_agent/sessions", json={})).status, 401)

    async def test_sessions_from_an_older_helper_are_not_implemented(self):
        # A helper that does not know an operation refuses it, as it refuses anything else it does not know.
        async def old_helper(request):
            if await request.json() == ["zellij", "list-sessions", "--short", "--no-formatting"]:
                raise web.HTTPBadRequest(text="Unsupported Zellij operation")
            return web.Response(text="token_1: token\n")
        helper = web.Application()
        helper.router.add_post("/token", old_helper)
        runner = web.AppRunner(helper)
        await runner.setup()
        with tempfile.TemporaryDirectory() as directory:
            socket = os.path.join(directory, "terminal.sock")
            await web.UnixSite(runner, socket).start()
            try:
                with patch.dict(os.environ, {"RT_TERMINAL_SOCKET": socket}):
                    result = await self.client.post("/_agent/sessions", headers=self.headers, json={})
                    self.assertEqual((result.status, await result.text()), (501, "The terminal helper is older than the agent"))
                    result = await self.client.post("/_agent/token", headers=self.headers, json={"grant_id": "g_" + "b" * 24})
                    self.assertEqual(await result.json(), {"name": "token_1", "token": "token"})
            finally:
                await runner.cleanup()

    async def restart(self):
        await self.client.close()
        await self.asyncSetUp()
        await self.agent.reset_tokens()

    async def test_restart_then_revoke_leaves_no_valid_token(self):
        zellij = FakeZellij()
        gid = "g_" + "a" * 24
        with patch("rootkeel.agent.command", zellij.command):
            name = (await (await self.client.post("/_agent/token", headers=self.headers, json={"grant_id": gid})).json())["name"]
            await self.restart()
            self.assertEqual(zellij.valid, {})
            result = await self.client.post("/_agent/revoke", headers=self.headers, json={"grant_id": gid, "name": name})
            self.assertEqual(result.status, 200)
        self.assertEqual(zellij.valid, {})

    async def test_unknown_grant_revoke_leaves_no_token_but_spares_live_grant(self):
        zellij = FakeZellij()
        live = "g_" + "a" * 24
        with patch("rootkeel.agent.command", zellij.command):
            # A token this agent does not know about, e.g. left behind by lost agent state.
            await zellij.command("zellij", "web", "--create-token")
            minted = await (await self.client.post("/_agent/token", headers=self.headers, json={"grant_id": live})).json()
            for name in ("token_1", minted["name"], "token_9"):
                result = await self.client.post("/_agent/revoke", headers=self.headers,
                                                json={"grant_id": "g_" + "b" * 24, "name": name})
                self.assertEqual(result.status, 200)
        self.assertEqual(zellij.valid, {minted["name"]: minted["token"]})

    async def test_credentials_required_and_malformed_mapping_rejected(self):
        result = await self.client.post("/_agent/token", json={"grant_id": "g_" + "a" * 24})
        self.assertEqual(result.status, 401)
        result = await self.client.post("/_agent/token", headers=self.headers, json={"grant_id": "../bad"})
        self.assertEqual(result.status, 400)


class FrpcTargetTests(unittest.TestCase):
    MACHINE = {"id": "m_x", "credential": "c" * 40, "domain": "m_x.rt.internal"}

    def test_dials_enrolled_relay_host_and_port_only(self):
        from rootkeel.agent import frpc_config
        text = frpc_config(self.MACHINE | {"relay_host": "relay.staging.corp-example.net", "relay_port": 8443,
                                           "ca_file": "/etc/rt/staging-ca.crt"})
        self.assertIn('serverAddr = "relay.staging.corp-example.net"\nserverPort = 8443\n', text)
        self.assertIn('transport.tls.serverName = "relay.staging.corp-example.net"', text)
        # TLS trust must precede the proxy table to apply to the transport.
        self.assertLess(text.index("trustedCaFile"), text.index("[[proxies]]"))
        # The broker learns at the relay login which agent protocol this machine speaks.
        self.assertIn('metadatas.protocol = "1"\n', text)
        self.assertLess(text.index("metadatas.protocol"), text.index("[[proxies]]"))

    def test_credential_without_port_defaults_to_https(self):
        from rootkeel.agent import frpc_config
        self.assertIn("serverPort = 443\n", frpc_config(self.MACHINE | {"relay_host": "relay.example.org"}))

    def test_incomplete_or_invalid_target_refused(self):
        from rootkeel.agent import frpc_config
        from rootkeel.config import ConfigError
        for change in ({}, {"relay_host": "10.0.0.5"}, {"relay_host": "relay.example.org", "relay_port": 0},
                       {"relay_host": "https://relay.example.org"}):
            with self.assertRaises(ConfigError, msg=change):
                frpc_config(self.MACHINE | change)


class TerminalSocketTests(unittest.TestCase):
    def test_terminal_socket_may_be_abstract(self):
        from rootkeel.agent import terminal_socket
        with patch.dict(os.environ, {"RT_TERMINAL_SOCKET": "@rootkeel/terminal"}):
            self.assertEqual(terminal_socket(), "\0rootkeel/terminal")
        with patch.dict(os.environ, {"RT_TERMINAL_SOCKET": "/run/x/terminal.sock"}):
            self.assertEqual(terminal_socket(), "/run/x/terminal.sock")
