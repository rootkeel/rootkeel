# SPDX-License-Identifier: AGPL-3.0-only
"""Execute policy/auth/grant lifetime against real aiohttp sockets and a fake agent."""
import asyncio
import contextlib
import io
import json
from http.cookies import SimpleCookie
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

from aiohttp import WSMsgType, WSServerHandshakeError, web
from aiohttp.test_utils import TestClient, TestServer
from rootkeel import admin, broker
from rootkeel.agent import PROTOCOL as AGENT_PROTOCOL
from rootkeel.broker import COOKIE_AGE, FULLSCREEN_JS, FULLSCREEN_SCRIPT, ICON_LINKS, LINK_CLEANUP, MAX_COOKIES, MOBILE_JS, MOBILE_SCRIPT, PERMANENT_EXPIRES, \
    RELOAD_GUARD, SIGNIN_JS, Broker
from rootkeel.store import digest

# Idle and absolute limits other terminal services commonly impose: 15 minutes without input, 8 hours.
OLD_IDLE, OLD_CAP = 900, 28800


class BrokerTests(unittest.IsolatedAsyncioTestCase):
    # Lab fixture names; NonDefaultDomainBrokerTests reruns every test on another environment.
    ENV = {"RT_ENVIRONMENT": "lab", "RT_PUBLIC_HOST": "term.test", "RT_RELAY_HOST": "relay.test"}
    ORIGIN = "https://term.test"
    HOST = "term.test"
    PORT_HOST = "term.test:443"  # The stock native client spells the port explicitly.
    HSTS = "max-age=31536000"
    FOREIGN_ORIGINS = ("https://evil.test", "null")

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        owner_path = Path(self.temp.name) / "owner"
        owner_path.write_text("o" * 40)
        self.key_path = Path(self.temp.name) / "db-key"
        self.key_path.write_text("ab" * 32)
        (Path(self.temp.name) / "data").mkdir()
        self.owner = {"Authorization": "Bearer " + "o" * 40}
        self.upstream_sockets = []
        self.backend_logins = 0
        # The agent's grant map and the tokens Zellij currently accepts.
        self.agent_tokens, self.zellij_valid, self.minted = {}, set(), 0
        self.agent_requests = []
        # Each machine's credential, as its agent checks it.
        self.agent_credentials = {"m_test": "m" * 40, "m_bob": "b" * 40}
        self.upstream_messages = []  # (path, data) as the machine's Zellij receives them
        self.sessions = ["main", "other"]
        self.agent_state = "current"  # or "old-helper" (cannot list sessions), or "offline"

        async def fake(request):
            mid = request.headers.get("Host", "").removesuffix(".rt.internal")
            self.agent_requests.append((mid, request.path))
            credential = self.agent_credentials.get(mid)
            if not credential or request.headers.get("Authorization") != "Bearer " + credential:
                raise web.HTTPUnauthorized()
            if request.path == "/_agent/token":
                gid = (await request.json())["grant_id"]
                if gid not in self.agent_tokens:
                    self.minted += 1
                    self.agent_tokens[gid] = {"name": f"token_{self.minted}", "token": f"backend-secret-{self.minted}"}
                    self.zellij_valid.add(self.agent_tokens[gid]["token"])
                return web.json_response(self.agent_tokens[gid])
            if request.path == "/_agent/revoke":
                token = self.agent_tokens.pop((await request.json())["grant_id"], None)
                if token:
                    self.zellij_valid.discard(token["token"])
                return web.json_response({"ok": True})
            if self.agent_state == "offline":
                raise web.HTTPNotFound()  # As the relay answers for a machine without a tunnel.
            if request.path == "/_agent/sessions" and self.agent_state == "old-helper":
                raise web.HTTPNotImplemented(text="The terminal helper is older than the agent")
            if request.path == "/_agent/sessions" and self.agent_state == "current":
                return web.json_response({"sessions": self.sessions})
            if request.path == "/command/login":
                self.backend_logins += 1
                token = (await request.json())["auth_token"]
                if token not in self.zellij_valid:
                    return web.json_response({"success": False}, status=401)
                response = web.json_response({"success": True})
                response.set_cookie("session_token", "upstream-cookie-" + token)
                return response
            # Zellij logs out the sessions of a revoked token.
            if request.path in ("/session", "/session-list") or request.path.startswith("/ws/"):
                if request.cookies.get("session_token", "").removeprefix("upstream-cookie-") not in self.zellij_valid:
                    raise web.HTTPUnauthorized()
            if request.path.startswith("/ws/"):
                ws = web.WebSocketResponse()
                await ws.prepare(request)
                self.upstream_sockets.append(ws)
                async for msg in ws:
                    self.upstream_messages.append((request.path, msg.data))
                    if msg.data == "detach":  # As Zellij ends the connection on a detach.
                        await ws.close(code=1000, message=b"Connection closed")
                    elif msg.type == WSMsgType.TEXT:
                        await ws.send_str(("bob:" if mid == "m_bob" else "") + msg.data)
                return ws
            if request.path == "/assets/large.js":
                stream = web.StreamResponse(headers={"Content-Type": "application/javascript"})
                await stream.prepare(request)
                for _ in range(4):
                    await stream.write(b"x" * 20000)
                    await asyncio.sleep(0.02)
                await stream.write_eof()
                return stream
            if request.path == "/session":
                return web.json_response({"web_client_id": "x"})
            if request.path == "/session-list":
                return web.json_response({"sessions": [{"name": "main"}, {"name": "other"}]})
            if request.path == "/assets/owner.js":
                return web.Response(text=mid, content_type="application/javascript")
            if request.path == "/main":  # Zellij's page, its assets in <head>.
                return web.Response(text='<!doctype html><html><head><script src="assets/xterm.js"></script></head></html>',
                                    content_type="text/html")
            return web.Response(text="0.45.1")

        upstream = web.Application()
        upstream.router.add_route("*", "/{path:.*}", fake)
        self.upstream = TestServer(upstream)
        await self.upstream.start_server()
        env = {"RT_DB": self.temp.name + "/data/db", "RT_OWNER_FILE": str(owner_path), "RT_DB_KEY_FILE": str(self.key_path),
               "RT_RELAY_VHOST_URL": str(self.upstream.make_url("/")).rstrip("/"), **self.ENV}
        with patch.dict(os.environ, env):
            self.broker = Broker()
        self.add_machine("m_test", "m" * 40)
        self.client = TestClient(TestServer(broker.application(self.broker)))
        await self.client.start_server()
        # A browser or native client opens a grant on the host of its URL: the grant owner's terminal host.
        request, ws_connect = self.client._request, self.client._ws_connect

        def owner_view(path, kwargs):
            headers = dict(kwargs.get("headers") or {})
            match = re.match(r"/g/(g_[0-9a-f]{24})/", path)
            if headers.pop("X-Test-Public-Host", None):
                kwargs = dict(kwargs, headers=headers)
            elif match and headers.get("Host") in (self.HOST, self.PORT_HOST):
                origin = self.broker.terminal_origin
                host = urlsplit(origin).netloc
                headers["Host"] = host + (":443" if headers["Host"] != self.HOST and ":" not in host else "")
                if headers.get("Origin") == self.ORIGIN:
                    headers["Origin"] = origin
                kwargs = dict(kwargs, headers=headers)
            return kwargs
        self.client._request = lambda method, path, **kwargs: request(method, path, **owner_view(path, kwargs))
        self.client._ws_connect = lambda path, **kwargs: ws_connect(path, **owner_view(path, kwargs))
        self.host = {"Host": self.HOST}
        private = web.Application()
        private.router.add_post("/_frp", self.broker.policy)
        self.private = TestClient(TestServer(private))
        await self.private.start_server()

    def add_machine(self, mid, secret, name=None, owner=None):
        self.broker.store.execute("INSERT INTO machines(id,secret,owner,name,enrolled) VALUES(?,?,?,?,?)",
                                  (mid, self.broker.store.seal(secret, "machines.secret", mid), owner or self.broker.owner_id,
                                   name, time.time()))

    async def asyncTearDown(self):
        await self.private.close()
        await self.client.close()
        await self.upstream.close()
        self.temp.cleanup()

    async def grant(self, **params):
        response = await self.client.post("/api/grants", headers=self.host | self.owner,
                                          json={"session": "main", **params})
        self.assertEqual(response.status, 200)
        grant = await response.json()
        self.assertEqual(grant["url"], self.owner_origin("owner") + "/g/" + grant["id"] + "/" + params.get("session", "main"))
        self.current_grant = grant
        return grant

    def owner_origin(self, owner):
        scheme, host = self.ORIGIN.split("://")
        return f"{scheme}://{owner}.{host}"

    def terminal(self, path, grant=None):
        return "/g/" + (grant or self.current_grant)["id"] + path

    async def login(self, grant, browser=False):
        headers = self.host | ({"Origin": self.ORIGIN} if browser else {})
        response = await self.client.post(self.terminal("/command/login", grant), headers=headers, json={"auth_token": grant["token"]})
        self.assertEqual(response.status, 200)
        self.assertTrue(response.cookies["session_token"]["secure"])
        self.assertTrue(response.cookies["session_token"]["httponly"])
        self.assertEqual(response.cookies["session_token"]["path"], self.terminal("/", grant))
        raw = SimpleCookie()
        raw.load(response.headers["Set-Cookie"])
        self.assertFalse(raw["session_token"]["domain"])
        self.assertEqual(response.headers["Strict-Transport-Security"], self.HSTS)
        return response.cookies["session_token"].value

    async def sockets(self, grant, browser=False):
        cookie = await self.login(grant, browser)
        headers = self.host | {"Cookie": "session_token=" + cookie}
        if browser:
            headers["Origin"] = self.ORIGIN
        session = grant["url"].rsplit("/", 1)[1]
        return [await self.client.ws_connect(self.terminal(path, grant), headers=headers)
                for path in ("/ws/control", "/ws/terminal/" + session)]

    async def closed(self, sockets, within=3):
        for ws in sockets:
            msg = await asyncio.wait_for(ws.receive(), within)
            self.assertIn(msg.type, (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR))

    @contextlib.contextmanager
    def fake_clock(self):
        """Wall-clock time the broker and its store see; advance it with now[0] += seconds."""
        now = [time.time()]
        clock = SimpleNamespace(time=lambda: now[0], monotonic=time.monotonic)
        with patch("rootkeel.store.time", clock), patch("rootkeel.broker.time", clock):
            yield now

    async def echoes(self, ws, text):
        await ws.send_str(text)
        self.assertEqual((await asyncio.wait_for(ws.receive(), 3)).data, text)

    def admin(self, *argv):
        env = {"RT_DB": self.temp.name + "/data/db", "RT_DB_KEY_FILE": str(self.key_path)}
        with patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(admin.main(list(argv)), 0)
        return out.getvalue()

    async def test_grant_cookie_selected_among_root_and_other_grant_cookies(self):
        old, new = await self.grant(), await self.grant()
        legacy = await self.login(old)

        async def get(path, *cookies, grant=new):
            return await self.client.get(self.terminal(path, grant), headers=self.host | {
                "Cookie": "; ".join("session_token=" + c for c in cookies)})
        for cookies in ((legacy,), ("invalid",), ()):
            page = await get("/main", *cookies)
            self.assertEqual(page.status, 200)
            self.assertIn('type="password"', await page.text())
            self.assertEqual((await get("/assets/owner.js", *cookies)).status, 401)
        cookie = await self.login(new)
        for cookies in ((cookie, legacy), (legacy, cookie), ("invalid", cookie, legacy)):
            asset = await get("/assets/owner.js", *cookies)
            self.assertEqual((asset.status, await asset.text()), (200, "m_test"))
            page = await get("/main", *cookies)
            self.assertNotIn('type="password"', await page.text())
            self.assertEqual((await get("/assets/owner.js", *cookies, grant=old)).status, 200)
        headers = self.host | {"Cookie": f"session_token={legacy}; session_token={cookie}"}
        ws = await self.client.ws_connect(self.terminal("/ws/terminal/main", new), headers=headers)
        await ws.send_str("both-cookies")
        self.assertEqual((await ws.receive()).data, "both-cookies")
        await ws.close()
        with self.assertRaises(WSServerHandshakeError):
            await self.client.ws_connect(self.terminal("/ws/terminal/main", new), headers=self.host | {"Cookie": "session_token=" + legacy})
        response = await self.client.post("/api/revoke", headers=self.host | self.owner, json={"id": new["id"]})
        self.assertEqual(response.status, 200)
        page = await get("/main", cookie, legacy)
        self.assertEqual(page.status, 200)
        self.assertNotIn('type="password"', await page.text())
        self.assertEqual((await get("/assets/owner.js", cookie, legacy)).status, 401)
        self.assertEqual((await get("/assets/owner.js", legacy, cookie, grant=old)).status, 200)

    async def test_revoke_closes_both_browser_and_native_channels(self):
        grant = await self.grant()
        sockets = await self.sockets(grant) + await self.sockets(grant, True)
        response = await self.client.post("/api/revoke", headers=self.host | self.owner, json={"id": grant["id"]})
        self.assertEqual(response.status, 200)
        await self.closed(sockets)
        denied = await self.client.post(self.terminal("/command/login", grant), headers=self.host, json={"auth_token": grant["token"]})
        self.assertEqual(denied.status, 401)

    async def test_grants_outlive_the_old_idle_and_absolute_limits(self):
        with self.fake_clock() as now:
            grant = await self.grant(ttl=60, idle=60)  # Time limits are the operator's, never the client's to set.
            issued = self.broker.store.grant(grant["id"])["issued"]
            self.assertEqual((grant["expires"], grant["permanent"]), (issued + PERMANENT_EXPIRES, True))
            response = await self.client.post(self.terminal("/command/login", grant), headers=self.host | {"Origin": self.ORIGIN},
                                              json={"auth_token": grant["token"]})
            self.assertEqual(int(response.cookies["session_token"]["max-age"]), COOKIE_AGE)
            cookie = response.cookies["session_token"].value
            sockets = await self.sockets(grant) + await self.sockets(grant, True)
            # No input for longer than the old idle limit, then past the old absolute cap.
            for advance in (OLD_IDLE + 60, OLD_CAP):
                now[0] += advance
                await asyncio.sleep(1.2)  # More than two policy sweeps.
                for ws in sockets[1::2]:
                    await self.echoes(ws, "still-here")
            self.assertIsNone(self.broker.store.grant(grant["id"])["revoked"])
            state = await (await self.client.get("/api/state", headers=self.host | self.owner)).json()
            self.assertEqual([(g["id"], g["expires"], g["permanent"], g["idle"], g["revoked"]) for g in state["grants"]],
                             [(grant["id"], issued + PERMANENT_EXPIRES, True, None, None)])
            # A client comparing expires with the clock on reconnect must neither fail nor end the grant.
            current = state["grants"][0]
            self.assertFalse(current["revoked"] or time.time() >= current["expires"])
            # The page Zellij's client reloads to reconnect stays signed in, and renews the cookie.
            page = await self.client.get(self.terminal("/main"), headers=self.host | {"Cookie": "session_token=" + cookie})
            self.assertNotIn('type="password"', await page.text())
            self.assertEqual((page.cookies["session_token"].value, int(page.cookies["session_token"]["max-age"])),
                             (cookie, COOKIE_AGE))
            self.assertEqual(page.cookies["session_token"]["path"], self.terminal("/", grant))
            for ws in sockets:
                await ws.close()

    async def test_terminal_page_reloads_itself_when_its_load_fails(self):
        grant = await self.grant()
        cookie = await self.login(grant, True)
        page = await self.client.get(self.terminal("/main"), headers=self.host | {"Cookie": "session_token=" + cookie})
        self.assertEqual(page.status, 200)
        # The guard comes first in <head>, so it sees every asset of the page fail or load.
        self.assertEqual(await page.read(), b'<!doctype html><html><head>' + RELOAD_GUARD + ICON_LINKS.encode() + LINK_CLEANUP + MOBILE_SCRIPT +
                         FULLSCREEN_SCRIPT + b'<script src="assets/xterm.js"></script></head></html>')
        self.assertNotIn(b"rt-page-retries", await (await self.client.get(self.terminal("/assets/owner.js"))).read())

    async def test_terminal_page_loads_phone_copy_and_paste_under_its_grant(self):
        grant = await self.grant()
        cookie = await self.login(grant, True)
        page = await self.client.get(self.terminal("/main"), headers=self.host | {"Cookie": "session_token=" + cookie})
        self.assertIn(MOBILE_SCRIPT, await page.read())
        # Relative, so the page loads it from under its own grant path, with the rest of the page.
        self.assertEqual(MOBILE_SCRIPT, b'<script src="./_rt_mobile.js" defer></script>')
        script = await self.client.get(self.terminal("/_rt_mobile.js"), headers=self.host)
        self.assertEqual((script.status, script.content_type), (200, "application/javascript"))
        self.assertEqual(await script.text(), MOBILE_JS.read_text())
        # Only Zellij's page gains it: the machine's own assets pass through unchanged.
        self.assertNotIn(b"_rt_mobile.js", await (await self.client.get(self.terminal("/assets/owner.js"))).read())

    async def test_terminal_page_loads_the_full_screen_button_under_its_grant(self):
        grant = await self.grant()
        cookie = await self.login(grant, True)
        page = await self.client.get(self.terminal("/main"), headers=self.host | {"Cookie": "session_token=" + cookie})
        self.assertIn(FULLSCREEN_SCRIPT, await page.read())
        self.assertEqual(FULLSCREEN_SCRIPT, b'<script src="./_rt_fullscreen.js" defer></script>')
        script = await self.client.get(self.terminal("/_rt_fullscreen.js"), headers=self.host)
        self.assertEqual((script.status, script.content_type), (200, "application/javascript"))
        self.assertEqual(await script.text(), FULLSCREEN_JS.read_text())
        self.assertNotIn(b"_rt_fullscreen.js", await (await self.client.get(self.terminal("/assets/owner.js"))).read())

    @unittest.skipUnless(shutil.which("node"), "needs Node.js")
    def test_reload_guard_backs_off_and_gives_up(self):
        # Each load runs the guard in a fresh page sharing one tab's sessionStorage, then fails its script (or
        # rejects a fetch, which is not the guard's to retry) and runs the page's timers to the 30 second reset.
        script = RELOAD_GUARD.decode().removeprefix("<script>").removesuffix("</script>")
        driver = """
const vm = require('vm'), storage = new Map(), reloads = [];
class HTMLScriptElement {} class HTMLLinkElement {}
const load = (fail, rejection) => {
  const listeners = {}, timers = [];
  let reloaded = false;
  vm.runInNewContext(SCRIPT, {
    HTMLScriptElement, HTMLLinkElement, Math,
    sessionStorage: {getItem: k => storage.has(k) ? storage.get(k) : null, setItem: (k, v) => storage.set(k, String(v)),
                     removeItem: k => storage.delete(k)},
    location: {reload: () => { reloaded = true; }},
    addEventListener: (type, listener) => { (listeners[type] ||= []).push(listener); },
    setTimeout: (callback, delay) => timers.push([delay, callback]),
  });
  if (fail) for (const listener of listeners.error || []) listener({target: new HTMLScriptElement()});
  if (rejection) for (const listener of listeners.unhandledrejection || []) listener({reason: new TypeError('Failed to fetch')});
  for (const [delay, callback] of timers.sort((a, b) => a[0] - b[0])) { if (!reloaded) callback(); if (reloaded) return delay; }
  return null;
};
const run = steps => steps.map(([fail, rejection]) => load(fail, rejection));
console.log(JSON.stringify([run(Array(6).fill([true, false])), run([[false, false], [true, false], [false, true]])]));
""".replace("SCRIPT", json.dumps(script))
        result = subprocess.run(["node", "-e", driver], capture_output=True, text=True, check=True)
        failing, recovered = json.loads(result.stdout)
        self.assertEqual(failing, [1000, 2000, 4000, 8000, 16000, None])
        self.assertEqual(recovered, [None, 1000, None])

    async def test_dropped_socket_reconnects_to_the_same_session(self):
        with self.fake_clock() as now:
            grant = await self.grant()
            cookie = await self.login(grant, True)
            headers = self.host | {"Cookie": "session_token=" + cookie, "Origin": self.ORIGIN}
            ws = await self.client.ws_connect(self.terminal("/ws/terminal/main"), headers=headers)
            await self.echoes(ws, "before")
            # The connection drops without a close frame: a network change, a laptop asleep, a proxy timeout.
            for transport in self.broker.sockets[grant["id"]].values():
                transport.close()
            await self.closed([ws])
            now[0] += OLD_CAP + OLD_IDLE  # Asleep past every old limit.
            await asyncio.sleep(1.2)
            # The same cookie reconnects to the same session; nobody issues or signs in to a new grant.
            ws = await self.client.ws_connect(self.terminal("/ws/terminal/main"), headers=headers)
            await self.echoes(ws, "after")
            await ws.close()
            self.assertEqual([path for path, data in self.upstream_messages if data in ("before", "after")],
                             ["/ws/terminal/main", "/ws/terminal/main"])
            self.assertEqual(len(self.broker.store.all("SELECT id FROM grants")), 1)
            self.assertEqual(self.broker.store.one("SELECT COUNT(*) AS n FROM cookies")["n"], 1)
            self.assertIsNone(self.broker.store.grant(grant["id"])["revoked"])

    async def test_sign_ins_never_run_out_and_evict_the_least_recently_used(self):
        with self.fake_clock() as now:
            grant = await self.grant()

            async def terminal(cookie):
                return await self.client.ws_connect(self.terminal("/ws/terminal/main"), headers=self.host | {"Cookie": "session_token=" + cookie})

            async def asset(cookie):
                return (await self.client.get(self.terminal("/assets/owner.js"), headers=self.host | {"Cookie": "session_token=" + cookie})).status

            def used(cookie):
                return self.broker.store.one("SELECT used FROM cookies WHERE hash=?", (digest(cookie),))["used"]
            first = await self.login(grant)
            idle = await self.login(grant)
            idle_ws = await terminal(idle)  # Open but never typed in: older than every other sign-in.
            typing = await self.login(grant)
            typing_ws = await terminal(typing)
            kept = await self.login(grant)
            for _ in range(MAX_COOKIES + 8):
                now[0] += 1
                await self.echoes(typing_ws, "typing")  # Terminal input is use, without any new request.
                self.assertEqual(used(typing), now[0])
                now[0] += 1
                self.assertEqual(await asset(kept), 200)
                now[0] += 1
                latest = await self.login(grant)
                self.assertLessEqual(self.broker.store.one("SELECT COUNT(*) AS n FROM cookies WHERE grant_id=?", (grant["id"],))["n"], MAX_COOKIES)
            self.assertLess(used(idle), min(used(typing), used(kept)))  # Kept only for its open socket.
            self.assertEqual((await asset(first), await asset(idle), await asset(typing), await asset(kept)), (401, 200, 200, 200))
            await self.echoes(idle_ws, "idle-still-attached")
            await self.echoes(typing_ws, "typing-still-attached")
            ws = await terminal(latest)
            await self.echoes(ws, "session-continues")
            for socket in (ws, idle_ws, typing_ws):
                await socket.close()
            self.assertIsNone(self.broker.store.grant(grant["id"])["revoked"])
            audit = self.broker.store.all("SELECT * FROM audit WHERE event='login-evicted'")
            self.assertEqual(len(audit), 4 + MAX_COOKIES + 8 - MAX_COOKIES)
            self.assertEqual({r["grant_id"] for r in audit}, {grant["id"]})
            values = [r["hash"] for r in self.broker.store.all("SELECT hash FROM cookies")] + [digest(first), first, idle, typing, kept, latest]
            self.assertFalse(any(c in json.dumps([dict(r) for r in audit]) for c in values))

    async def test_input_is_still_recorded_without_an_idle_limit(self):
        with self.fake_clock() as now:
            grant = await self.grant()
            sockets = await self.sockets(grant)
            now[0] += OLD_IDLE * 4
            await sockets[0].send_str('{"payload":{"type":"TerminalResize"}}')  # Control traffic is no input.
            await sockets[0].receive()
            self.assertLess(self.broker.store.grant(grant["id"])["last_input"], now[0] - OLD_IDLE)
            await self.echoes(sockets[1], "input")
            self.assertEqual(self.broker.store.grant(grant["id"])["last_input"], now[0])
            for ws in sockets:
                await ws.close()

    async def test_operator_revoke_closes_sockets_at_once(self):
        first, second, other = await self.grant(), await self.grant(), await self.grant(session="other")
        sockets = [await self.sockets(g) for g in (first, second, other)]
        listed = self.admin("list")
        self.assertTrue(all(g["id"] in listed for g in (first, second, other)))
        self.assertIn("never (revoke ends it)", listed)
        # One grant.
        self.assertIn(first["id"], self.admin("revoke", "--grant", first["id"]))
        await self.closed(sockets[0], within=2)
        await self.echoes(sockets[1][1], "unaffected")
        # One session name, then every open grant.
        self.admin("revoke", "--all", "--session", "other")
        await self.closed(sockets[2], within=2)
        await self.echoes(sockets[1][1], "unaffected")
        self.admin("revoke", "--all")
        await self.closed(sockets[1], within=2)
        self.assertEqual([self.broker.store.grant(g["id"])["revoked"] for g in (first, second, other)], ["operator-revoked"] * 3)
        denied = await self.client.post(self.terminal("/command/login", second), headers=self.host, json={"auth_token": second["token"]})
        self.assertEqual(denied.status, 401)
        events = [(r["actor"], r["event"]) for r in self.broker.store.all("SELECT * FROM audit WHERE grant_id=?", (other["id"],))]
        self.assertIn(("operator", "operator-revoked"), events)
        # The machine: disabled with all its grants, the open ones closed at once.
        grant = await self.grant()
        sockets = await self.sockets(grant)
        self.assertIn("m_test", self.admin("machines"))
        self.admin("machine-revoke", "--machine", "m_test")
        await self.closed(sockets, within=2)
        self.assertEqual(self.broker.store.grant(grant["id"])["revoked"], "machine-revoked")
        self.assertEqual(self.broker.store.one("SELECT enabled FROM machines WHERE id='m_test'")["enabled"], 0)

    async def test_operator_limits_are_opt_in_and_end_grants_when_set(self):
        with self.fake_clock() as now:
            self.broker.store.limits = {"idle": 600, "lifetime": 3600}
            grant = await self.grant()
            self.assertEqual((grant["expires"], grant["permanent"]), (self.broker.store.grant(grant["id"])["issued"] + 3600, False))
            response = await self.client.post(self.terminal("/command/login", grant), headers=self.host, json={"auth_token": grant["token"]})
            self.assertLessEqual(int(response.cookies["session_token"]["max-age"]), 3600)
            sockets = await self.sockets(grant)
            # Output and control traffic are no input; terminal input defers the idle limit but not the lifetime.
            for _ in range(7):
                now[0] += 500
                await sockets[0].send_str('{"payload":{"type":"TerminalResize"}}')
                await sockets[0].receive()
                await self.echoes(sockets[1], "input")
            now[0] += 200  # 3700 seconds after issuance, 200 after the last input.
            await self.closed(sockets)
            self.assertEqual(self.broker.store.grant(grant["id"])["revoked"], "expired")
            idle = await self.grant()
            sockets = await self.sockets(idle)
            now[0] += 601
            await self.closed(sockets)
            self.assertEqual(self.broker.store.grant(idle["id"])["revoked"], "idle")

    async def test_cookie_origin_and_anonymous_access(self):
        grant = await self.grant()
        cookie = await self.login(grant, True)
        for origin in (None, *self.FOREIGN_ORIGINS):
            headers = self.host | {"Cookie": "session_token=" + cookie, "Upgrade": "websocket"}
            if origin is not None:
                headers["Origin"] = origin
            response = await self.client.get(self.terminal("/ws/control"), headers=headers)
            self.assertEqual(response.status, 403, origin)
            self.assertEqual(response.headers["Strict-Transport-Security"], self.HSTS)
        for path in ("/ws/control", "/session-list"):
            response = await self.client.get(self.terminal(path), headers=self.host)
            self.assertEqual(response.status, 401)
        response = await self.client.get("/info/version", headers={"Host": "evil.test"})
        self.assertEqual(response.status, 403)

    async def test_backend_renewal_does_not_extend_grant(self):
        grant = await self.grant()
        cookie = await self.login(grant)
        self.broker.store.execute("UPDATE cookies SET renewed=0 WHERE hash=?", (digest(cookie),))
        before = self.broker.store.grant(grant["id"])["expires"]
        response = await self.client.post(self.terminal("/session?session=main"), headers=self.host | {"Cookie": "session_token=" + cookie})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.backend_logins, 2)
        self.assertEqual(self.broker.store.grant(grant["id"])["expires"], before)

    def restart_agent(self):
        self.agent_tokens.clear()
        self.zellij_valid.clear()

    async def test_login_after_agent_restart_remints_sealed_token(self):
        grant = await self.grant()
        stale = self.broker.store.grant(grant["id"])["backend_token"]
        self.restart_agent()
        await self.login(grant)
        fresh = self.broker.store.grant(grant["id"])
        self.assertEqual(fresh["backend_name"], "token_2")
        self.assertNotIn(stale, self.zellij_valid)
        self.assertEqual(self.zellij_valid, {fresh["backend_token"]})
        for path in (Path(self.temp.name) / "data").glob("db*"):
            self.assertNotIn(fresh["backend_token"].encode(), path.read_bytes(), path.name)
        # The broker reaches the agent's new mapping when it cleans up.
        response = await self.client.post("/api/revoke", headers=self.host | self.owner, json={"id": grant["id"]})
        self.assertEqual(response.status, 200)
        for _ in range(30):
            if self.broker.store.grant(grant["id"])["cleaned"]:
                break
            await asyncio.sleep(0.2)
        self.assertEqual(self.zellij_valid, set())

    async def test_refresh_after_agent_restart_remints_once(self):
        grant = await self.grant()
        cookies = [await self.login(grant), await self.login(grant)]
        self.restart_agent()
        for cookie in cookies:
            self.broker.store.execute("UPDATE cookies SET renewed=0 WHERE hash=?", (digest(cookie),))
        responses = await asyncio.gather(*(self.client.post(self.terminal("/session?session=main"), headers=self.host | {"Cookie": "session_token=" + c})
                                           for c in cookies))
        self.assertEqual([r.status for r in responses], [200, 200])
        self.assertEqual(self.minted, 2)
        self.assertEqual(self.zellij_valid, {self.broker.store.grant(grant["id"])["backend_token"]})

    async def test_recent_cookie_reattaches_after_agent_restart(self):
        grant = await self.grant()
        cookie = await self.login(grant, browser=True)
        stale = self.broker.store.grant(grant["id"])["backend_token"]
        self.restart_agent()
        headers = self.host | {"Cookie": "session_token=" + cookie, "Origin": self.ORIGIN}
        for path in ("/ws/control", "/ws/terminal/main"):
            ws = await self.client.ws_connect(self.terminal(path), headers=headers)
            await ws.send_str("after-restart")
            self.assertEqual((await ws.receive()).data, "after-restart")
            await ws.close()
        response = await self.client.post(self.terminal("/session?session=main"), headers=headers)
        self.assertEqual(response.status, 200)
        response = await self.client.get(self.terminal("/session-list"), headers=headers)
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["sessions"], [{"name": "main"}])
        self.assertEqual((self.minted, self.backend_logins), (2, 3))
        self.assertEqual(self.zellij_valid, {self.broker.store.grant(grant["id"])["backend_token"]})
        self.assertNotIn(stale, self.zellij_valid)
        for _ in range(30):
            events = [r["event"] for r in self.broker.store.all("SELECT event FROM audit WHERE grant_id=?", (grant["id"],))]
            if events.count("detach") >= 2:
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.2)
        events = [r["event"] for r in self.broker.store.all("SELECT event FROM audit WHERE grant_id=?", (grant["id"],))]
        self.assertEqual((events.count("attach"), events.count("detach")), (2, 2))

    async def test_concurrent_attach_after_agent_restart_refreshes_once(self):
        grant = await self.grant()
        cookie = await self.login(grant)
        self.restart_agent()
        headers = self.host | {"Cookie": "session_token=" + cookie}
        responses = await asyncio.gather(*(self.client.post(self.terminal("/session?session=main"), headers=headers) for _ in range(3)))
        self.assertEqual([r.status for r in responses], [200, 200, 200])
        self.assertEqual((self.minted, self.backend_logins), (2, 3))

    async def test_enrollment_single_use_and_owner_required(self):
        response = await self.client.post("/api/enrollment", headers=self.host | self.owner, json={})
        code = (await response.json())["code"]
        response = await self.client.post("/api/enroll", headers=self.host, json={"code": code})
        self.assertEqual(response.status, 200)
        config = await response.json()
        self.assertEqual(config["domain"], config["id"] + ".rt.internal")
        # The agent is bound to this environment's relay and nothing else.
        self.assertEqual((config["relay_host"], config["relay_port"], config["environment"], config["origin"]),
                         (self.ENV["RT_RELAY_HOST"].lower(), int(self.ENV.get("RT_PUBLIC_PORT", 443)),
                          self.ENV["RT_ENVIRONMENT"], self.ORIGIN))
        response = await self.client.post("/api/enroll", headers=self.host, json={"code": code})
        self.assertEqual(response.status, 401)
        for path in ("/api/grants", "/api/enrollment", "/api/state"):
            response = await self.client.post(path, headers=self.host, json={"session": "main"})
            self.assertEqual(response.status, 401, path)
            response = await self.client.post(path, headers=self.host | {"Authorization": "Bearer " + "x" * 40},
                                              json={"session": "main"})
            self.assertEqual(response.status, 401, path)
        self.assertEqual((await self.client.get("/api/state", headers=self.host | {"Authorization": "Bearer é"})).status, 401)

    async def test_relay_routes_bound_to_authenticated_login(self):
        async def hook(op, content):
            response = await self.private.post("/_frp", json={"op": op, "content": content})
            return await response.json()
        denied = await hook("Login", {"metas": {"machine": "m_test", "credential": "wrong"}})
        self.assertTrue(denied["reject"])
        login = await hook("Login", {"metas": {"machine": "m_test", "credential": "m" * 40}})
        run = login["content"]["run_id"]
        valid = {"user": {"run_id": run}, "proxy_name": "m_test.zellij", "proxy_type": "http",
                 "custom_domains": ["m_test.rt.internal"]}
        self.assertFalse((await hook("NewProxy", valid))["reject"])
        for changes in ({"proxy_type": "tcp", "remote_port": 9000}, {"custom_domains": ["other.test"]},
                        {"proxy_name": "another.zellij"}, {"host_header_rewrite": "admin"},
                        {"locations": ["/restricted"]}, {"user": {"run_id": "forged"}}):
            self.assertTrue((await hook("NewProxy", valid | changes))["reject"])
        self.broker.store.execute("UPDATE machines SET enabled=0")
        self.assertTrue((await hook("Ping", {"user": {"run_id": run}}))["reject"])

    async def test_assets_preserve_complete_chunked_body(self):
        grant = await self.grant()
        cookie = await self.login(grant)
        response = await self.client.get(self.terminal("/assets/large.js"), headers=self.host | {"Cookie": "session_token=" + cookie})
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), b"x" * 80000)

    async def test_each_terminal_path_uses_only_its_grants_machine(self):
        self.add_machine("m_bob", "b" * 40, name="desktop")
        first = await self.grant(machine="m_test")
        response = await self.client.post("/api/grants", headers=self.host | self.owner, json={"session": "main", "machine": "desktop"})
        self.assertEqual(response.status, 200)
        second = await response.json()
        self.assertEqual((first["machine"], second["machine"]), ("m_test", "m_bob"))
        # Both machines' terminals are the owner's, on the owner's terminal origin.
        self.assertEqual(second["url"], self.owner_origin("owner") + self.terminal("/main", second))
        cookie = await self.login(second, browser=True)
        headers = self.host | {"Cookie": "session_token=" + cookie, "Origin": self.ORIGIN}
        self.agent_requests.clear()
        # Revoking the other machine cannot affect this grant's page or sockets.
        response = await self.client.post("/api/machine-revoke", headers=self.host | self.owner, json={"machine": "m_test"})
        self.assertEqual((response.status, (await response.json())["machine"]), (200, "m_test"))
        self.assertEqual(self.broker.store.grant(first["id"])["revoked"], "machine-revoked")
        for path in ("/", "/main", "/assets/owner.js", "/info/version", "/session-list"):
            response = await self.client.get(self.terminal(path, second), headers=headers)
            self.assertEqual(response.status, 200, path)
            if path == "/assets/owner.js":
                self.assertEqual(await response.text(), "m_bob")
        response = await self.client.post(self.terminal("/session?session=main", second), headers=headers)
        self.assertEqual(response.status, 200)
        for path in ("/ws/control", "/ws/terminal/main"):
            ws = await self.client.ws_connect(self.terminal(path, second), headers=headers)
            await ws.send_str("private")
            self.assertEqual((await ws.receive()).data, "bob:private")
            await ws.close()
        self.assertTrue(self.agent_requests)
        self.assertTrue(all(mid == "m_bob" for mid, _ in self.agent_requests))
        # A cookie of one grant opens nothing of the other machine's grant.
        response = await self.client.get(self.terminal("/assets/owner.js", first), headers=headers)
        self.assertEqual(response.status, 401)

    async def test_the_owners_terminal_has_its_own_origin(self):
        grant = await self.grant()
        owner_host = urlsplit(grant["url"]).netloc
        self.assertEqual(owner_host, urlsplit(self.owner_origin("owner")).netloc)
        cookie = await self.login(grant, browser=True)
        # The terminal origin only: the shared public origin, or another owner's, is refused.
        for origin in (self.owner_origin("bob"), self.ORIGIN):
            response = await self.client.get(self.terminal("/session-list", grant),
                                             headers={"Host": owner_host, "Cookie": "session_token=" + cookie, "Origin": origin})
            self.assertEqual(response.status, 403, origin)
        with self.assertRaises(WSServerHandshakeError):
            await self.client.ws_connect(self.terminal("/ws/terminal/main", grant), headers={"Host": owner_host, "Cookie": "session_token=" + cookie})
        # Grants are never served on the shared public host, and the owner API never on the terminal host.
        response = await self.client.get(self.terminal("/assets/owner.js", grant), headers={
            "Host": self.HOST, "Cookie": "session_token=" + cookie, "X-Test-Public-Host": "1"})
        self.assertEqual(response.status, 404)
        self.assertEqual((await self.client.get("/api/state", headers={"Host": owner_host} | self.owner)).status, 404)
        # Any other owner's host is no host of this server.
        bob_host = urlsplit(self.owner_origin("bob")).netloc
        for host in (bob_host, "evil." + owner_host, "-x." + self.HOST.split(":")[0], "owner." + self.ENV["RT_RELAY_HOST"].lower()):
            self.assertEqual((await self.client.get("/", headers={"Host": host})).status, 403, host)
        response = await self.client.get(self.terminal("/session-list", grant), headers={
            "Host": bob_host, "Cookie": "session_token=" + cookie})
        self.assertEqual(response.status, 403)
        response = await self.client.get(self.terminal("/session-list", grant), headers={"Host": owner_host, "Cookie": "session_token=" + cookie})
        self.assertEqual(response.status, 200)

    async def test_anonymous_pages_are_broker_owned_and_cookie_cannot_cross_grants(self):
        first, second = await self.grant(), await self.grant()
        cookie = await self.login(first)
        self.agent_requests.clear()
        for path in ("/", "/main", self.terminal("/main", second)):
            response = await self.client.get(path, headers=self.host)
            self.assertEqual(response.status, 200)
            self.assertIn("Rootkeel", await response.text())
            self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])
        for path in ("/assets/owner.js", "/info/version", "/session-list", "/ws/control"):
            response = await self.client.get(self.terminal(path, second), headers=self.host)
            self.assertEqual(response.status, 401)
            response = await self.client.get(self.terminal(path, second), headers=self.host | {"Cookie": "session_token=" + cookie})
            self.assertEqual(response.status, 401)
        for path in ("/assets/owner.js", "/info/version", "/ws/control"):
            self.assertEqual((await self.client.get(path, headers=self.host | {"Cookie": "session_token=" + cookie})).status, 401)
        response = await self.client.post(self.terminal("/command/login", second), headers=self.host,
                                          json={"auth_token": first["token"]})
        self.assertEqual(response.status, 401)
        self.assertEqual(self.agent_requests, [])

    async def test_native_explicit_default_port_is_accepted(self):
        grant = await self.grant()
        cookie = await self.login(grant)
        ws = await self.client.ws_connect(self.terminal("/ws/control"), headers={"Host": self.PORT_HOST, "Cookie": "session_token=" + cookie})
        await ws.send_str("port-proof")
        self.assertEqual((await ws.receive()).data, "port-proof")
        await ws.close()

    async def test_unknown_session_name_refused_unless_creation_is_asked(self):
        minted = self.minted
        for body, status in (({"session": "nosuch"}, 404), ({"session": "nosuch", "create": "yes"}, 400),
                             ({"session": "nosuch", "create": False}, 404)):
            response = await self.client.post("/api/grants", headers=self.host | self.owner, json=body)
            self.assertEqual(response.status, status, body)
            if status == 404:
                self.assertEqual(await response.text(), "No session nosuch on machine m_test. To start a new session "
                                 "named nosuch, ask for it with --create (rt attach, rt grant). Existing sessions: main, other.")
        self.assertEqual((self.minted, self.broker.store.all("SELECT id FROM grants")), (minted, []))
        response = await self.client.post("/api/grants", headers=self.host | self.owner, json={"session": "nosuch", "create": True})
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["url"].endswith("/nosuch"))
        self.sessions = []
        response = await self.client.post("/api/grants", headers=self.host | self.owner, json={"session": "main"})
        self.assertIn("Existing sessions: none.", await response.text())
        response = await self.client.post("/api/revoke", headers=self.host | self.owner, json={"id": "g_" + "f" * 24})
        self.assertEqual((response.status, await response.text()), (404, "No such session grant"))
        # Grants do not expire, so there is nothing to renew.
        response = await self.client.post("/api/renew", headers=self.host | self.owner, json={"id": "g_" + "f" * 24})
        self.assertEqual(response.status, 404)

    async def test_many_long_session_names_leave_the_create_hint_first(self):
        self.sessions = [f"{i:02}" + "s" * 62 for i in range(30)]
        response = await self.client.post("/api/grants", headers=self.host | self.owner, json={"session": "n" * 64})
        text = await response.text()
        self.assertEqual(response.status, 404)
        self.assertIn("ask for it with --create (rt attach, rt grant).", text[:300])
        self.assertTrue(text.endswith(", ".join(self.sessions[:10]) + "."))

    async def test_an_unreachable_machine_or_an_agent_below_the_supported_protocol_is_refused(self):
        self.assertEqual((broker.MIN_AGENT_PROTOCOL, AGENT_PROTOCOL), (1, 1))
        for state in ("offline", "old-helper"):
            self.agent_state = state
            response = await self.client.post("/api/grants", headers=self.host | self.owner, json={"session": "main"})
            self.assertEqual((response.status, await response.text()), (503, "Machine unavailable"), state)
        self.agent_state = "current"
        # A later broker that no longer serves protocol 1 refuses it with how to update, and starts nothing.
        minted, grants = self.minted, len(self.broker.store.all("SELECT id FROM grants"))
        self.broker.store.execute("INSERT INTO relay_logins(run_id,machine,protocol) VALUES('run-1','m_test',1)")
        with patch.object(broker, "MIN_AGENT_PROTOCOL", 2):
            for body in ({"session": "main"}, {"session": "new", "create": True}):
                response = await self.client.post("/api/grants", headers=self.host | self.owner, json=body)
                self.assertEqual((response.status, await response.text()), (409, "Machine m_test runs an older agent "
                                 "(protocol 1) than this broker supports (2 or later); update its agent"))
        self.assertEqual((self.minted, len(self.broker.store.all("SELECT id FROM grants"))), (minted, grants))
        response = await self.client.post("/api/grants", headers=self.host | self.owner, json={"session": "nosuch"})
        self.assertEqual(response.status, 404)

    async def test_relay_login_records_the_announced_agent_protocol(self):
        async def login(metas):
            response = await self.private.post("/_frp", json={"op": "Login", "content": {"metas": metas}})
            return await response.json()
        credential = {"machine": "m_test", "credential": "m" * 40}
        protocol = lambda: self.broker.store.one("SELECT protocol FROM relay_logins WHERE machine='m_test'")["protocol"]
        self.assertIn("run_id", (await login(credential | {"protocol": "2"}))["content"])
        self.assertEqual(protocol(), 2)
        for junk in ({}, {"protocol": "two"}, {"protocol": "0"}, {"protocol": "-1"}, {"protocol": "99999"}):
            self.assertIn("run_id", (await login(credential | junk))["content"])
            self.assertIsNone(protocol(), junk)
        self.assertTrue((await login({"machine": "m_test", "credential": "wrong", "protocol": "2"}))["reject"])

    async def test_terminal_text_split_never_ends_inside_a_character(self):
        grant = await self.grant()
        control, terminal = await self.sockets(grant)
        # Alt+Shift+O and then č in one read: SS3's one key is the whole character.
        await terminal.send_str("\x1bOčx\x1bčšž\r")
        for _ in range(5):
            await terminal.receive()
        self.assertEqual([data for path, data in self.upstream_messages if path == "/ws/terminal/main"],
                         ["\x1bOč", "x", "\x1bč", "šž", "\r"])

    async def test_terminal_input_reaches_zellij_one_key_per_message(self):
        grant = await self.grant()
        control, terminal = await self.sockets(grant)
        await terminal.send_bytes(b"One Two\rthree\r\x1b[200~pasted\rtext\x1b[201~\x1b\x07d")
        await terminal.send_str("ls\r")
        await control.send_str('{"payload":"a\\rb"}')
        self.assertEqual((await control.receive()).data, '{"payload":"a\\rb"}')
        for _ in range(2):
            self.assertIn((await terminal.receive()).data, ("ls", "\r"))
        self.assertEqual([data for path, data in self.upstream_messages if path == "/ws/terminal/main"], [
            b"One Two", b"\r", b"three", b"\r", b"\x1b[200~", b"pasted", b"\r", b"text", b"\x1b[201~", b"\x1b\x07", b"d",
            "ls", "\r"])
        self.assertEqual([data for path, data in self.upstream_messages if path == "/ws/control"], ['{"payload":"a\\rb"}'])

    async def test_zellij_close_reaches_the_client_but_a_drop_sends_none(self):
        grant = await self.grant()
        control, terminal = await self.sockets(grant)
        await control.send_str("detach")
        closed = await asyncio.wait_for(control.receive(), 3)
        self.assertEqual((closed.type, closed.data), (WSMsgType.CLOSE, 1000))
        await terminal.close()
        control, terminal = await self.sockets(grant)
        response = await self.client.post("/api/revoke", headers=self.host | self.owner, json={"id": grant["id"]})
        self.assertEqual(response.status, 200)
        for ws in (control, terminal):
            self.assertIn((await asyncio.wait_for(ws.receive(), 3)).type, (WSMsgType.CLOSED, WSMsgType.ERROR))

    async def test_terminal_socket_requires_granted_session_name(self):
        grant = await self.grant()
        cookie = await self.login(grant)
        headers = self.host | {"Cookie": "session_token=" + cookie, "Upgrade": "websocket"}
        for path, status in (("/ws/terminal", 404), ("/ws/terminal/", 403), ("/ws/terminal/other", 403)):
            response = await self.client.get(self.terminal(path), headers=headers)
            self.assertEqual(response.status, status, path)
        self.assertEqual(self.upstream_sockets, [])

    async def test_cleanup_progresses_past_revoked_machine_grants(self):
        store = self.broker.store
        store.execute("INSERT INTO machines(id,secret,enabled,owner) VALUES('m_old',?,0,'owner')", (store.seal("x" * 40, "machines.secret", "m_old"),))
        for n in range(6):
            store.execute("INSERT INTO grants(id,hash,machine,session,issued,expires,idle,last_input,revoked,"
                          "backend_name,backend_token) VALUES(?,?,'m_old','main',0,1,900,0,'machine-revoked',?,?)",
                          (f"g_old{n}", f"h{n}", f"token_{n}", store.seal("t", "grants.backend_token", f"g_old{n}")))
        grant = await self.grant()
        response = await self.client.post("/api/revoke", headers=self.host | self.owner, json={"id": grant["id"]})
        self.assertEqual(response.status, 200)
        for _ in range(30):
            if store.grant(grant["id"])["cleaned"]:
                break
            await asyncio.sleep(0.2)
        self.assertEqual(store.grant(grant["id"])["cleaned"], 1)

    async def test_real_grant_login_and_enrollment_never_persist_plaintext(self):
        grant = await self.grant()
        cookie = await self.login(grant)
        # Includes live WAL pages, not only decoded SELECT output.
        for path in (Path(self.temp.name) / "data").glob("db*"):
            for secret in ("m" * 40, "backend-secret", "upstream-cookie", grant["token"], cookie):
                self.assertNotIn(secret.encode(), path.read_bytes(), path.name)
        self.broker.store.execute("UPDATE machines SET enabled=0")
        response = await self.client.post("/api/enrollment", headers=self.host | self.owner, json={})
        code = (await response.json())["code"]
        response = await self.client.post("/api/enroll", headers=self.host, json={"code": code})
        machine = await response.json()
        self.assertEqual(response.status, 200)
        for path in (Path(self.temp.name) / "data").glob("db*"):
            self.assertNotIn(machine["credential"].encode(), path.read_bytes(), path.name)

    async def test_store_retains_grants_across_reopen(self):
        grant = await self.grant()
        from rootkeel.store import Store
        second = Store(self.temp.name + "/data/db", self.key_path)
        self.assertEqual(second.grant(grant["id"])["session"], "main")
        second.db.close()
        audit = json.dumps([dict(r) for r in self.broker.store.all("SELECT * FROM audit")])
        for secret in (grant["token"], "backend-secret", "input", "m" * 40):
            self.assertNotIn(secret, audit)


    async def test_session_list_shows_last_activity_newest_first(self):
        first = await self.grant()
        await asyncio.sleep(0.01)
        second = await self.grant()
        state = await (await self.client.get("/api/state", headers=self.host | self.owner)).json()
        self.assertEqual([g["id"] for g in state["grants"]], [second["id"], first["id"]])
        self.assertEqual([g["last_active"] for g in state["grants"]], [None, None])
        self.current_grant = first
        sockets = await self.sockets(first)
        await sockets[0].send_str('{"payload":{"type":"TerminalResize"}}')  # Control traffic is no activity.
        await sockets[0].receive()
        self.assertIsNone(self.broker.store.one("SELECT CASE WHEN last_input>issued THEN 1 END AS a FROM grants WHERE id=?",
                                                (first["id"],))["a"])
        before = time.time()
        await sockets[1].send_str("typed")
        self.assertEqual((await sockets[1].receive()).data, "typed")
        for ws in sockets:
            await ws.close()
        rows = (await (await self.client.get("/api/state", headers=self.host | self.owner)).json())["grants"]
        active = {g["id"]: g["last_active"] for g in rows}
        self.assertIsNone(active[second["id"]])
        self.assertGreaterEqual(active[first["id"]], before - 1)
        self.assertTrue(all(isinstance(g["issued"], float) for g in rows))
        # Metadata only: no terminal content in the list.
        self.assertNotIn("typed", json.dumps(rows))

    async def test_authentication_attempts_are_limited_per_client(self):
        def attempt(client, via_front_door=False):
            request = SimpleNamespace(remote="10.0.0.2" if via_front_door else client,
                                      headers={"X-Forwarded-For": client} if via_front_door else {})
            return self.broker.client_address(request)
        for _ in range(broker.AUTH_ATTEMPTS):
            self.assertFalse(self.broker.rate_limited(await attempt("192.0.2.1")))
        self.assertTrue(self.broker.rate_limited(await attempt("192.0.2.1")))
        # Another client is not locked out by the first one's flood.
        self.assertFalse(self.broker.rate_limited(await attempt("192.0.2.2")))
        # An IPv6 client counts as its /64, so rotating addresses within it does not escape the limit.
        for index in range(broker.AUTH_ATTEMPTS):
            self.assertFalse(self.broker.rate_limited(await attempt(f"2001:db8::{index:x}")))
        self.assertTrue(self.broker.rate_limited(await attempt("2001:db8::ffff")))
        self.assertFalse(self.broker.rate_limited(await attempt("2001:db8:0:1::1")))
        # Only the front door names the client: from anywhere else X-Forwarded-For is ignored.
        self.broker.front_door = "localhost"
        with patch.object(asyncio.get_running_loop(), "getaddrinfo",
                          new=AsyncMock(return_value=[(None, None, None, "", ("10.0.0.2", 0))])):
            self.assertEqual(await attempt("192.0.2.9", via_front_door=True), "192.0.2.9")
            spoofed = SimpleNamespace(remote="192.0.2.3", headers={"X-Forwarded-For": "192.0.2.10"})
            self.assertEqual(await self.broker.client_address(spoofed), "192.0.2.3")
        # Memory stays bounded: the least recently seen clients are forgotten first.
        for index in range(broker.AUTH_CLIENTS + 10):
            self.broker.rate_limited(f"198.51.{index // 256}.{index % 256}")
        self.assertEqual(len(self.broker.attempts), broker.AUTH_CLIENTS)
        self.assertNotIn("192.0.2.1", self.broker.attempts)

    async def test_one_clients_flood_does_not_lock_out_another_over_http(self):
        path = "/g/g_" + "0" * 24 + "/command/login"
        flood = {"Host": self.HOST, "X-Forwarded-For": "192.0.2.50"}
        self.broker.front_door = "localhost"
        with patch.object(self.broker, "front_door_addresses", {"127.0.0.1", "::1"}), \
                patch.object(self.broker, "front_door_checked", float("inf")):
            for _ in range(broker.AUTH_ATTEMPTS):
                await self.client.post(path, headers=flood | {"X-Test-Public-Host": "1"}, json={"auth_token": "x"})
            response = await self.client.post(path, headers=flood | {"X-Test-Public-Host": "1"}, json={"auth_token": "x"})
            self.assertEqual(response.status, 429)
            response = await self.client.post("/api/enrollment", headers=self.host | self.owner | {"X-Forwarded-For": "192.0.2.51"},
                                              json={})
            self.assertEqual(response.status, 200)

    async def test_terminal_pages_are_the_brokers_own(self):
        from rootkeel.broker import SHELL_CSS
        grant = await self.grant()
        response = await self.client.get("/_rt.css", headers=self.host)
        self.assertEqual((response.status, response.content_type), (200, "text/css"))
        self.assertEqual(await response.text(), SHELL_CSS.read_text())
        for path, heading in (("/", "Open a terminal session"), (self.terminal("/main", grant), "Sign in to the terminal")):
            response = await self.client.get(path, headers=self.host)
            text = await response.text()
            self.assertIn('<link rel="stylesheet" href="/_rt.css">', text)
            self.assertIn('<header class="shell">', text)
            self.assertIn("<h1>" + heading + "</h1>", text)
            self.assertIn("rt grant", text)
            policy = response.headers["Content-Security-Policy"]
            self.assertIn("style-src 'self'", policy)
            self.assertIn("frame-ancestors 'none'", policy)
            self.assertNotIn("unsafe-inline", policy)
        script = await self.client.get(self.terminal("/_signin.js", grant), headers=self.host)
        self.assertEqual((script.status, script.content_type), (200, "application/javascript"))
        self.assertEqual(await script.text(), SIGNIN_JS.read_text())
        self.assertIn("./command/login", await script.text())

    async def test_sign_in_links_are_handled_on_every_grant_page(self):
        # A scanned rt grant --qr link carries the credential in its fragment; whatever the grant's state, the page
        # under its path loads the script that takes it out of the address bar. The owner's landing page has no link.
        loads = '<p role="status" aria-live="polite"></p><script src="./_signin.js" defer></script>'
        grant = await self.grant()
        open_page = await (await self.client.get(self.terminal("/main", grant), headers=self.host)).text()
        self.assertIn('<input type="password"', open_page)
        self.assertIn(loads, open_page)
        await self.client.post("/api/revoke", headers=self.host | self.owner, json={"id": grant["id"]})
        unknown = {"id": "g_" + "0" * 24}
        for path in (self.terminal("/main", grant), self.terminal("/main", unknown)):
            ended = await (await self.client.get(path, headers=self.host)).text()
            self.assertIn("<h1>Open a terminal session</h1>", ended)
            self.assertIn(loads, ended)
            script = await self.client.get(path.removesuffix("/main") + "/_signin.js", headers=self.host)
            self.assertEqual((script.status, await script.text()), (200, SIGNIN_JS.read_text()))
        self.assertNotIn("_signin.js", await (await self.client.get("/", headers=self.host)).text())

    @unittest.skipUnless(shutil.which("node"), "needs Node.js")
    def test_sign_in_link_signs_in_and_leaves_no_credential_in_the_address_bar(self):
        # Runs the sign-in script on a fake page: the form page or an ended grant's page (no form), a URL fragment,
        # and the server's answer to the sign-in request. Records the address bar at each step.
        driver = """
const vm = require('vm');
const load = async ({hash, form = true, answer = 401, submit, later}) => {
  const steps = [], listeners = {}, events = {}, status = {textContent: ''}, input = {value: ''};
  const location = {hash, pathname: '/g/g_x/main', search: '', reload: () => steps.push(['reload', location.hash])};
  const settle = async () => { for (let i = 0; i < 5; i++) await new Promise(resolve => setImmediate(resolve)); };
  vm.runInNewContext(SCRIPT, {
    URLSearchParams, JSON,
    location,
    addEventListener: (type, listener) => { events[type] = listener; },
    history: {state: null, replaceState: (state, title, url) => { steps.push(['address', url]); location.hash = ''; }},
    document: {querySelector: selector => selector === '[role=status]' ? status :
      selector === 'form' && form ? {addEventListener: (type, listener) => { listeners[type] = listener; }, querySelector: () => input} : null},
    fetch: async (url, options) => {
      steps.push(['fetch', url, JSON.parse(options.body).auth_token, location.hash]);
      if (answer === 'offline') throw new TypeError('Failed to fetch');
      return {ok: answer === 200};
    },
  });
  await settle();
  if (later !== undefined) {
    // The link opened again in this tab: only the fragment changes, and the page gets a hashchange, not a load.
    steps.push(['opened', later]);
    location.hash = later;
    events.hashchange();
    await settle();
  }
  if (submit !== undefined) {
    input.value = submit;
    await listeners.submit({preventDefault: () => steps.push(['prevented'])});
    steps.push(['input', input.value]);
  }
  return {steps, status: status.textContent};
};
(async () => console.log(JSON.stringify(await Promise.all([
  load({hash: '#token=secret-1', answer: 200}),
  load({hash: '#token=secret-2', answer: 401}),
  load({hash: '#token=secret-3', answer: 'offline'}),
  load({hash: '#token=secret-4', form: false}),
  load({hash: '', answer: 401, submit: 'typed'}),
  load({hash: '', answer: 'offline', submit: 'typed'}),
  load({hash: '#other', answer: 200}),
  load({hash: '', answer: 200, later: '#token=secret-5'}),
  load({hash: '', form: false, later: '#token=secret-6'}),
]))))();
""".replace("SCRIPT", json.dumps(SIGNIN_JS.read_text()))
        result = subprocess.run(["node", "-e", driver], capture_output=True, text=True, check=True)
        scanned, refused, offline, ended, typed, typed_offline, other, reopened, reopened_ended = json.loads(result.stdout)
        # The fragment leaves the address bar before the sign-in request; on success the page reloads into the terminal.
        self.assertEqual(scanned["steps"], [["address", "/g/g_x/main"], ["fetch", "./command/login", "secret-1", ""],
                                            ["reload", ""]])
        self.assertEqual(scanned["status"], "Signing in…")
        for case in (refused, offline):
            self.assertEqual(case["steps"][:2], [["address", "/g/g_x/main"], ["fetch", "./command/login", case["steps"][1][2], ""]])
            self.assertNotIn(["reload", ""], case["steps"])
        for case in (refused, offline):
            self.assertEqual(case["status"], "The scanned code did not sign in. Grant invalid or ended. Request a new session.")
        # An ended grant's page has no form: it still clears the address bar, and says why there is nothing to do.
        self.assertEqual(ended["steps"], [["address", "/g/g_x/main"]])
        self.assertEqual(ended["status"], "The scanned code did not sign in. Grant invalid or ended. Request a new session.")
        # The form works as before, and an unrelated fragment is dropped without a sign-in.
        self.assertEqual(typed["steps"], [["prevented"], ["fetch", "./command/login", "typed", ""], ["input", ""]])
        self.assertEqual(typed["status"], "Grant invalid or ended. Request a new session.")
        self.assertEqual(typed_offline["status"], "Grant invalid or ended. Request a new session.")
        self.assertEqual(other, {"steps": [["address", "/g/g_x/main"]], "status": ""})
        # A link opened again in an open tab changes only the fragment: it is cleared and used just as on a load.
        self.assertEqual(reopened["steps"], [["opened", "#token=secret-5"], ["address", "/g/g_x/main"],
                                             ["fetch", "./command/login", "secret-5", ""], ["reload", ""]])
        self.assertEqual(reopened_ended["steps"], [["opened", "#token=secret-6"], ["address", "/g/g_x/main"]])
        self.assertEqual(reopened_ended["status"], "The scanned code did not sign in. Grant invalid or ended. Request a new session.")

    @unittest.skipUnless(shutil.which("node"), "needs Node.js")
    def test_terminal_page_takes_a_sign_in_link_out_of_the_address_bar(self):
        # A browser already signed in opens the terminal straight away; the link's credential must not stay visible.
        script = LINK_CLEANUP.decode().removeprefix("<script>").removesuffix("</script>")
        driver = """
const vm = require('vm');
// Each page loads with one fragment, then the tab is given another (a link opened again in it): only a hashchange.
console.log(JSON.stringify([['#token=secret', ''], ['#t=1', ''], ['', '#token=again'], ['', '#t=2']].map(([hash, later]) => {
  const replaced = [], events = {}, location = {hash, pathname: '/g/g_x/main', search: '?a=1'};
  vm.runInNewContext(SCRIPT, {URLSearchParams, location, addEventListener: (type, listener) => { events[type] = listener; },
                              history: {state: 'kept', replaceState: (...args) => { replaced.push(args); location.hash = ''; }}});
  if (later) {
    location.hash = later;
    events.hashchange();
  }
  return [replaced, location.hash];
})));
""".replace("SCRIPT", json.dumps(script))
        result = subprocess.run(["node", "-e", driver], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), [[[["kept", "", "/g/g_x/main?a=1"]], ""], [[], "#t=1"],
                                                     [[["kept", "", "/g/g_x/main?a=1"]], ""], [[], "#t=2"]])


    # ----- several machines per owner ----------------------------------------

    async def enroll(self, name=None, status=200):
        response = await self.client.post("/api/enrollment", headers=self.host | self.owner,
                                          json={"name": name} if name else {})
        self.assertEqual(response.status, 200 if status != "issue" else 409, await response.text())
        if status == "issue":
            return await response.text()
        code = (await response.json())["code"]
        response = await self.client.post("/api/enroll", headers=self.host, json={"code": code})
        self.assertEqual(response.status, status, await response.text())
        return await response.json() if status == 200 else await response.text()

    async def test_several_machines_enroll_and_each_is_chosen_by_id_or_name(self):
        laptop, desktop = await self.enroll("laptop"), await self.enroll("desktop")
        self.assertEqual((laptop["name"], desktop["name"]), ("laptop", "desktop"))
        self.assertNotEqual(laptop["id"], desktop["id"])
        state = await (await self.client.get("/api/state", headers=self.host | self.owner)).json()
        self.assertEqual([(m["id"], m["name"], m["enabled"]) for m in state["machines"]],
                         [("m_test", None, True), (laptop["id"], "laptop", True), (desktop["id"], "desktop", True)])
        self.assertNotIn("secret", json.dumps(state["machines"]))
        # With several machines, a grant names one; the refusal lists them.
        response = await self.client.post("/api/grants", headers=self.host | self.owner, json={"session": "main"})
        self.assertEqual((response.status, await response.text()),
                         (409, "Several machines are enrolled; choose one with --machine: m_test, laptop, desktop"))
        response = await self.client.get("/api/version", headers=self.host | self.owner)
        self.assertEqual(response.status, 409)
        for machine, expected in (("laptop", laptop["id"]), (desktop["id"], desktop["id"]), ("m_test", "m_test")):
            self.agent_credentials[expected] = laptop["credential"] if expected == laptop["id"] else \
                desktop["credential"] if expected == desktop["id"] else "m" * 40
            response = await self.client.post("/api/grants", headers=self.host | self.owner,
                                              json={"session": "main", "machine": machine})
            self.assertEqual(response.status, 200, await response.text())
            self.assertEqual((await response.json())["machine"], expected)
            response = await self.client.get("/api/version?machine=" + machine, headers=self.host | self.owner)
            self.assertEqual((response.status, await response.text()), (200, "0.45.1"))
        for machine, status in (("nosuch", 404), (7, 400), ("", 400)):
            response = await self.client.post("/api/grants", headers=self.host | self.owner,
                                              json={"session": "main", "machine": machine})
            self.assertEqual(response.status, status, machine)
        state = await (await self.client.get("/api/state", headers=self.host | self.owner)).json()
        self.assertEqual(sorted(g["machine_name"] or g["machine"] for g in state["grants"]), ["desktop", "laptop", "m_test"])
        # Revoking needs a choice too, and revokes only the chosen machine and its grants.
        response = await self.client.post("/api/machine-revoke", headers=self.host | self.owner, json={})
        self.assertEqual(response.status, 409)
        response = await self.client.post("/api/machine-revoke", headers=self.host | self.owner, json={"machine": "laptop"})
        self.assertEqual(response.status, 200)
        machines = {g["machine"]: g["revoked"] for g in
                    (await (await self.client.get("/api/state", headers=self.host | self.owner)).json())["grants"]}
        self.assertEqual(machines, {laptop["id"]: "machine-revoked", desktop["id"]: None, "m_test": None})
        # A revoked machine's credential no longer logs in to the relay; the others' still do.
        for machine, allowed in ((laptop, False), (desktop, True)):
            response = await self.private.post("/_frp", json={"op": "Login", "content": {
                "metas": {"machine": machine["id"], "credential": machine["credential"], "protocol": "1"}}})
            self.assertEqual(not (await response.json()).get("reject"), allowed)
        # Its name is free again.
        self.assertEqual((await self.enroll("laptop"))["name"], "laptop")

    async def test_machine_names_are_unique_and_plain(self):
        await self.enroll("laptop")
        self.assertIn("already named laptop", await self.enroll("laptop", status="issue"))
        for name in ("m_fake", "-x", "a b", "x" * 65, 7):
            response = await self.client.post("/api/enrollment", headers=self.host | self.owner, json={"name": name})
            self.assertEqual(response.status, 400, name)
        # Two codes for one name: the second enrollment is refused, and its code stays unused.
        codes = []
        for _ in range(2):
            response = await self.client.post("/api/enrollment", headers=self.host | self.owner, json={"name": "desktop"})
            codes.append((await response.json())["code"])
        self.assertEqual((await self.client.post("/api/enroll", headers=self.host, json={"code": codes[0]})).status, 200)
        response = await self.client.post("/api/enroll", headers=self.host, json={"code": codes[1]})
        self.assertEqual((response.status, await response.text()), (409, "An enrolled machine is already named desktop"))

    async def test_the_operator_cli_lists_and_revokes_one_machine_by_name(self):
        laptop = await self.enroll("laptop")
        listed = self.admin("machines")
        self.assertRegex(listed, r"m_test\s+-\s+enabled")
        self.assertRegex(listed, laptop["id"] + r"\s+laptop\s+enabled")
        grant = await self.grant(machine="m_test")
        sockets = await self.sockets(grant)
        self.assertIn("Disabled 1 machine(s): " + laptop["id"], self.admin("machine-revoke", "--machine", "laptop"))
        await self.echoes(sockets[1], "unaffected")
        self.assertRegex(self.admin("machines"), laptop["id"] + r"\s+laptop\s+revoked")
        self.admin("machine-revoke", "--all")
        await self.closed(sockets)
        self.assertEqual(self.broker.store.one("SELECT COUNT(*) AS n FROM machines WHERE enabled=1")["n"], 0)

class NonDefaultDomainBrokerTests(BrokerTests):
    """Every broker test again with another environment's domain, port and HSTS policy."""
    ENV = {"RT_ENVIRONMENT": "staging", "RT_PUBLIC_HOST": "Term.Staging.Corp-Example.net",
           "RT_RELAY_HOST": "relay.staging.corp-example.net", "RT_PUBLIC_PORT": "8443", "RT_HSTS_MAX_AGE": "300"}
    ORIGIN = "https://term.staging.corp-example.net:8443"
    HOST = PORT_HOST = "term.staging.corp-example.net:8443"
    HSTS = "max-age=300"
    # The lab default and the same host without its port are other origins here.
    FOREIGN_ORIGINS = BrokerTests.FOREIGN_ORIGINS + ("https://term.test", "https://term.staging.corp-example.net",
                                                     "http://term.staging.corp-example.net:8443")

    async def test_other_environment_hosts_refused(self):
        for host in ("term.test", "term.staging.corp-example.net", "term.staging.corp-example.net:443",
                     "relay.staging.corp-example.net:8443"):
            response = await self.client.get("/info/version", headers={"Host": host})
            self.assertEqual(response.status, 403, host)
            self.assertEqual(response.headers["Strict-Transport-Security"], self.HSTS)


if __name__ == "__main__":
    unittest.main()
