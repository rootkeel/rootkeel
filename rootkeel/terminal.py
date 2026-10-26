# SPDX-License-Identifier: AGPL-3.0-only
"""Owner-side Zellij helper, isolated from the machine credential.

Only listens on an owner/agent Unix socket. Its operations are already available to the shell's
OS user; it never receives machine credentials or broker Authorization headers. On a socket file
its mode keeps others out; with RT_TERMINAL_PEER_UID (required for an abstract "@name" socket)
only the agent's uid is admitted.

It starts the session and Zellij's web server only when they are not already running: both
outlive a restart of the helper.
"""
import asyncio
import os
import re
import signal
from pathlib import Path

from aiohttp import web

from . import peer

ZELLIJ_CONFIG = Path(__file__).resolve().parent.parent / "agent" / "zellij"

SESSIONS = ["zellij", "list-sessions", "--short", "--no-formatting"]
WEB_ADDRESS = ["--ip", "127.0.0.1", "--port", "8082"]
WEB_ONLINE = "Web server online"  # zellij web --status, when its web server answers


async def command(*args):
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                  stderr=asyncio.subprocess.DEVNULL)
    output, _ = await process.communicate()
    if process.returncode and not (list(args) == SESSIONS and not output):
        raise web.HTTPServiceUnavailable(text="Zellij command failed")
    return output.decode()  # Listing no sessions at all exits 1 with no output.


async def token_command(request):
    try:
        args = await request.json()
        if args not in (["zellij", "web", "--create-token"], ["zellij", "web", "--list-tokens"],
                        ["zellij", "web", "--revoke-all-tokens"], SESSIONS) and not (
                isinstance(args, list) and len(args) == 4
                and args[:3] == ["zellij", "web", "--revoke-token"]
                and isinstance(args[3], str) and re.fullmatch(r"token_\d+", args[3])):
            raise ValueError()
    except (ValueError, TypeError):
        raise web.HTTPBadRequest(text="Unsupported Zellij operation") from None
    return web.Response(text=await command(*args))


async def start(app):
    if (await command("zellij", "--version")).strip() != "zellij 0.45.1":
        raise ValueError("Terminal requires Zellij 0.45.1")
    session = os.environ.get("RT_SESSION", "main")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", session):
        raise ValueError("Invalid RT_SESSION")
    config_dir = os.environ.get("RT_ZELLIJ_CONFIG", str(ZELLIJ_CONFIG))
    config = ["--config-dir", config_dir, "--config", str(Path(config_dir) / "config.kdl")]
    # The session and the web server outlive a helper restart (KillMode=process), and Zellij refuses to start either
    # again ("Session already exists", "Address in use"), so only what is not running is started. Restarts are
    # routine: needrestart restarts the unit after every security update to a library Python maps.
    if session not in (await command(*SESSIONS)).split():
        await command("zellij", "--config-dir", config_dir, "attach", "--create-background", session)
    # Zellij 0.45.1's web --status exits 0 whether the server answers or not; only its output tells.
    status = await command("zellij", *config, "web", "--status", "--timeout", "5", *WEB_ADDRESS)
    if not status.startswith(WEB_ONLINE):
        await command("zellij", *config, "web", "--daemonize", *WEB_ADDRESS)


async def ready(request):
    return web.Response(text="ready")


def application(agent_uid=None):
    """The helper's routes; with agent_uid, for that uid only."""
    middlewares = [] if agent_uid is None else [peer.only(agent_uid, "Only the agent may use the terminal helper")]
    app = web.Application(client_max_size=1024, middlewares=middlewares)
    app.router.add_post("/token", token_command)
    app.router.add_get("/ready", ready)
    return app


async def main():
    os.umask(0o077)
    path = os.environ.get("RT_TERMINAL_SOCKET", "/run/rootkeel-helper/terminal.sock")
    agent_uid = os.environ.get("RT_TERMINAL_PEER_UID")
    if peer.abstract(path) and not agent_uid:
        raise ValueError("An abstract RT_TERMINAL_SOCKET needs RT_TERMINAL_PEER_UID, the agent's uid")
    app = application(int(agent_uid) if agent_uid else None)
    app.on_startup.append(start)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        await web.UnixSite(runner, peer.address(path)).start()
        if not peer.abstract(path):
            Path(path).chmod(0o660)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
