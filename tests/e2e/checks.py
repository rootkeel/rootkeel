#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""End-to-end checks a person's computer makes against a running server, with its rt login.

Runs in the e2e client container (the agent test image, which has aiohttp), as run.sh starts it.

  checks.py online URL CA NAME...        wait until each named machine's agent has logged in to the relay
  checks.py browser GRANT CA MACHINE     the browser flow of one grant: sign-in page, a wrong session
                                         credential refused, sign in, the terminal page, a live shell on
                                         MACHINE over WebSocket, foreign Origin and missing sign-in refused
  checks.py revoke GRANT CA MACHINE KEPT KEPT_MACHINE
                                         with a live terminal open on each grant, rt machine-revoke MACHINE
                                         closes the first at once and refuses it from then on; the grant
                                         KEPT on KEPT_MACHINE keeps working
"""
import asyncio
import json
import secrets
import ssl
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

RT = "/src/rt"


def fail(message):
    print("FAIL " + message, file=sys.stderr, flush=True)
    sys.exit(1)


def check(condition, message):
    if not condition:
        fail(message)
    print("PASS " + message, flush=True)


def online(url, ca, names):
    """Each named machine has an agent protocol, recorded at its first relay login."""
    credential = (Path.home() / ".config/rt" / urlsplit(url).hostname / "credential").read_text().strip()
    context = ssl.create_default_context(cafile=ca)
    deadline, machines = time.monotonic() + 120, []
    while time.monotonic() < deadline:
        request = urllib.request.Request(url + "/api/state", headers={"Authorization": "Bearer " + credential})
        with urllib.request.urlopen(request, context=context, timeout=8) as response:
            machines = json.load(response)["machines"]
        ready = {m["name"] for m in machines if m["enabled"] and m.get("protocol")}
        if set(names) <= ready:
            check(True, "machines %s are enrolled and their agents logged in to the relay" % ", ".join(names))
            return
        time.sleep(1)
    fail("machines did not come online: %s" % json.dumps(machines))


class Grant:
    """One grant as a browser uses it: its origin, sign-in, and Zellij's web client sockets."""

    def __init__(self, path, ca):
        self.grant = json.loads(Path(path).read_text())
        self.url = self.grant["url"]
        self.origin = self.url.split("/g/")[0]
        self.base = self.origin + "/g/" + self.grant["id"]
        self.session = self.url.rsplit("/", 1)[1]
        self.tls = ssl.create_default_context(cafile=ca)

    def client(self):
        return aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True),
                                     connector=aiohttp.TCPConnector(ssl=self.tls))

    async def sign_in(self, client, token=None):
        async with client.post(self.base + "/command/login", json={"auth_token": token or self.grant["token"]},
                               headers={"Origin": self.origin}) as response:
            return response.status

    async def terminal(self, client, origin=None):
        """The terminal socket for the grant's session, after the control socket's resize, as Zellij's web client."""
        headers = {"Origin": origin or self.origin}
        async with client.post(self.base + "/session?session=" + self.session, headers=headers, json={}) as response:
            body = await response.text()
            if response.status != 200:
                raise AssertionError("session request refused (%d): %s" % (response.status, body))
            client_id = json.loads(body)["web_client_id"]
        query = "?web_client_id=" + client_id
        control = await client.ws_connect(self.base + "/ws/control" + query, headers=headers)
        await control.send_str(json.dumps({"web_client_id": client_id,
                                           "payload": {"type": "TerminalResize", "rows": 24, "cols": 80}}))
        terminal = await client.ws_connect(self.base + "/ws/terminal/" + self.session + query, headers=headers)
        return control, terminal


async def run_line(ws, line, want, timeout=20):
    """Type one line into the shell and wait for want in its output; returns whether it appeared."""
    await ws.send_str(line + "\r")
    seen = ""
    try:
        async with asyncio.timeout(timeout):
            async for message in ws:
                if message.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                    data = message.data if isinstance(message.data, str) else message.data.decode(errors="replace")
                    seen += data
                    if want in seen:
                        return True
                else:
                    return False
    except TimeoutError:
        pass
    return False


async def shell_on(ws, machine):
    """The shell behind the socket runs on machine: its E2E_MACHINE answers."""
    word = "rk-" + secrets.token_hex(4)
    return await run_line(ws, 'echo "%s-$E2E_MACHINE-$((6*7))"' % word, "%s-%s-42" % (word, machine))


async def browser(path, ca, machine):
    grant = Grant(path, ca)
    async with grant.client() as client:
        async with client.get(grant.url) as response:
            page = await response.text()
            check(response.status == 200 and 'type="password"' in page,
                  "%s: the grant URL shows the sign-in form" % grant.grant["id"])
        check(await grant.sign_in(client, secrets.token_urlsafe(32)) == 401, "a wrong session credential is refused")
        async with client.post(grant.base + "/session?session=" + grant.session, headers={"Origin": grant.origin},
                               json={}) as response:
            check(response.status == 401, "the terminal refuses a browser that has not signed in")
        check(await grant.sign_in(client) == 200, "the session credential signs the browser in")
        async with client.get(grant.url) as response:
            page = await response.text()
            check(response.status == 200 and "_rt_mobile.js" in page and 'type="password"' not in page,
                  "the signed-in browser gets the terminal page")
        control, terminal = await grant.terminal(client)
        check(await shell_on(terminal, machine), "typing into the terminal over WebSocket runs it in a shell on " + machine)
        await terminal.close()
        await control.close()
        try:
            async with client.ws_connect(grant.base + "/ws/terminal/" + grant.session,
                                         headers={"Origin": "https://attacker.example.net"}):
                fail("a foreign Origin opened the terminal socket")
        except aiohttp.WSServerHandshakeError as error:
            check(error.status == 403, "a foreign Origin is refused on the terminal socket (%d)" % error.status)


async def revoke(path, ca, machine, kept_path, kept_machine):
    gone, kept = Grant(path, ca), Grant(kept_path, ca)
    async with gone.client() as gone_client, kept.client() as kept_client:
        for grant, client, name in ((gone, gone_client, machine), (kept, kept_client, kept_machine)):
            if await grant.sign_in(client) != 200:
                fail("%s: sign-in refused before the revoke" % grant.grant["id"])
        gone_control, gone_terminal = await gone.terminal(gone_client)
        kept_control, kept_terminal = await kept.terminal(kept_client)
        check(await shell_on(gone_terminal, machine) and await shell_on(kept_terminal, kept_machine),
              "live terminals are open on %s and %s" % (machine, kept_machine))
        result = await asyncio.to_thread(subprocess.run, ["python3", RT, "machine-revoke", machine],
                                         capture_output=True, text=True, timeout=30)
        check(result.returncode == 0, "rt machine-revoke %s: %s" % (machine, result.stdout.strip() or result.stderr))
        closed = False
        try:
            async with asyncio.timeout(5):
                async for message in gone_terminal:
                    if message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
                closed = True
        except TimeoutError:
            pass
        check(closed and gone_terminal.closed, "the revoked machine's open terminal closed within 5 seconds")
        await gone_control.close()
        check(await gone.sign_in(gone_client) == 401, "the revoked machine's grant refuses sign-in")
        async with gone_client.get(gone.url) as response:
            page = await response.text()
            check("_rt_mobile.js" not in page and 'type="password"' not in page,
                  "the revoked machine's grant no longer serves the terminal page to its signed-in browser")
        check(await shell_on(kept_terminal, kept_machine), "the other machine's open terminal keeps working")
        await kept_terminal.close()
        await kept_control.close()
        control, terminal = await kept.terminal(kept_client)
        check(await shell_on(terminal, kept_machine), "the other machine's grant still opens a new terminal")
        await terminal.close()
        await control.close()


def main(argv):
    if len(argv) >= 4 and argv[0] == "online":
        online(argv[1].rstrip("/"), argv[2], argv[3:])
    elif len(argv) == 4 and argv[0] == "browser":
        asyncio.run(browser(*argv[1:]))
    elif len(argv) == 6 and argv[0] == "revoke":
        asyncio.run(revoke(*argv[1:]))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
