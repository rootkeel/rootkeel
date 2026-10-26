# SPDX-License-Identifier: AGPL-3.0-only
"""Exercise the native compatibility adapter over real HTTP, HTTPS and WS sockets."""
import asyncio
import datetime
import ipaddress
import ssl
import tempfile
import unittest
from pathlib import Path

from aiohttp import ClientSession, WSMsgType, web
from aiohttp.test_utils import TestServer
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from test_cli import rt


class NativeGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_prefix_tls_cookies_websocket_and_browser_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "localhost")])
            now = datetime.datetime.now(datetime.timezone.utc)
            cert = x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(
                x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1)).not_valid_after(
                now + datetime.timedelta(days=1)).add_extension(x509.SubjectAlternativeName([
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False).sign(key, hashes.SHA256())
            ca, pem = Path(directory) / "ca.pem", Path(directory) / "key.pem"
            ca.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            pem.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()))
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(ca, pem)
            gid = "g_" + "a" * 24
            prefix = "/g/" + gid
            calls, refuse = [], []

            async def upstream(request):
                calls.append(request.path)
                self.assertTrue(request.path.startswith(prefix + "/"))
                if request.path.endswith("/command/login"):
                    self.assertEqual((await request.json())["auth_token"], "capability")
                    response = web.json_response({"success": True})
                    response.set_cookie("session_token", "broker-cookie", secure=True, httponly=True, path=prefix + "/")
                    return response
                if refuse:  # As the broker answers once the grant's login is gone.
                    raise web.HTTPUnauthorized()
                self.assertEqual(request.cookies.get("session_token"), "broker-cookie")
                if request.path.endswith("/session"):
                    self.assertEqual(request.query["session"], "main")
                    return web.json_response({"web_client_id": "client"})
                ws = web.WebSocketResponse()
                await ws.prepare(request)
                await ws.send_str("initial")
                async for msg in ws:
                    if msg.data == "detach":  # As Zellij ends the connection on a detach.
                        await ws.close(code=1000, message=b"Connection closed")
                        break
                    await ws.send_str(msg.data)
                return ws

            app = web.Application()
            app.router.add_route("*", "/{path:.*}", upstream)
            server = TestServer(app, scheme="https")
            await server.start_server(ssl=tls)
            try:
                grant = {"id": gid, "url": str(server.make_url(prefix + "/main")), "token": "capability"}
                gateway = rt.NativeGateway(grant, "main", str(ca))
                gateway.__enter__()
                try:
                    base = gateway.url.removesuffix("/main")
                    async with ClientSession() as client:
                        for headers in ({"Origin": "https://evil.test"}, {"Host": "evil.test"}):
                            response = await client.post(base + "/command/login", headers=headers, json={"auth_token": "capability"})
                            self.assertEqual(response.status, 403)
                        response = await client.post(base + "/command/login", json={"auth_token": "wrong"})
                        self.assertEqual(response.status, 401)
                        self.assertEqual(calls, [])
                        response = await client.post(base + "/command/login", json={"auth_token": "capability"})
                        self.assertEqual(response.status, 200)
                        self.assertTrue((await response.json())["success"])
                        headers = {"Cookie": "session_token=broker-cookie"}
                        self.assertEqual((await client.post(base + "/session?session=other", headers=headers)).status, 403)
                        self.assertEqual((await client.post(base + "/session?session=main")).status, 401)
                        response = await client.post(base + "/session?session=main", headers=headers)
                        self.assertEqual(await response.json(), {"web_client_id": "client"})
                        for path in ("/ws/control", "/ws/terminal/main"):
                            ws = await client.ws_connect(base + path + "?web_client_id=client", headers=headers)
                            self.assertEqual((await asyncio.wait_for(ws.receive(), 3)).data, "initial")
                            await ws.send_str("native-input")
                            self.assertEqual((await asyncio.wait_for(ws.receive(), 3)).data, "native-input")
                            await ws.close()
                        await asyncio.sleep(0.2)
                        self.assertFalse(gateway.ended)  # The client's own close, answered by the server, is no detach.
                        ws = await client.ws_connect(base + "/ws/control?web_client_id=client", headers=headers)
                        self.assertEqual((await asyncio.wait_for(ws.receive(), 3)).data, "initial")
                        await ws.send_str("x" * 70000)  # A long frame first: the watch follows frame lengths.
                        self.assertEqual(len((await asyncio.wait_for(ws.receive(), 3)).data), 70000)
                        self.assertFalse(gateway.ended)
                        await ws.send_str("detach")
                        self.assertEqual((await asyncio.wait_for(ws.receive(), 3)).type, WSMsgType.CLOSE)
                        self.assertTrue(gateway.ended)
                        self.assertEqual((await client.get(base + "/ws/terminal/other", headers=headers)).status, 403)
                        # A reconnect's new zellij client logs in again: answered with the cookie already held,
                        # so reconnecting never uses up the grant's logins at the broker.
                        self.assertEqual((await client.post(base + "/command/login", json={"auth_token": "wrong"})).status, 401)
                        response = await client.post(base + "/command/login", json={"auth_token": "capability"})
                        self.assertEqual((response.status, response.cookies["session_token"].value), (200, "broker-cookie"))
                        self.assertTrue((await response.json())["success"])
                        self.assertEqual(calls.count(prefix + "/command/login"), 1)
                        # Once the broker refuses that cookie, the next login is a real one.
                        refuse.append(True)
                        self.assertEqual((await client.post(base + "/session?session=main", headers=headers)).status, 401)
                        response = await client.post(base + "/command/login", json={"auth_token": "capability"})
                        self.assertEqual(response.status, 200)
                    self.assertEqual(calls, [prefix + path for path in (
                        "/command/login", "/session", "/ws/control", "/ws/terminal/main", "/ws/control", "/session",
                        "/command/login")])
                finally:
                    await asyncio.to_thread(gateway.__exit__)
            finally:
                await server.close()
