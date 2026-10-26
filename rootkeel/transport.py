# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded HTTP and WebSocket forwarding; never log payloads or credentials."""
import asyncio
import contextlib
import re
from aiohttp import ClientError, WSMsgType, WSServerHandshakeError, web

try:  # aiohttp 3.11+ deprecates a bare number; agents may run a distribution's older aiohttp.
    from aiohttp import ClientWSTimeout
    WS_CLOSE_TIMEOUT = ClientWSTimeout(ws_close=5)
except ImportError:
    WS_CLOSE_TIMEOUT = 5

MAX_BODY = 2 * 1024 * 1024
# One keystroke of terminal input: an escape sequence (optionally after Alt's ESC), Alt with
# one key, a run of printable characters, or any other single control key.
KEYSTROKE_PATTERN = r"""
      \x1b?\x1b\[M[\s\S]{3}                  # X10 mouse report
    | \x1b?\x1b\[[0-?]*[ -/]*[@-~]           # CSI: arrows, function keys, paste markers, mouse
    | \x1b?\x1bO[\s\S]                      # SS3: arrows and F1-F4 in application mode
    | \x1b[]P_^X][\s\S]*?(?:\x07|\x1b\\)     # OSC, DCS and other strings: replies to queries
    | \x1b(?:ALT)?                          # Alt with one key (Ctrl+Alt+g is ESC Ctrl-g), or Esc
    | PRINTABLE+                            # printable characters
    | [\s\S]                                 # Enter, Tab, Backspace, Ctrl with a key
"""
# Bytes hold UTF-8; text is matched as characters, so no piece ends inside one.
KEYSTROKE = re.compile(KEYSTROKE_PATTERN.replace("ALT", r"[\xc0-\xf7][\x80-\xbf]*|[\s\S]")
                       .replace("PRINTABLE", r"[\x20-\x7e\x80-\xff]").encode(), re.X)
TEXT_KEYSTROKE = re.compile(KEYSTROKE_PATTERN.replace("ALT", r"[\s\S]").replace("PRINTABLE", r"[^\x00-\x1f\x7f]"), re.X)
# Close codes a peer may send; anything else ends the other side with a normal close.
SENDABLE_CLOSE = set(range(1000, 1004)) | set(range(1007, 1015)) | set(range(3000, 5000))


def keystrokes(data):
    """Terminal input split so each piece holds one key.

    Zellij 0.45.1's web server gives every non-character key in a multi-key
    message the whole message's bytes, so a burst (a paste without bracketed
    paste, or typing faster than a read) reached the shell twice. A run of
    printable characters stays one piece: Zellij handles those one by one.
    """
    return (TEXT_KEYSTROKE if isinstance(data, str) else KEYSTROKE).findall(data)


async def bridge(request, client, url, headers, on_input=None, register=None, split_input=False):
    """Connect upstream before accepting a downstream socket; tear down both ends.

    A close frame (Zellij ending the connection on a detach, or a client
    leaving) reaches the other side with its code; a dropped connection does not.
    split_input sends terminal input one key per message (see keystrokes)."""
    try:
        upstream = await client.ws_connect(url, headers=headers, max_msg_size=MAX_BODY,
                                           heartbeat=20, timeout=WS_CLOSE_TIMEOUT)
    except WSServerHandshakeError as error:
        if error.status == 401:
            raise web.HTTPUnauthorized(text="Upstream login required") from None
        raise web.HTTPServiceUnavailable(text="Machine unavailable") from None
    except (ClientError, asyncio.TimeoutError):
        raise web.HTTPServiceUnavailable(text="Machine unavailable") from None
    downstream = web.WebSocketResponse(max_msg_size=MAX_BODY, heartbeat=20)
    try:
        await downstream.prepare(request)
        if register:
            register(downstream, request.transport)

        async def pump(source, dest, input_side=False):
            """Until the source closes: (code, reason) for a close frame, None for a dropped connection."""
            while True:
                msg = await source.receive()
                if msg.type in (WSMsgType.TEXT, WSMsgType.BINARY):
                    if input_side and on_input and msg.data:
                        on_input()
                    send = dest.send_str if msg.type == WSMsgType.TEXT else dest.send_bytes
                    for piece in keystrokes(msg.data) if input_side and split_input else [msg.data]:
                        await send(piece)
                elif msg.type == WSMsgType.CLOSE:
                    return msg.data, msg.extra or ""
                else:
                    return None

        tasks = [asyncio.create_task(pump(downstream, upstream, True)),
                 asyncio.create_task(pump(upstream, downstream))]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task, other in zip(tasks, (upstream, downstream)):
                if task in done and not task.cancelled() and not task.exception() and task.result():
                    code, reason = task.result()
                    with contextlib.suppress(asyncio.TimeoutError, ClientError, ConnectionError, RuntimeError):
                        await asyncio.wait_for(other.close(code=code if code in SENDABLE_CLOSE else 1000,
                                                           message=reason.encode()[:123]), 2)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        # Abort transports rather than waiting for an uncooperative peer's close.
        if request.transport:
            request.transport.close()
        await upstream.close()
    return downstream


async def forward(request, client, url, headers):
    try:
        async with client.request(request.method, url, headers=headers,
                                  data=await request.read(), allow_redirects=False) as upstream:
            chunks, size = [], 0
            async for chunk in upstream.content.iter_chunked(65536):
                size += len(chunk)
                if size > MAX_BODY:
                    raise web.HTTPBadGateway(text="Upstream response too large")
                chunks.append(chunk)
            body = b"".join(chunks)
            safe = {name: upstream.headers[name] for name in
                    ("Content-Type", "X-Frame-Options") if name in upstream.headers}
            return web.Response(status=upstream.status, body=body, headers=safe)
    except (ClientError, asyncio.TimeoutError):
        raise web.HTTPServiceUnavailable(text="Machine unavailable") from None
