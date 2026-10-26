# SPDX-License-Identifier: AGPL-3.0-only
"""The Unix socket between the agent and the terminal helper: where it lives and who is calling.

A socket setting is a path, or "@name" for a name in Linux's abstract namespace.
An abstract socket has no file: it belongs to the network namespace, which
containers sharing one network namespace can reach even where they share no
filesystem. It has no file mode either, and /proc/net/unix lists its name to
every process in the namespace, so whoever serves one must admit callers by
their uid (SO_PEERCRED) alone.
"""
import socket
import struct

from aiohttp import web


def abstract(value):
    return value.startswith("@")


def address(value):
    """What bind() and connect() take: a path as it is, "@name" as "\\0name"."""
    return "\0" + value[1:] if abstract(value) else value


def uid(sock):
    """The uid of the process at the other end of a connected Unix socket."""
    _, peer, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
    return peer


def only(allowed, refusal):
    """Middleware admitting only connections from the uid `allowed`, checked on the connection itself."""
    @web.middleware
    async def middleware(request, handler):
        sock = request.transport.get_extra_info("socket") if request.transport else None
        if sock is None or not hasattr(socket, "SO_PEERCRED"):
            raise web.HTTPForbidden(text="Peer credentials unavailable")
        if uid(sock) != allowed:
            raise web.HTTPForbidden(text=refusal)
        return await handler(request)
    return middleware
