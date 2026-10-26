# SPDX-License-Identifier: AGPL-3.0-only
"""Execute rt's public main with controlled network/process boundaries."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import signal
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

loader = importlib.machinery.SourceFileLoader("rt_cli", str(Path(__file__).resolve().parents[2] / "rt"))
spec = importlib.util.spec_from_loader(loader.name, loader)
rt = importlib.util.module_from_spec(spec)
loader.exec_module(rt)


class CLITests(unittest.TestCase):
    def run_main(self, request, child=None, url="https://term.test", extra=(), sleep=None):
        with tempfile.TemporaryDirectory() as directory:
            credential = Path(directory) / "owner"
            credential.write_text("private-owner")
            args = ["rt", "attach", "main", "--url", url, "--credential-file", str(credential), *extra]
            with patch.object(sys, "argv", args), patch.object(rt, "request", request), \
                 patch.object(rt.subprocess, "check_output", return_value="zellij 0.45.1\n"), \
                 patch.object(rt.subprocess, "Popen", child or Mock()), patch.object(rt.time, "sleep", sleep or Mock()), \
                 contextlib.redirect_stderr(io.StringIO()) as stderr:
                result = rt.main()
                return result, stderr.getvalue()

    def test_server_mismatch_refused_before_grant_or_native_process(self):
        request = Mock(return_value=b"0.44.3")
        child = Mock()
        result, message = self.run_main(request, child)
        self.assertEqual(result, 2)
        self.assertIn("Server version mismatch", message)
        self.assertEqual(request.call_count, 1)
        child.assert_not_called()

    def test_leading_hyphen_capability_and_reconnect_policy(self):
        grant = {"id": "g_" + "a" * 24, "token": "-finite-capability", "url": "https://owner.term.test/g/" + "g_" + "a" * 24 + "/main", "expires": time.time() + 60}
        state_checks = 0
        def request(base, path, credential, data=None, ca=None):
            nonlocal state_checks
            if path == "/api/version":
                return b"0.45.1"
            if path == "/api/grants":
                return json.dumps(grant).encode()
            if path == "/api/state":
                state_checks += 1
                record = {**grant, "revoked": "revoked" if state_checks > 1 else None}
                return json.dumps({"grants": [record]}).encode()
            if path == "/api/revoke":
                return b'{"ok":true}'
            raise AssertionError(path)
        child = Mock()
        result, message = self.run_main(request, child)
        self.assertEqual(result, 0)
        argv = child.call_args.args[0]
        self.assertIn("--token=-finite-capability", argv)
        self.assertNotIn("--token", argv)
        self.assertEqual(child.call_count, 1)  # No new process after revoke.
        self.assertIn("Grant ended: revoked", message)
        self.assertNotIn(grant["token"], message)
        self.assertFalse(Path(child.call_args.kwargs["env"]["HOME"]).exists())

    def test_open_grant_reconnects_past_the_old_limits_until_revoked(self):
        clock = [1000.0]
        # Permanent: the broker ends a grant only when it is revoked (or an operator set a time limit).
        grant = {"id": "g_" + "a" * 24, "token": "capability", "url": "https://owner.term.test/g/" + "g_" + "a" * 24 + "/main",
                 "expires": 1000.0 + 100 * 365 * 86400, "permanent": True}
        state_checks, revoked = 0, []
        def request(base, path, credential, data=None, ca=None):
            nonlocal state_checks
            if path == "/api/version":
                return b"0.45.1"
            if path == "/api/grants":
                self.assertNotIn("ttl", data)
                self.assertNotIn("idle", data)
                return json.dumps(grant).encode()
            if path == "/api/state":
                state_checks += 1
                record = {**grant, "idle": None, "revoked": "revoked" if state_checks > 3 else None}
                return json.dumps({"grants": [record]}).encode()
            if path == "/api/revoke":
                revoked.append(clock[0])
                return b'{"ok":true}'
            raise AssertionError(path)
        def dropped(*args, **kwargs):
            clock[0] += 9 * 3600  # Each connection drops after longer than the old eight-hour cap.
            return Mock()
        child = Mock(side_effect=dropped)
        with patch.object(rt.time, "time", lambda: clock[0]):
            result, message = self.run_main(request, child)
        self.assertEqual(result, 0)
        self.assertEqual(child.call_count, 3)  # The same grant reconnected twice, with no new grant.
        self.assertEqual(message.count("Disconnected; retrying"), 3)
        self.assertIn("Grant ended: revoked", message)
        self.assertEqual(revoked, [1000.0 + 3 * 9 * 3600])

    def test_attach_shows_the_services_reason_when_access_ends(self):
        grant = {"id": "g_" + "a" * 24, "token": "capability", "url": "https://owner.term.test/g/" + "g_" + "a" * 24 + "/main",
                 "expires": 1e12, "permanent": True}
        refused = b"Host not allowed"
        for status, body, shown, hinted in ((403, refused, "Access ended (403): " + refused.decode(), False),
                                            (401, b"Owner credential required", "Access ended (401): Owner credential required", True)):
            def request(base, path, credential, data=None, ca=None):
                if path == "/api/version":
                    return b"0.45.1"
                if path == "/api/grants":
                    return json.dumps(grant).encode()
                if path == "/api/state":
                    raise urllib.error.HTTPError(base + path, status, "Refused", {}, io.BytesIO(body))
                if path == "/api/revoke":
                    return b'{"ok":true}'
                raise AssertionError(path)
            result, message = self.run_main(request, Mock())
            self.assertEqual(result, 0)
            self.assertIn(shown, message)
            self.assertNotIn("revoked", message)
            self.assertEqual("run rt login" in message, hinted)

    def test_operator_time_limit_still_ends_attach(self):
        clock = [1000.0]
        grant = {"id": "g_" + "a" * 24, "token": "capability", "url": "https://owner.term.test/g/" + "g_" + "a" * 24 + "/main",
                 "expires": 1060.0, "permanent": False}
        def request(base, path, credential, data=None, ca=None):
            if path == "/api/version":
                return b"0.45.1"
            if path in ("/api/grants", "/api/state"):
                return json.dumps(grant if path == "/api/grants" else {"grants": [{**grant, "revoked": None}]}).encode()
            if path == "/api/revoke":
                return b'{"ok":true}'
            raise AssertionError(path)
        def dropped(*args, **kwargs):
            clock[0] += 120
            return Mock()
        child = Mock(side_effect=dropped)
        with patch.object(rt.time, "time", lambda: clock[0]):
            result, message = self.run_main(request, child)
        self.assertEqual((result, child.call_count), (0, 1))
        self.assertIn("Grant ended: expired", message)

    def test_grants_lists_open_grants(self):
        state = {"grants": [{"id": "g_" + "a" * 24, "machine": "m_x", "session": "main", "expires": 1e12, "permanent": True, "revoked": None},
                            {"id": "g_" + "b" * 24, "machine": "m_x", "session": "old", "expires": 1e12, "permanent": True, "revoked": "revoked"}]}
        result, out, err = self.run_cli(["grants"], lambda base, path, *rest, **kw: json.dumps(state).encode())
        self.assertEqual(result, 0)
        self.assertIn("g_" + "a" * 24 + "  m_x      main     when revoked", out)
        self.assertNotIn("g_" + "b" * 24, out)
        self.assertIn("rt revoke GRANT", out)

    def grant_server(self, grant, calls):
        def request(base, path, credential, data=None, ca=None):
            calls.append((base, path))
            if path == "/api/version":
                return b"0.45.1"
            if path == "/api/grants":
                return json.dumps(grant).encode()
            if path == "/api/state":
                # Attach once, then end as revoked.
                ended = sum(p == "/api/state" for _, p in calls) > 1
                return json.dumps({"grants": [{**grant, "revoked": "revoked" if ended else None}]}).encode()
            if path == "/api/revoke":
                return b'{"ok":true}'
            raise AssertionError(path)
        return request

    def test_non_default_origin_attach_url(self):
        origin = "https://term.staging.corp-example.net:8443"
        grant = {"id": "g_" + "a" * 24, "token": "capability", "expires": time.time() + 60,
                 "url": "https://owner.term.staging.corp-example.net:8443/g/" + "g_" + "a" * 24 + "/main"}
        calls, child = [], Mock()
        result, message = self.run_main(self.grant_server(grant, calls), child, url=origin + "/")
        self.assertEqual(result, 0)
        self.assertEqual(child.call_args.args[0][1], "attach")
        self.assertTrue(child.call_args.args[0][2].startswith("http://127.0.0.1:"))
        self.assertTrue(child.call_args.args[0][2].endswith("/main"))
        self.assertTrue(all(base == origin for base, _ in calls))

    def test_attach_url_from_another_environment_refused_and_grant_revoked(self):
        path = "/g/" + "g_" + "a" * 24 + "/main"
        for url in ("https://term.dev.corp-example.net/main", "https://term.staging.corp-example.net/main",
                    "https://term.staging.corp-example.net:8443/other", "https://term.staging.corp-example.net:8443" + path,
                    "https://owner.term.dev.corp-example.net:8443" + path, "https://owner.term.staging.corp-example.net" + path,
                    "https://a.b.term.staging.corp-example.net:8443" + path, "https://owner.staging.corp-example.net:8443" + path,
                    "https://alice.term.staging.corp-example.net:8443" + path):
            grant = {"id": "g_" + "a" * 24, "token": "capability", "url": url, "expires": time.time() + 60}
            calls, child = [], Mock()
            result, message = self.run_main(self.grant_server(grant, calls), child,
                                            url="https://term.staging.corp-example.net:8443")
            self.assertEqual(result, 2, url)
            child.assert_not_called()
            self.assertIn("is not on https://term.staging.corp-example.net:8443", message)
            self.assertEqual(calls[-1][1], "/api/revoke")

    def run_cli(self, argv, request):
        with tempfile.TemporaryDirectory() as directory:
            credential = Path(directory) / "owner"
            credential.write_text("private-owner")
            args = ["rt", *argv, "--url", "https://term.test", "--credential-file", str(credential)]
            with patch.object(sys, "argv", args), patch.object(rt, "request", request), \
                    contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
                return rt.main(), stdout.getvalue(), stderr.getvalue()

    def test_grant_and_revoke_show_the_servers_reason(self):
        gid = "g_" + "a" * 24
        for argv, code, body, shown in (
                (["grant", "nosuch"], 404, b"No session nosuch on machine m_x. Existing sessions: main.",
                 "rt grant refused (404): No session nosuch on machine m_x. Existing sessions: main."),
                (["revoke", gid], 404, b"No such session grant", "rt revoke refused (404): No such session grant"),
                (["grant", "main"], 429, b"Grant limit reached", "rt grant refused (429): Grant limit reached"),
                # Only printable text: no terminal control sequences from a response body.
                (["revoke", gid], 403, b"\x1b[2JOrigin \x07not allowed", "rt revoke refused (403):  [2JOrigin  not allowed"),
                # A proxy's HTML error page gives way to its status reason.
                (["grant", "main"], 502, b"<html><body>nginx</body></html>", "rt grant refused (502): Bad Gateway")):
            def request(base, path, credential, data=None, ca=None):
                raise urllib.error.HTTPError(base + path, code, "Bad Gateway" if code == 502 else "Error", {}, io.BytesIO(body))
            result, out, err = self.run_cli(argv, request)
            self.assertEqual((result, out), (1, ""), argv)
            self.assertEqual(err.strip(), shown)
            self.assertNotIn("private-owner", err)

    def test_unknown_session_is_created_only_when_asked(self):
        sent = []
        def request(base, path, credential, data=None, ca=None):
            sent.append((path, data))
            return b'{"id": "g_x"}'
        self.assertEqual(self.run_cli(["grant", "main"], request)[0], 0)
        self.assertEqual(self.run_cli(["grant", "scratch", "--create"], request)[0], 0)
        self.assertEqual(sent, [("/api/grants", {"session": "main"}),
                                ("/api/grants", {"session": "scratch", "create": True})])
        calls, child = [], Mock()
        grant = {"id": "g_" + "a" * 24, "token": "capability", "expires": time.time() + 60,
                 "url": "https://owner.term.test/g/" + "g_" + "a" * 24 + "/main"}
        server = self.grant_server(grant, calls)
        def recording(base, path, credential, data=None, ca=None):
            sent.append((path, data))
            return server(base, path, credential, data, ca)
        self.run_main(recording, child, extra=["--create"])
        self.assertIn(("/api/grants", {"session": "main", "create": True}), sent)

    def gateway_capture(self):
        gateways, enter = [], rt.NativeGateway.__enter__
        def capture(gateway):
            gateways.append(gateway)
            return enter(gateway)
        return gateways, patch.object(rt.NativeGateway, "__enter__", capture)

    def test_detach_ends_attach_without_reconnecting(self):
        grant = {"id": "g_" + "a" * 24, "token": "capability", "expires": time.time() + 600,
                 "url": "https://owner.term.test/g/" + "g_" + "a" * 24 + "/main"}
        calls = []
        def request(base, path, credential, data=None, ca=None):
            calls.append(path)
            if path == "/api/state":  # Revoked only after several reconnects: never, when detaching works.
                return json.dumps({"grants": [{**grant, "revoked": "revoked" if calls.count(path) > 3 else None}]}).encode()
            return self.grant_server(grant, [])(base, path, credential, data, ca)
        gateways, capture = self.gateway_capture()
        # Zellij closed the WebSocket on purpose (Ctrl+Alt+g, d): the native client exits once.
        child = Mock(side_effect=lambda *args, **kwargs: setattr(gateways[0], "ended", True) or Mock())
        with capture:
            result, message = self.run_main(request, child)
        self.assertEqual((result, child.call_count), (0, 1))
        self.assertIn("Detached from session main, or it ended; not reconnecting.", message)
        self.assertNotIn("retrying", message)
        self.assertEqual(calls[-1], "/api/revoke")

    def test_dropped_connection_reconnects_and_ctrl_c_while_waiting_stops(self):
        grant = {"id": "g_" + "a" * 24, "token": "capability", "expires": time.time() + 600,
                 "url": "https://owner.term.test/g/" + "g_" + "a" * 24 + "/main"}
        calls = []
        def request(base, path, credential, data=None, ca=None):
            calls.append(path)
            if path == "/api/state":
                return json.dumps({"grants": [{**grant, "revoked": None}]}).encode()
            return self.grant_server(grant, [])(base, path, credential, data, ca)
        child = Mock()  # The connection dropped: no close frame, so the gateway did not see one.
        result, message = self.run_main(request, child, sleep=Mock(side_effect=[None, KeyboardInterrupt]))
        self.assertEqual((result, child.call_count), (0, 2))
        self.assertEqual(message.count("Disconnected; retrying"), 2)
        self.assertIn("Stopped; the session keeps running on the machine.", message)
        self.assertEqual(calls[-1], "/api/revoke")

    def test_closing_the_terminal_window_revokes_the_grant(self):
        grant = {"id": "g_" + "a" * 24, "token": "capability", "expires": 1e12, "permanent": True,
                 "url": "https://owner.term.test/g/" + "g_" + "a" * 24 + "/main"}
        for number in (signal.SIGHUP, signal.SIGTERM):
            with self.subTest(signal=number.name):
                for restore in (signal.SIGHUP, signal.SIGTERM):
                    self.addCleanup(signal.signal, restore, signal.getsignal(restore))
                calls = []
                def request(base, path, credential, data=None, ca=None):
                    calls.append((path, data))
                    if path == "/api/state":
                        return json.dumps({"grants": [{**grant, "revoked": None}]}).encode()
                    return self.grant_server(grant, [])(base, path, credential, data, ca)
                process = Mock()
                # The signal arrives while attached; the second wait is rt terminating the native client.
                waits = iter([lambda: signal.raise_signal(number), lambda: None])
                process.wait.side_effect = lambda: next(waits)()
                result, message = self.run_main(request, Mock(return_value=process))
                self.assertEqual(result, 0)
                process.terminate.assert_called_once()
                self.assertIn("Stopped; the session keeps running on the machine.", message)
                self.assertEqual(calls[-1], ("/api/revoke", {"id": grant["id"]}))

    def test_enrollment_bound_to_requested_environment(self):
        for origin, expected in (("https://term.staging.corp-example.net:8443", 0), ("https://term.dev.corp-example.net", 2)):
            enrolled = {"id": "m_x", "credential": "c" * 40, "relay_host": "relay.staging.corp-example.net",
                        "relay_port": 8443, "environment": "staging", "origin": origin, "domain": "m_x.rt.internal"}
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "machine.json"
                args = ["rt", "enroll", str(target), "--url", "https://term.staging.corp-example.net:8443"]
                with patch.object(sys, "argv", args), patch.object(rt, "request", Mock(return_value=json.dumps(enrolled).encode())), \
                     patch.object(rt.getpass, "getpass", return_value="code"), \
                     contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
                    self.assertEqual(rt.main(), expected, origin)
                self.assertEqual(target.exists(), expected == 0)
                if expected:
                    # The refused credential is live on that server; name it for revocation, never print it.
                    self.assertIn("rt machine-revoke m_x ", stderr.getvalue())
                    self.assertNotIn("c" * 40, stderr.getvalue() + stdout.getvalue())
                if expected == 0:
                    self.assertEqual(json.loads(target.read_text())["relay_port"], 8443)
                    self.assertIn("environment staging", stdout.getvalue())
                    self.assertIn("relay.staging.corp-example.net:8443", stdout.getvalue())


class MachineCLITests(unittest.TestCase):
    """Several machines per owner: rt machines lists them, --machine picks one for grant, attach and machine-revoke."""

    def run_cli(self, argv, request, env=None):
        with tempfile.TemporaryDirectory() as directory:
            credential = Path(directory) / "owner"
            credential.write_text("private-owner")
            args = ["rt", *argv, "--url", "https://term.test", "--credential-file", str(credential)]
            with patch.object(sys, "argv", args), patch.object(rt, "request", request), patch.dict(os.environ, env or {}), \
                    contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
                return rt.main(), stdout.getvalue(), stderr.getvalue()

    def test_machines_lists_names_and_state(self):
        state = {"grants": [], "machines": [
            {"id": "m_" + "a" * 24, "name": "laptop", "enabled": True, "enrolled": 1791900000, "protocol": 1},
            {"id": "m_" + "b" * 24, "name": None, "enabled": False, "enrolled": 1791800000, "protocol": None}]}
        result, out, _ = self.run_cli(["machines"], lambda *args, **kwargs: json.dumps(state).encode())
        self.assertEqual(result, 0)
        self.assertRegex(out, r"m_a{24}\s+laptop\s+enabled\s")
        self.assertRegex(out, r"m_b{24}\s+-\s+revoked\s")
        result, out, _ = self.run_cli(["machines"], lambda *args, **kwargs: b'{"grants": [], "machines": []}')
        self.assertIn("No machine is enrolled yet", out)

    def test_machine_is_sent_with_grants_and_machine_revoke(self):
        sent = []

        def request(base, path, credential, data=None, ca=None):
            sent.append((path, data))
            return b'{"id": "g_x", "ok": true, "machine": "m_x"}'
        self.assertEqual(self.run_cli(["grant", "main", "--machine", "laptop"], request)[0], 0)
        self.assertEqual(self.run_cli(["grant", "main"], request, env={"RT_MACHINE": "desktop"})[0], 0)
        self.assertEqual(self.run_cli(["grant", "main"], request)[0], 0)
        self.assertEqual(self.run_cli(["machine-revoke", "laptop"], request)[0], 0)
        result, out, _ = self.run_cli(["machine-revoke", "--machine", "m_x"], request)
        self.assertIn("Revoked machine m_x", out)
        self.assertEqual(sent, [("/api/grants", {"session": "main", "machine": "laptop"}),
                                ("/api/grants", {"session": "main", "machine": "desktop"}),
                                ("/api/grants", {"session": "main"}),
                                ("/api/machine-revoke", {"machine": "laptop"}),
                                ("/api/machine-revoke", {"machine": "m_x"})])

    def test_several_machines_without_a_choice_shows_the_servers_list(self):
        def request(base, path, credential, data=None, ca=None):
            raise urllib.error.HTTPError(base + path, 409, "Conflict", {}, io.BytesIO(
                b"Several machines are enrolled; choose one with --machine: laptop, desktop"))
        result, _, err = self.run_cli(["grant", "main"], request)
        self.assertEqual(result, 1)
        self.assertIn("choose one with --machine: laptop, desktop", err)

    def test_attach_checks_the_version_of_the_granted_machine(self):
        grant = {"id": "g_" + "a" * 24, "token": "capability", "machine": "m_" + "c" * 24, "expires": 1e12, "permanent": True,
                 "url": "https://owner.term.test/g/" + "g_" + "a" * 24 + "/main"}
        calls = []

        def request(base, path, credential, data=None, ca=None):
            calls.append((path, data))
            if path.startswith("/api/version"):
                return b"0.45.1"
            if path == "/api/grants":
                return json.dumps(grant).encode()
            if path == "/api/state":
                ended = sum(p == "/api/state" for p, _ in calls) > 1
                return json.dumps({"grants": [{**grant, "revoked": "revoked" if ended else None}]}).encode()
            return b'{"ok": true}'
        with tempfile.TemporaryDirectory() as directory:
            credential = Path(directory) / "owner"
            credential.write_text("private-owner")
            args = ["rt", "attach", "main", "--machine", "laptop", "--url", "https://term.test", "--credential-file", str(credential)]
            with patch.object(sys, "argv", args), patch.object(rt, "request", request), \
                    patch.object(rt.subprocess, "check_output", return_value="zellij 0.45.1\n"), \
                    patch.object(rt.subprocess, "Popen", Mock()), patch.object(rt.time, "sleep", Mock()), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(rt.main(), 0)
        paths = [path for path, _ in calls]
        # Before the grant, the machine asked for; afterwards, the one the grant opened.
        self.assertEqual(paths[0], "/api/version?machine=laptop")
        self.assertEqual(calls[1], ("/api/grants", {"session": "main", "machine": "laptop"}))
        self.assertIn("/api/version?machine=m_" + "c" * 24, paths)

    def test_enrollment_names_the_machine_and_prints_the_code_once(self):
        sent = []

        def request(base, path, credential, data=None, ca=None):
            sent.append((path, data))
            return json.dumps({"code": "one-use-code", "expires_in": 300, "name": "laptop"}).encode()
        result, out, _ = self.run_cli(["enrollment", "--name", "laptop"], request)
        self.assertEqual(result, 0)
        self.assertEqual(sent, [("/api/enrollment", {"name": "laptop"})])
        self.assertEqual(out.count("one-use-code"), 1)
        self.assertIn("valid 5 minutes", out)
        self.run_cli(["enrollment"], request)
        self.assertEqual(sent[-1], ("/api/enrollment", {}))


class LoginCLITests(unittest.TestCase):
    """rt login: the owner credential, checked against the server, then saved privately and used by default."""

    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.home = Path(home.name)
        environment = patch.dict(os.environ, {"HOME": home.name})
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("RT_URL", "RT_CREDENTIAL_FILE", "RT_MACHINE"):
            os.environ.pop(name, None)
        self.credential, self.origin, self.calls = "c" * 64, "https://term.test", []

    def server(self, base, path, credential, data=None, ca=None):
        self.calls.append((base, path, credential, data))
        if path == "/api/state":
            if credential != self.credential:
                raise urllib.error.HTTPError(base + path, 401, "Unauthorized", {}, io.BytesIO(b"Owner credential required"))
            return b'{"machines": [], "grants": []}'
        raise AssertionError(path)

    def rt(self, *argv, typed=None, request=None):
        typed = self.credential if typed is None else typed
        with patch.object(sys, "argv", ["rt", *argv]), patch.object(rt, "request", request or self.server), \
                patch.object(rt.sys.stdin, "isatty", return_value=True), \
                patch.object(rt.getpass, "getpass", return_value=typed) as prompt, \
                contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
            result = rt.main()
        return result, stdout.getvalue(), stderr.getvalue(), prompt.call_count

    def mode(self, path):
        return path.stat().st_mode & 0o777

    def test_login_saves_a_private_credential_and_rt_then_just_works(self):
        (self.home / ".config" / "rt").mkdir(parents=True, mode=0o755)
        (self.home / ".config" / "rt").chmod(0o755)
        result, out, err, prompts = self.rt("login", "--url", self.origin)
        self.assertEqual((result, prompts), (0, 1), err)
        self.assertIn("Signed in to term.test", out)
        self.assertIn("No machine is enrolled yet", out)
        self.assertNotIn(self.credential, out + err)
        # Checked against the server before anything is saved.
        self.assertEqual(self.calls, [(self.origin, "/api/state", self.credential, None)])
        directory = self.home / ".config" / "rt" / "term.test"
        self.assertEqual((directory / "credential").read_text(), self.credential + "\n")
        self.assertEqual([self.mode(p) for p in (directory.parent, directory, directory / "credential")], [0o700, 0o700, 0o600])
        self.assertEqual(((directory.parent / "url").read_text(), self.mode(directory.parent / "url")), (self.origin + "\n", 0o600))
        self.assertEqual(sorted(p.name for p in directory.iterdir()), ["credential"])
        # No RT_URL and no RT_CREDENTIAL_FILE: rt finds both, and prompts for nothing.
        self.calls.clear()
        result, out, err, prompts = self.rt("grants")
        self.assertEqual((result, prompts, self.calls), (0, 0, [(self.origin, "/api/state", self.credential, None)]))
        # RT_CREDENTIAL_FILE, when set, still wins.
        owner = self.home / "owner-credential"
        owner.write_text("other-credential\n")
        self.calls.clear()
        self.rt("grants", "--credential-file", str(owner))
        self.assertEqual(self.calls[0][2], "other-credential")

    def test_login_reads_a_piped_credential(self):
        with patch.object(sys, "argv", ["rt", "login", "--url", self.origin]), patch.object(rt, "request", self.server), \
                patch.object(rt.sys, "stdin", io.StringIO(self.credential + "\n")), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(rt.main(), 0)
        self.assertTrue((self.home / ".config/rt/term.test/credential").is_file())

    def test_each_server_keeps_its_own_credential(self):
        self.origin = "https://term.staging.corp-example.net:8443"
        self.assertEqual(self.rt("login", "--url", self.origin)[0], 0)
        self.assertTrue((self.home / ".config/rt/term.staging.corp-example.net:8443/credential").is_file())
        self.assertFalse((self.home / ".config/rt/term.test").exists())

    def test_a_refused_or_malformed_credential_saves_nothing(self):
        for typed, code, shown in (("w" * 64, 1, "rt login refused (401): Owner credential required"),
                                   ("short", 2, "at least 32 characters"),
                                   ("c" * 40 + " " + "c" * 10, 2, "at least 32 characters")):
            result, out, err, _ = self.rt("login", "--url", self.origin, typed=typed)
            self.assertEqual(result, code, typed)
            self.assertIn(shown, err)
            self.assertIn("Nothing was saved", err)
            self.assertFalse((self.home / ".config" / "rt").exists())
            self.assertNotIn(typed, out + err)

    def test_logout_deletes_only_rts_own_file(self):
        self.assertEqual(self.rt("login", "--url", self.origin)[0], 0)
        result, out, _, _ = self.rt("logout")
        self.assertEqual(result, 0)
        self.assertIn("Signed out of term.test", out)
        self.assertFalse((self.home / ".config" / "rt" / "term.test").exists())
        self.assertFalse((self.home / ".config" / "rt" / "url").exists())
        # Nothing left, and no network call either way.
        self.calls.clear()
        self.assertEqual(self.rt("logout", "--url", self.origin)[0], 1)
        self.assertEqual(self.calls, [])
        # A file you named is never deleted.
        owner = self.home / "owner-credential"
        owner.write_text(self.credential + "\n")
        self.assertEqual(self.rt("logout", "--url", self.origin, "--credential-file", str(owner))[0], 1)
        self.assertTrue(owner.exists())
