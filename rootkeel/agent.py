# SPDX-License-Identifier: AGPL-3.0-only
"""The agent on an owner's Linux machine: only loopback listeners and an outbound frpc tunnel."""
import asyncio
import ctypes
import hmac
import json
import os
import re
import signal
import sys
from pathlib import Path

from aiohttp import ClientError, ClientSession, ClientTimeout, DummyCookieJar, UnixConnector, web
from . import config, peer
from .transport import MAX_BODY, bridge, forward


def protect_process():
    """Disable ptrace/proc memory and core dumps, for the credential-bearing Python process."""
    if sys.platform != "linux":
        raise RuntimeError("The machine agent requires Linux process isolation")
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE
        raise OSError(ctypes.get_errno(), "Cannot disable agent process dumps")


def terminal_socket():
    return peer.address(os.environ.get("RT_TERMINAL_SOCKET", "/run/rootkeel-helper/terminal.sock"))


# The agent API the broker calls, by protocol: 1 mints and revokes Zellij tokens and lists sessions.
# Announced at every relay login (frp metadata), so the broker knows what it may ask of an agent that has not
# been updated yet. Raise it when the broker gains a call it must not make to older agents.
PROTOCOL = 1
SESSION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


async def command(*args):
    # This fixed endpoint has no machine credential and runs as the shell user.
    try:
        async with ClientSession(connector=UnixConnector(path=terminal_socket()),
                                 trust_env=False, timeout=ClientTimeout(total=10)) as client:
            async with client.post("http://localhost/token", json=list(args)) as response:
                if response.status == 400:
                    raise web.HTTPNotImplemented(text="The terminal helper is older than the agent")
                if response.status != 200:
                    raise web.HTTPServiceUnavailable(text="Zellij command failed")
                return await response.text()
    except (ClientError, asyncio.TimeoutError):
        raise web.HTTPServiceUnavailable(text="Terminal helper unavailable") from None


def frpc_config(cfg):
    """frpc settings for the one environment this machine enrolled with; refuses an incomplete target."""
    relay_host = config.hostname(cfg.get("relay_host"), "machine.json relay_host", "relay.term.example.com")
    relay_port = config.port(cfg.get("relay_port"), "machine.json relay_port")
    # All values are JSON quoted, which is also valid TOML basic string syntax.
    lines = [f"serverAddr = {json.dumps(relay_host)}",
             f"serverPort = {relay_port}",
             "loginFailExit = false",
             f"user = {json.dumps(cfg['id'])}",
             f"metadatas.machine = {json.dumps(cfg['id'])}",
             f"metadatas.credential = {json.dumps(cfg['credential'])}",
             f"metadatas.protocol = {json.dumps(str(PROTOCOL))}",
             'transport.protocol = "wss"',
             f"transport.tls.serverName = {json.dumps(relay_host)}"]
    if cfg.get("ca_file"):
        # TLS configuration belongs before [[proxies]].
        lines.append(f"transport.tls.trustedCaFile = {json.dumps(cfg['ca_file'])}")
    lines += ["transport.heartbeatInterval = 2",
              "transport.heartbeatTimeout = 10",
              'log.to = "console"',
              'log.level = "warn"',
              "[[proxies]]",
              'name = "zellij"',
              'type = "http"',
              'localIP = "127.0.0.1"',
              "localPort = 8083",
              f"customDomains = [{json.dumps(cfg['domain'])}]"]
    return "\n".join(lines) + "\n"


class Agent:
    def __init__(self, config):
        self.config = config
        self.token_lock = asyncio.Lock()
        self.tokens = {}
        self.client = None
        self.frpc = None

    async def reset_tokens(self):
        """Revoke every Zellij web token, so only tokens minted in this lifetime are valid."""
        async with self.token_lock:
            await command("zellij", "web", "--revoke-all-tokens")
            self.tokens = {}

    @web.middleware
    async def authenticate(self, request, handler):
        if not hmac.compare_digest(request.headers.get("Authorization", "").encode(), ("Bearer " + self.config["credential"]).encode()):
            raise web.HTTPUnauthorized()
        return await handler(request)

    async def token(self, request):
        try:
            gid = (await request.json())["grant_id"]
            if not isinstance(gid, str) or not re.fullmatch(r"g_[a-f0-9]{24}", gid):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise web.HTTPBadRequest() from None
        async with self.token_lock:
            if gid not in self.tokens:
                text = await command("zellij", "web", "--create-token")
                match = re.search(r"^(token_\d+): (\S+)", text, re.M)
                if not match:
                    raise web.HTTPBadGateway(text="Unexpected Zellij token response")
                self.tokens[gid] = {"name": match[1], "token": match[2]}
            return web.json_response(self.tokens[gid])

    async def sessions(self, request):
        """The machine's Zellij session names, so the broker can refuse a grant for one that does not exist."""
        text = await command("zellij", "list-sessions", "--short", "--no-formatting")
        return web.json_response({"sessions": [name for name in text.split() if SESSION_NAME.fullmatch(name)]})

    async def revoke(self, request):
        try:
            data = await request.json()
            gid, name = data["grant_id"], data["name"]
            if not isinstance(name, str) or not re.fullmatch(r"token_\d+", name):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise web.HTTPBadRequest() from None
        async with self.token_lock:
            token = self.tokens.get(gid)
            if token:
                if token["name"] != name:
                    raise web.HTTPConflict(text="Token mapping differs")
                await command("zellij", "web", "--revoke-token", name)
                del self.tokens[gid]
            else:
                # An old grant must never revoke a reused token_N minted for a live grant.
                print(f"Revoke for unknown grant {gid}; ensuring {name} is not valid", file=sys.stderr)
                listed = re.findall(r"^(token_\d+)\b", await command("zellij", "web", "--list-tokens"), re.M)
                if name in listed and name not in {t["name"] for t in self.tokens.values()}:
                    await command("zellij", "web", "--revoke-token", name)
        return web.json_response({"ok": True})

    async def proxy(self, request):
        headers = {"Host": "127.0.0.1:8082", "Accept-Encoding": "identity"}
        for name in ("Cookie", "Content-Type"):
            if name in request.headers:
                headers[name] = request.headers[name]
        url = "http://127.0.0.1:8082" + request.raw_path
        if request.path.startswith("/ws/"):
            return await bridge(request, self.client, url, headers)
        # Login is the only place its upstream cookie must survive for the broker.
        if request.path == "/command/login":
            async with self.client.post(url, headers=headers, data=await request.read()) as response:
                result = web.Response(status=response.status, body=await response.read(), content_type="application/json")
                if "session_token" in response.cookies:
                    result.set_cookie("session_token", response.cookies["session_token"].value, httponly=True)
                return result
        return await forward(request, self.client, url, headers)

    async def start(self, app):
        text = frpc_config(self.config)  # Refuse an incomplete enrollment target before starting anything.
        # Wait for the owner-side helper before admitting tunnel traffic.
        for attempt in range(60):
            try:
                async with ClientSession(connector=UnixConnector(path=terminal_socket()),
                                         timeout=ClientTimeout(total=2)) as client:
                    async with client.get("http://localhost/ready") as response:
                        if response.status == 200:
                            break
            except (ClientError, asyncio.TimeoutError):
                pass
            if attempt == 59:
                raise RuntimeError("Terminal helper unavailable")
            await asyncio.sleep(1)
        # The token map does not survive restarts; tokens from earlier lifetimes must not outlive their grants.
        await self.reset_tokens()
        self.client = ClientSession(cookie_jar=DummyCookieJar(), timeout=ClientTimeout(total=5), auto_decompress=False)
        path = Path(os.environ.get("RT_FRPC_CONFIG", str(Path(os.environ["RT_MACHINE_FILE"]).parent / "frpc.toml")))
        path.write_text(text)
        path.chmod(0o600)
        self.frpc = await asyncio.create_subprocess_exec("frpc", "-c", str(path))

    async def stop(self, app):
        if self.frpc and self.frpc.returncode is None:
            self.frpc.terminate()
            await self.frpc.wait()
        if self.client:
            await self.client.close()
        # Zellij and its shell deliberately survive agent/tunnel restarts.


async def main():
    os.umask(0o077)
    protect_process()
    config = json.loads(Path(os.environ["RT_MACHINE_FILE"]).read_text())
    agent = Agent(config)
    app = web.Application(middlewares=[agent.authenticate], client_max_size=MAX_BODY)
    app.router.add_post("/_agent/token", agent.token)
    app.router.add_post("/_agent/revoke", agent.revoke)
    app.router.add_post("/_agent/sessions", agent.sessions)
    app.router.add_route("*", "/{path:.*}", agent.proxy)
    app.on_startup.append(agent.start)
    app.on_cleanup.append(agent.stop)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 8083).start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    exited = asyncio.create_task(agent.frpc.wait())
    stopping = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait((exited, stopping), return_when=asyncio.FIRST_COMPLETED)
        if exited.done():
            raise RuntimeError("frpc exited; restart agent to restore tunnel")
    finally:
        stopping.cancel()
        await runner.cleanup()
        await exited


if __name__ == "__main__":
    asyncio.run(main())
