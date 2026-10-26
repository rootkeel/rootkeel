# SPDX-License-Identifier: AGPL-3.0-only
"""One configuration model: env files, validation errors and the rendered front door."""
import os
import re
import string
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rootkeel import config
from rootkeel.config import ConfigError

ROOT = Path(__file__).resolve().parents[2]
DIGEST = "sha256:" + "a" * 64
VALID = {"RT_ENVIRONMENT": "qa-eu", "RT_PUBLIC_HOST": "shell.qa.corp-example.net",
         "RT_RELAY_HOST": "tunnel.qa.corp-example.net", "RT_HTTPS_BIND": "192.0.2.10:443",
         "RT_STATE_DIR": "/var/lib/rt-qa", "RT_BROKER_IMAGE": "registry.corp-example.net:5000/rt/broker:2.0.1",
         "RT_RELAY_IMAGE": "registry.corp-example.net:5000/rt/relay@" + DIGEST,
         "RT_PROXY_IMAGE": "nginx:1.30.1-alpine@" + DIGEST}


def problems(values):
    try:
        config.env_file(values)
    except ConfigError as error:
        return "\n".join(error.problems)
    raise AssertionError("expected ConfigError")


class EnvFileTests(unittest.TestCase):
    def test_example_is_valid(self):
        values = config.parse_env_file(ROOT / "server" / "example.env")
        settings = config.env_file(values)
        self.assertEqual(settings["origin"], "https://term.example.com")
        self.assertEqual(settings["relay_host"], "term-relay.example.com")
        # Release images by version tag; the front door by digest.
        self.assertIn("@sha256:", values["RT_PROXY_IMAGE"])

    def test_non_default_values_and_derived_origin(self):
        settings = config.env_file(VALID | {"RT_PUBLIC_PORT": "8443"})
        self.assertEqual(settings["origin"], "https://shell.qa.corp-example.net:8443")
        self.assertEqual(config.env_file(VALID)["origin"], "https://shell.qa.corp-example.net")

    def test_every_missing_key_reported_together(self):
        report = problems({})
        for key in config.REQUIRED_ENV_FILE:
            self.assertIn(key + " is required", report)

    def test_invalid_values_named(self):
        cases = {
            "RT_PUBLIC_HOST": ("192.0.2.44", "must be a DNS name, not an IP address"),
            "RT_RELAY_HOST": ("tunnel", "is not a valid DNS name"),
            "RT_ENVIRONMENT": ("Prod", "lowercase"),
            "RT_PUBLIC_PORT": ("70000", "TCP port"),
            "RT_HTTPS_BIND": ("example.com:443", "RT_HTTPS_BIND"),
            "RT_STATE_DIR": ("relative/state", "absolute path"),
            "RT_BROKER_IMAGE": ("registry.corp-example.net/rt/broker:latest", "moving tag"),
            "RT_RELAY_IMAGE": ("registry.corp-example.net/rt/relay", "neither a tag nor a digest"),
            "RT_HSTS_MAX_AGE": ("-1", "RT_HSTS_MAX_AGE"),
        }
        for key, (value, message) in cases.items():
            self.assertIn(message, problems(VALID | {key: value}), key)

    def test_hostnames_must_differ_and_stay_public(self):
        self.assertIn("must differ", problems(VALID | {"RT_RELAY_HOST": "Shell.QA.corp-example.net."}))
        self.assertIn(".rt.internal", problems(VALID | {"RT_PUBLIC_HOST": "m_x.rt.internal"}))

    def test_digest_required_when_configured(self):
        report = problems(VALID | {"RT_REQUIRE_IMAGE_DIGEST": "true"})
        self.assertIn("RT_BROKER_IMAGE must be pinned by digest", report)
        self.assertNotIn("RT_RELAY_IMAGE", report)

    def test_env_file_parsing_is_literal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env"
            path.write_text("# comment\nexport RT_ENVIRONMENT='dev'\nRT_PUBLIC_HOST=term.example.org # inline\n")
            self.assertEqual(config.parse_env_file(path), {"RT_ENVIRONMENT": "dev", "RT_PUBLIC_HOST": "term.example.org"})
            path.write_text("RT_PUBLIC_HOST=${DOMAIN}\nRT_PUBLIC_HOST=x\nnot a line\n")
            with self.assertRaises(ConfigError) as error:
                config.parse_env_file(path)
            self.assertEqual(len(error.exception.problems), 3)

    def test_cli_reports_problems_and_exit_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env"
            path.write_text("".join(f"{k}={v}\n" for k, v in (VALID | {"RT_STATE_DIR": directory}).items()))
            with patch("sys.stdout"):
                self.assertEqual(config.main(["config", "check-env", str(path)]), 0)
            path.write_text("RT_ENVIRONMENT=dev\n")
            with patch("sys.stderr") as stderr:
                self.assertEqual(config.main(["config", "check-env", str(path)]), 2)
            self.assertIn("RT_PUBLIC_HOST is required", "".join(c.args[0] for c in stderr.write.call_args_list))


class GrantLimitTests(unittest.TestCase):
    def test_no_time_limit_unless_the_operator_sets_one(self):
        self.assertEqual(config.grant_limits({}), {"idle": 0, "lifetime": 0})
        self.assertEqual(config.grant_limits({"RT_GRANT_IDLE_TIMEOUT": "", "RT_GRANT_MAX_LIFETIME": "0"}),
                         {"idle": 0, "lifetime": 0})
        self.assertEqual(config.grant_limits({"RT_GRANT_IDLE_TIMEOUT": "900", "RT_GRANT_MAX_LIFETIME": " 28800 "}),
                         {"idle": 900, "lifetime": 28800})
        with self.assertRaises(ConfigError) as error:
            config.grant_limits({"RT_GRANT_IDLE_TIMEOUT": "59", "RT_GRANT_MAX_LIFETIME": "1h"})
        self.assertEqual(len(error.exception.problems), 2)
        self.assertIn("RT_GRANT_IDLE_TIMEOUT must be whole seconds, 0 (no limit, the default) or at least 60",
                      str(error.exception))
        self.assertIn("RT_GRANT_MAX_LIFETIME", problems(VALID | {"RT_GRANT_MAX_LIFETIME": "-5"}))


class TerminalHostTests(unittest.TestCase):
    def test_the_owners_terminal_host_is_owner_dot_public_host(self):
        settings = config.server({"RT_ENVIRONMENT": "prod", "RT_PUBLIC_HOST": "Term.Example.net",
                                  "RT_RELAY_HOST": "relay.example.net"})
        self.assertEqual(settings["terminal_host"], "owner.term.example.net")
        with self.assertRaisesRegex(ConfigError, "RT_RELAY_HOST must not be owner.term.example.net"):
            config.server({"RT_ENVIRONMENT": "prod", "RT_PUBLIC_HOST": "term.example.net",
                           "RT_RELAY_HOST": "owner.term.example.net"})


class BrokerConfigTests(unittest.TestCase):
    def test_broker_refuses_incomplete_environment(self):
        from rootkeel.broker import Broker
        with tempfile.TemporaryDirectory() as directory:
            owner = Path(directory) / "owner"
            owner.write_text("o" * 40)
            base = {"RT_OWNER_FILE": str(owner), "RT_DB": directory + "/db", "RT_RELAY_VHOST_URL": "http://relay:8080",
                    "RT_ENVIRONMENT": "dev", "RT_PUBLIC_HOST": "term.example.org", "RT_RELAY_HOST": "term-relay.example.org"}
            for change, message in (({"RT_PUBLIC_HOST": ""}, "RT_PUBLIC_HOST is required"),
                                    ({"RT_RELAY_VHOST_URL": ""}, "RT_RELAY_VHOST_URL is required"),
                                    ({"RT_RELAY_HOST": "term.example.org"}, "must differ")):
                with patch.dict(os.environ, base | change, clear=True), self.assertRaises(ConfigError) as error:
                    Broker()
                self.assertIn(message, str(error.exception))


def nginx_blocks(text):
    """Parse nginx configuration into [(directive, args, children-or-None)]."""
    tokens = iter(token for token in re.findall(r'#[^\n]*|"[^"]*"|[{};]|[^\s{};"]+', text) if token[0] != "#")

    def block():
        tree, args = [], []
        for token in tokens:
            if token in ";{":
                tree.append((args[0], args[1:], block() if token == "{" else None))
                args = []
            elif token == "}":
                return tree
            else:
                args.append(token.strip('"'))
        return tree
    return block()


def directives(tree, name):
    return [(args, children) for directive, args, children in tree if directive == name]


class FrontDoorTests(unittest.TestCase):
    def servers(self, **env):
        # The nginx image's envsubst step: only RT_* names (NGINX_ENVSUBST_FILTER) are
        # substituted; nginx's own $variables pass through.
        text = (ROOT / "server" / "config" / "site.conf.template").read_text()
        tree = nginx_blocks(string.Template(text).safe_substitute(env))
        servers = {}
        for _, server in directives(tree, "server"):
            self.assertEqual(directives(server, "listen"), [(["443", "ssl"], None)])
            for args, _ in directives(server, "server_name"):
                servers[" ".join(args)] = server
        return servers

    def test_template_routes_only_configured_hosts(self):
        servers = self.servers(RT_PUBLIC_HOST="shell.qa.corp-example.net", RT_RELAY_HOST="tunnel.qa.corp-example.net",
                               RT_HSTS_MAX_AGE="300")
        public_names = "shell.qa.corp-example.net owner.shell.qa.corp-example.net"
        self.assertEqual(set(servers), {"tunnel.qa.corp-example.net", public_names})
        relay, public = servers["tunnel.qa.corp-example.net"], servers[public_names]
        # The tunnel path exists only on the relay host; the broker only on the public host.
        relay_locations = dict((tuple(args), body) for args, body in directives(relay, "location"))
        self.assertEqual(set(relay_locations), {("=", "/~!frp"), ("/",)})
        self.assertIn((["$relay", "relay:7000"], None), directives(relay_locations[("=", "/~!frp")], "set"))
        self.assertEqual(relay_locations[("/",)], [("return", ["404"], None)])
        (public_location, body), = directives(public, "location")
        self.assertEqual(public_location, ["/"])
        self.assertIn((["$broker", "broker:8080"], None), directives(body, "set"))
        self.assertIn((["Strict-Transport-Security", "max-age=300", "always"], None), directives(public, "add_header"))

