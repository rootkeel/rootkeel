# SPDX-License-Identifier: AGPL-3.0-only
"""One configuration model for every environment and packaging target.

Standard library only: the broker, agent, `rt` and the Compose wrapper all
validate the same settings here, so a bad domain, port or image pin fails with
the same message wherever it is first used. Nothing here names a real domain,
address, registry or machine; every environment supplies its own values.
"""
import ipaddress
import re
import sys
from pathlib import Path

# Private frp virtual-host suffix. Never resolved by DNS and never public; each
# environment's relay is its own namespace, so this does not vary per environment.
INTERNAL_SUFFIX = ".rt.internal"
DEFAULT_PORT = 443
# The owner's terminal pages have their own origin, apart from the API's: <TERMINAL_LABEL>.<RT_PUBLIC_HOST>.
TERMINAL_LABEL = "owner"
DEFAULT_HSTS = 31536000
MAX_HSTS = 63072000
ENVIRONMENT = re.compile(r"[a-z][a-z0-9-]{0,29}\Z")
LABEL = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)\Z")
# Tags that name a moving target rather than one build. Promotion changes a pin.
MUTABLE_TAGS = {"latest", "stable", "main", "master", "edge", "nightly", "dev", "development",
                "staging", "stage", "prod", "production", "release", "current", "head"}
REGISTRY = re.compile(r"[A-Za-z0-9.-]+(?::\d+)?\Z")
PATH_COMPONENT = re.compile(r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*\Z")
TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")
DIGEST = re.compile(r"sha256:[a-f0-9]{64}\Z")
REQUIRED_ENV_FILE = ("RT_ENVIRONMENT", "RT_PUBLIC_HOST", "RT_RELAY_HOST", "RT_STATE_DIR",
                     "RT_HTTPS_BIND", "RT_BROKER_IMAGE", "RT_RELAY_IMAGE", "RT_PROXY_IMAGE")


class ConfigError(ValueError):
    """Every problem found, one per line, each naming the setting to fix."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("\n".join(self.problems))


def is_ip(text):
    try:
        ipaddress.ip_address(text.strip("[]"))
        return True
    except ValueError:
        return False


def hostname(value, name, example="term.example.com"):
    host = (value or "").strip().rstrip(".").lower()
    if not host:
        raise ConfigError([f"{name} is required: the DNS name clients use, for example {example}"])
    if is_ip(host):
        raise ConfigError([f"{name} must be a DNS name, not an IP address ({value!r})"])
    labels = host.split(".")
    if len(host) > 253 or len(labels) < 2 or not all(LABEL.match(label) for label in labels):
        raise ConfigError([f"{name} is not a valid DNS name: {value!r} (letters, digits, hyphens and "
                           "at least one dot; no scheme, port, path or wildcard)"])
    if host.endswith(INTERNAL_SUFFIX) or host == INTERNAL_SUFFIX[1:]:
        raise ConfigError([f"{name} must not use the private relay suffix {INTERNAL_SUFFIX}"])
    return host


def port(value, name, default=DEFAULT_PORT):
    if value in (None, ""):
        return default
    try:
        number = int(str(value).strip())
        if 1 <= number <= 65535 and str(number) == str(value).strip():
            return number
    except ValueError:
        pass
    raise ConfigError([f"{name} must be a TCP port 1..65535, got {value!r}"])


def environment_name(value):
    if not value:
        raise ConfigError(["RT_ENVIRONMENT is required: a short lowercase name such as dev, staging or prod"])
    if not ENVIRONMENT.match(value):
        raise ConfigError([f"RT_ENVIRONMENT must be lowercase letters, digits and hyphens, starting with a "
                           f"letter, at most 30 characters; got {value!r}"])
    return value


def origin(host, public_port):
    return "https://" + host + ("" if public_port == DEFAULT_PORT else f":{public_port}")


def image_ref(value, name, require_digest=False):
    """Accept a reference pinned to one build: a digest, or a version tag that is not a moving name."""
    text = (value or "").strip()
    if not text:
        raise ConfigError([f"{name} is required: an image reference pinned by digest or immutable tag"])
    rest, _, digest = text.partition("@")
    repository, tag = rest, ""
    if ":" in rest.rsplit("/", 1)[-1]:
        repository, tag = rest.rsplit(":", 1)
    parts = repository.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        valid_registry, parts = bool(REGISTRY.match(parts[0])), parts[1:]
    else:
        valid_registry = True
    if (not valid_registry or not all(PATH_COMPONENT.match(p) for p in parts)
            or (tag and not TAG.match(tag)) or ("@" in text and not DIGEST.match(digest))):
        raise ConfigError([f"{name} is not an image reference: {value!r}"])
    if require_digest and not digest:
        raise ConfigError([f"{name} must be pinned by digest (...@sha256:<64 hex>) because "
                           f"RT_REQUIRE_IMAGE_DIGEST is true; got {value!r}"])
    if not digest and not tag:
        raise ConfigError([f"{name} has neither a tag nor a digest; pin one build, for example "
                           f"{repository}:1.2.3 or {repository}@sha256:<digest>"])
    if tag.lower() in MUTABLE_TAGS and not digest:
        raise ConfigError([f"{name} uses the moving tag {tag!r}; pin an immutable version tag or a digest "
                           "so promotion changes the pin instead of rebuilding"])
    return text


def boolean(value, name, default=False):
    if value in (None, ""):
        return default
    lowered = str(value).strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ConfigError([f"{name} must be true or false, got {value!r}"])


def hsts(value):
    if value in (None, ""):
        return DEFAULT_HSTS
    try:
        number = int(str(value).strip())
        if 0 <= number <= MAX_HSTS:
            return number
    except ValueError:
        pass
    raise ConfigError([f"RT_HSTS_MAX_AGE must be seconds 0..{MAX_HSTS}, got {value!r}"])


GRANT_LIMITS = (("idle", "RT_GRANT_IDLE_TIMEOUT"), ("lifetime", "RT_GRANT_MAX_LIFETIME"))
MIN_GRANT_LIMIT = 60


def grant_limits(env):
    """Operator time limits on session grants, in seconds; 0 (the default) means none.

    A terminal is meant to stay: by default no time or inactivity rule ends a
    grant, only an administrative act (revoke, machine revoke, the operator's
    rootkeel.admin). These are opt-in administrative policy for operators who
    must bound access anyway: idle ends a grant that long after its last
    terminal input, lifetime that long after issuance (nothing extends it).
    """
    limits, problems = {}, []
    for key, name in GRANT_LIMITS:
        value = str(env.get(name) or "0").strip()
        if not re.fullmatch(r"\d{1,10}", value) or 0 < int(value) < MIN_GRANT_LIMIT:
            problems.append(f"{name} must be whole seconds, 0 (no limit, the default) or at least "
                            f"{MIN_GRANT_LIMIT}; got {env.get(name)!r}")
            continue
        limits[key] = int(value)
    if problems:
        raise ConfigError(problems)
    return limits


def networks(value, name):
    """Comma-separated addresses or CIDR ranges, such as the front door's; empty means none."""
    found = []
    for item in (value or "").split(","):
        if item.strip():
            try:
                found.append(ipaddress.ip_network(item.strip(), strict=False))
            except ValueError:
                raise ConfigError([f"{name} must be IP addresses or CIDR ranges separated by commas; got {item.strip()!r}"]) from None
    return found


def collect(checks):
    """Run every check and report all problems at once rather than the first."""
    problems, values = [], []
    for check in checks:
        try:
            values.append(check())
        except ConfigError as error:
            problems.extend(error.problems)
            values.append(None)
    if problems:
        raise ConfigError(problems)
    return values


def server(env):
    """Settings shared by every server process of one environment."""
    name, public_host, public_port, relay_host, max_age = collect([
        lambda: environment_name(env.get("RT_ENVIRONMENT", "")),
        lambda: hostname(env.get("RT_PUBLIC_HOST"), "RT_PUBLIC_HOST"),
        lambda: port(env.get("RT_PUBLIC_PORT"), "RT_PUBLIC_PORT"),
        lambda: hostname(env.get("RT_RELAY_HOST"), "RT_RELAY_HOST", "relay.term.example.com"),
        lambda: hsts(env.get("RT_HSTS_MAX_AGE")),
    ])
    if public_host == relay_host:
        raise ConfigError(["RT_RELAY_HOST must differ from RT_PUBLIC_HOST: the front door routes the agent "
                           "tunnel by hostname"])
    terminal_host = TERMINAL_LABEL + "." + public_host
    if len(terminal_host) > 253:
        raise ConfigError([f"RT_PUBLIC_HOST is too long for the owner's terminal hostname {terminal_host}"])
    if relay_host == terminal_host:
        raise ConfigError([f"RT_RELAY_HOST must not be {terminal_host}: that is the owner's terminal hostname"])
    return {"environment": name, "public_host": public_host, "public_port": public_port,
            "origin": origin(public_host, public_port), "relay_host": relay_host, "terminal_host": terminal_host,
            "hsts_max_age": max_age}


def bind(value):
    text = (value or "").strip()
    match = re.fullmatch(r"(\[[0-9a-fA-F:]+\]|[0-9.]+):(\d+)", text)
    if not match:
        raise ConfigError([f"RT_HTTPS_BIND must be <address>:<port> on this host, for example 0.0.0.0:443; "
                           f"got {value!r}"])
    if not is_ip(match[1]):
        raise ConfigError([f"RT_HTTPS_BIND address is not an IP address: {match[1]!r}"])
    port(match[2], "RT_HTTPS_BIND port")
    return text


def absolute(value, name):
    if not value or not value.startswith("/"):
        raise ConfigError([f"{name} must be an absolute path, got {value!r}"])
    return value


def state_dir(value):
    absolute(value, "RT_STATE_DIR")
    if value.rstrip("/") == "":
        raise ConfigError(["RT_STATE_DIR must be a dedicated directory, not /"])
    return value


def parse_env_file(path):
    """KEY=VALUE lines with optional quotes and # comments; values are literal, never interpolated."""
    values, problems = {}, []
    for number, raw in enumerate(Path(path).read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            problems.append(f"{path}:{number}: expected KEY=VALUE")
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if "$" in value:
            problems.append(f"{path}:{number}: {key} must be a literal value without $ interpolation")
        if key in values:
            problems.append(f"{path}:{number}: {key} is set twice")
        values[key] = value
    if problems:
        raise ConfigError(problems)
    return values


def env_file(values):
    """Validate one environment file for the single-host Compose target."""
    missing = [key for key in REQUIRED_ENV_FILE if not values.get(key)]
    if missing:
        raise ConfigError([f"{key} is required in the environment file" for key in missing])
    require_digest = boolean(values.get("RT_REQUIRE_IMAGE_DIGEST"), "RT_REQUIRE_IMAGE_DIGEST")
    checks = [
        lambda: server(values),
        lambda: bind(values["RT_HTTPS_BIND"]),
        lambda: state_dir(values["RT_STATE_DIR"]),
        lambda: grant_limits(values),
    ]
    for key in ("RT_BROKER_IMAGE", "RT_RELAY_IMAGE", "RT_PROXY_IMAGE"):
        checks.append(lambda key=key: image_ref(values[key], key, require_digest))
    for key in ("RT_TLS_CERT_FILE", "RT_TLS_KEY_FILE"):
        if values.get(key):
            checks.append(lambda key=key: absolute(values[key], key))
    return collect(checks)[0]


USAGE = "usage: python3 -m rootkeel.config check-env <env-file> | get <env-file> <KEY>"


def main(argv):
    """check-env validates a file; get serves the shell scripts."""
    command, args = (argv[1], argv[2:]) if len(argv) > 1 else ("", [])
    if (command, len(args)) not in (("check-env", 1), ("get", 2)):
        print(USAGE, file=sys.stderr)
        return 64
    try:
        values = parse_env_file(args[0])
        settings = env_file(values)
    except (ConfigError, OSError) as error:
        print(f"Invalid environment file {args[0]}:", file=sys.stderr)
        for problem in getattr(error, "problems", [str(error)]):
            print("  - " + problem, file=sys.stderr)
        return 2
    if command == "get":
        # Normalized forms for derived or case-folded settings; raw text otherwise.
        derived = {"RT_PUBLIC_HOST": settings["public_host"], "RT_RELAY_HOST": settings["relay_host"],
                   "RT_PUBLIC_PORT": str(settings["public_port"]), "RT_ORIGIN": settings["origin"]}
        print(derived.get(args[1], values.get(args[1], "")))
    else:
        print(f"OK {settings['environment']}: {settings['origin']} relay {settings['relay_host']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
