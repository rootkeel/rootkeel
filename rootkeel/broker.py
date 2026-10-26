# SPDX-License-Identifier: AGPL-3.0-only
"""The broker: owner API, machine enrollment, private relay policy and session grant enforcement."""
import asyncio
import contextlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, DummyCookieJar, web
from . import config
from .store import Store, digest
from .transport import MAX_BODY, bridge, forward

# The Zellij release the agents and rt run; /api/version reports the machine's.
VERSION = "0.45.1"
# The oldest agent protocol (rootkeel.agent.PROTOCOL) this broker serves; an older agent is told to update.
MIN_AGENT_PROTOCOL = 1
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
GRANT_PATH = re.compile(r"/g/(g_[0-9a-f]{24})(/.*)\Z")
# The single owner's id in the store and audit.
OWNER_ID = "owner"
# Open grants per owner; each holds a Zellij token. Grants have no end by default, so the owner revokes old ones.
MAX_GRANTS = 32
# A browser keeps a grant's cookie this long (browsers cap cookies near 400 days); each page load renews it.
COOKIE_AGE = 400 * 86400
# The API's expires for a grant with no lifetime limit: far enough that a numeric check never ends it.
PERMANENT_EXPIRES = 100 * 365 * 86400
# Stored sign-ins per grant; a new sign-in beyond this evicts the least recently used one.
MAX_COOKIES = 32
# Enrolled machines per owner.
MAX_MACHINES = 64
ENROLLMENT_SECONDS = 300
# Authentication attempts (grant sign-ins, enrollment) allowed per client address in any minute, and how many
# client addresses are remembered; beyond that the least recently seen is forgotten.
AUTH_ATTEMPTS = 120
AUTH_CLIENTS = 1024
# How long the front door's resolved addresses are trusted before they are looked up again.
FRONT_DOOR_SECONDS = 30

STATIC = Path(__file__).resolve().parent / "static"
# The look of the broker's own pages.
SHELL_CSS = STATIC / "terminal.css"
# Copy and paste on a phone, which Zellij's web client does not offer there: loaded by the terminal page, under its grant.
MOBILE_JS = STATIC / "terminal-mobile.js"
MOBILE_SCRIPT = b'<script src="./_rt_mobile.js" defer></script>'
# A full-screen button on every device, likewise.
FULLSCREEN_JS = STATIC / "terminal-fullscreen.js"
FULLSCREEN_SCRIPT = b'<script src="./_rt_fullscreen.js" defer></script>'

SIGNIN_JS = """document.querySelector('form').addEventListener('submit', async event => {
event.preventDefault(); const input = document.querySelector('input');
try { const response = await fetch('./command/login', {method: 'POST', credentials: 'same-origin',
headers: {'Content-Type': 'application/json'}, body: JSON.stringify({auth_token: input.value})});
input.value = ''; if (!response.ok) throw Error(); location.reload(); }
catch { document.querySelector('[role=status]').textContent = 'Grant invalid or ended. Request a new session.'; }
});"""

# Zellij's client reconnects by reloading its page, often while the network is still changing (a Wi-Fi switch):
# Chrome then fails the reload's requests with ERR_NETWORK_CHANGED. A failed script or stylesheet leaves a blank page
# that never retries; this reloads it again, backing off as Zellij does to 16 seconds, at most 5 times in a row.
RELOAD_GUARD = b"""<script>(() => {
  const key = 'rt-page-retries', state = {};
  const retries = () => { try { return +sessionStorage.getItem(key) || 0; } catch { return 5; } };
  const retry = () => {
    const n = retries();
    if (state.retrying || n >= 5) return;
    state.retrying = true;
    try { sessionStorage.setItem(key, n + 1); } catch {}
    setTimeout(() => location.reload(), 1000 * Math.min(16, 2 ** n));
  };
  addEventListener('error', event => {
    const element = event.target;
    if (element instanceof HTMLScriptElement || (element instanceof HTMLLinkElement && element.rel === 'stylesheet')) retry();
  }, true);
  setTimeout(() => { if (!state.retrying) try { sessionStorage.removeItem(key); } catch {} }, 30000);
})();</script>"""


def internal_url(value):
    parts = urlsplit(value or "")
    if parts.scheme != "http" or not parts.hostname or parts.path not in ("", "/") or parts.query or parts.username:
        raise config.ConfigError(["RT_RELAY_VHOST_URL is required: the relay's private vhost listener, for "
                                  "example http://relay:8080"])
    return value.rstrip("/")


def machine_name(value):
    """An optional machine name: a session-style name that cannot be mistaken for a machine id."""
    if value is None:
        return None
    if not isinstance(value, str) or not NAME.fullmatch(value) or value.startswith("m_"):
        raise web.HTTPBadRequest(text="A machine name is letters, digits, hyphens and underscores (at most 64), "
                                      "starting with a letter or digit and not with m_")
    return value


class Broker:
    def __init__(self):
        settings = config.server(os.environ)
        self.relay = internal_url(os.environ.get("RT_RELAY_VHOST_URL"))
        # The single owner's credential, generated by scripts/prepare.sh; rt login stores it on the owner's computer.
        self.owner = Path(os.environ["RT_OWNER_FILE"]).read_text().strip()
        if len(self.owner) < 32:
            raise ValueError("Owner credential must contain at least 32 characters")
        self.owner_id = OWNER_ID
        self.store = Store(os.environ.get("RT_DB", "/data/broker.sqlite"), limits=config.grant_limits(os.environ))
        self.environment = settings["environment"]
        self.origin = settings["origin"]
        self.port = settings["public_port"]
        self.relay_host = settings["relay_host"]
        self.public_host = settings["public_host"]
        # The owner's terminal has its own origin, apart from the API's.
        self.terminal_host = settings["terminal_host"]
        self.terminal_origin = config.origin(self.terminal_host, self.port)
        # The bundled front door's hostname: only its X-Forwarded-For names the client.
        self.front_door = os.environ.get("RT_FRONT_DOOR_HOST", "")
        self.front_door_addresses, self.front_door_checked = set(), None
        self.security_headers = {"Strict-Transport-Security": "max-age=%d" % settings["hsts_max_age"],
                                 "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"}
        self.sockets = {}  # grant -> {downstream socket: transport}
        self.cookie_sockets = {}  # cookie hash -> {downstream socket: transport}
        self.locks = {}
        self.attempts = {}  # client address -> deque of attempt times, least recently seen first
        self.client = None
        self.tasks = []

    def host_owner(self, value):
        """None on the public host, the owner on the owner's terminal host; any other host is refused."""
        name, suffix = value.lower(), ":" + str(self.port)
        if name.endswith(suffix):
            name = name[:-len(suffix)]
        elif self.port != config.DEFAULT_PORT:
            raise web.HTTPForbidden(text="Host not allowed")
        if name == self.public_host:
            return None
        if name != self.terminal_host:
            raise web.HTTPForbidden(text="Host not allowed")
        return self.owner_id

    def owner_auth(self, request):
        if not hmac.compare_digest(request.headers.get("Authorization", "").encode(), ("Bearer " + self.owner).encode()):
            raise web.HTTPUnauthorized(text="Owner credential required")
        return self.owner_id

    async def json_body(self, request):
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except (ValueError, json.JSONDecodeError):
            raise web.HTTPBadRequest(text="JSON object required") from None

    def expiry(self, grant):
        """The API's expires and permanent. A permanent grant (no operator lifetime limit, the default) still reports a
        numeric expires, issued + 100 years, so a client comparing it with the clock keeps the grant."""
        ends = self.store.ends(grant)
        return {"expires": grant["issued"] + PERMANENT_EXPIRES if ends is None else ends, "permanent": ends is None}

    def grant_view(self, grant):
        """A grant as the owner sees it. expires and permanent: see expiry; idle is the operator's limit, null when none
        (the default); last_active is the last terminal input (never content), None before any."""
        return {"id": grant["id"], "machine": grant["machine"], "machine_name": grant["machine_name"],
                "session": grant["session"], "issued": grant["issued"], **self.expiry(grant),
                "idle": self.store.limits["idle"] or None, "revoked": grant["revoked"],
                "backend_name": grant["backend_name"],
                "last_active": grant["last_input"] if grant["last_input"] > grant["issued"] else None}

    def owner_grants(self, owner):
        """The owner's grants, newest first."""
        rows = self.store.all("SELECT g.id FROM grants g JOIN machines m ON m.id=g.machine WHERE m.owner=? "
                              "ORDER BY g.issued DESC", (owner,))
        return [self.grant_view(self.store.grant(row["id"])) for row in rows]

    def owner_machines(self, owner):
        """The owner's machines, enabled ones first, each with the agent protocol of its last relay login."""
        rows = self.store.db.execute("SELECT m.id, m.name, m.enabled, m.enrolled, l.protocol FROM machines m "
                                     "LEFT JOIN relay_logins l ON l.machine=m.id WHERE m.owner=? "
                                     "ORDER BY m.enabled DESC, m.enrolled", (owner,)).fetchall()
        return [{"id": r["id"], "name": r["name"], "enabled": bool(r["enabled"]), "enrolled": r["enrolled"],
                 "protocol": r["protocol"]} for r in rows]

    def headers(self, machine, cookie=None):
        headers = {"Host": machine["id"] + config.INTERNAL_SUFFIX, "Authorization": "Bearer " + machine["secret"],
                   "Accept-Encoding": "identity"}
        if cookie:
            headers["Cookie"] = "session_token=" + cookie
        return headers

    def machine(self, owner, ref=None):
        """An enabled machine of this owner: the one a request names by id or name, else the only one."""
        if ref is not None and (not isinstance(ref, str) or not ref):
            raise web.HTTPBadRequest(text="machine must be a machine id or name")
        machines = self.store.all("SELECT * FROM machines WHERE enabled=1 AND owner=? ORDER BY enrolled", (owner,))
        if ref:
            for machine in machines:
                if ref in (machine["id"], machine["name"]):
                    return machine
            raise web.HTTPNotFound(text=f"No enrolled machine {ref}")
        if not machines:
            raise web.HTTPServiceUnavailable(text="No enrolled machine")
        if len(machines) > 1:
            listed = ", ".join(m["name"] or m["id"] for m in machines[:10])
            raise web.HTTPConflict(text=f"Several machines are enrolled; choose one with --machine: {listed}")
        return machines[0]

    def name_taken(self, owner, name):
        return name is not None and self.store.one("SELECT id FROM machines WHERE enabled=1 AND owner=? AND name=?",
                                                   (owner, name)) is not None

    async def agent(self, machine, path, data):
        try:
            async with self.client.post(self.relay + path, headers=self.headers(machine), json=data) as response:
                if response.status != 200:
                    raise web.HTTPServiceUnavailable(text="Machine unavailable")
                return await response.json()
        except (ClientError, asyncio.TimeoutError, ValueError):
            raise web.HTTPServiceUnavailable(text="Machine unavailable") from None

    def agent_protocol(self, mid):
        """The protocol the machine's agent announced at its last relay login; None before its first."""
        row = self.store.one("SELECT protocol FROM relay_logins WHERE machine=?", (mid,))
        return row["protocol"] if row else None

    async def sessions(self, machine):
        """The machine's Zellij session names."""
        try:
            async with self.client.post(self.relay + "/_agent/sessions", headers=self.headers(machine), json={}) as response:
                listed = (await response.json(content_type=None)).get("sessions") if response.status == 200 else None
        except (ClientError, asyncio.TimeoutError, ValueError, AttributeError):
            listed = None
        if not isinstance(listed, list):
            raise web.HTTPServiceUnavailable(text="Machine unavailable")
        return [name for name in listed if isinstance(name, str) and NAME.fullmatch(name)]

    async def backend_login(self, grant, remint=True):
        machine = {"id": grant["machine"], "secret": grant["secret"]}
        try:
            async with self.client.post(self.relay + "/command/login", headers=self.headers(machine),
                                        json={"auth_token": grant["backend_token"], "remember_me": False}) as response:
                if response.status == 200 and "session_token" in response.cookies:
                    return response.cookies["session_token"].value
                if response.status != 401 or not remint:
                    raise web.HTTPServiceUnavailable(text="Backend login failed")
        except (ClientError, asyncio.TimeoutError):
            raise web.HTTPServiceUnavailable(text="Machine unavailable") from None
        return await self.backend_login(await self.remint(grant), remint=False)

    async def remint(self, grant):
        """The agent revokes every Zellij token when it restarts; a live grant gets a fresh one."""
        async with self.locks.setdefault(grant["id"], asyncio.Lock()):
            current = self.store.grant(grant["id"])
            if self.store.invalid(current):
                raise web.HTTPUnauthorized(text="Grant ended")
            if current["backend_token"] == grant["backend_token"]:
                token = await self.agent({"id": grant["machine"], "secret": grant["secret"]}, "/_agent/token",
                                         {"grant_id": grant["id"]})
                # Cleanup must target the newly minted token, even if the grant ended meanwhile.
                self.store.execute("UPDATE grants SET backend_name=?,backend_token=?,cleaned=0 WHERE id=?",
                                   (token["name"], self.store.seal(token["token"], "grants.backend_token", grant["id"]), grant["id"]))
                current = self.store.grant(grant["id"])
                if self.store.invalid(current):
                    raise web.HTTPUnauthorized(text="Grant ended")
            return current

    async def admin(self, request):
        action = request.match_info["action"]
        owner = self.owner_auth(request)
        if request.method == "GET" and action == "audit":
            return web.json_response([dict(r) for r in self.store.all(
                "SELECT * FROM audit WHERE owner=? ORDER BY seq DESC LIMIT 1000", (owner,))])
        if request.method == "GET" and action == "state":
            return web.json_response({"machines": self.owner_machines(owner), "grants": self.owner_grants(owner)})
        if request.method == "GET" and action == "version":
            machine = self.machine(owner, request.query.get("machine"))
            response = await forward(request, self.client, self.relay + "/info/version", self.headers(machine))
            if response.status != 200:
                raise web.HTTPServiceUnavailable(text="Machine unavailable")
            return response
        if request.method != "POST":
            raise web.HTTPMethodNotAllowed(request.method, ["POST"])
        data = await self.json_body(request)
        if action == "enrollment":
            name = machine_name(data.get("name"))
            if self.name_taken(owner, name):
                raise web.HTTPConflict(text=f"An enrolled machine is already named {name}")
            if self.store.one("SELECT COUNT(*) AS n FROM machines WHERE enabled=1 AND owner=?", (owner,))["n"] >= MAX_MACHINES:
                raise web.HTTPConflict(text=f"Machine limit reached: {MAX_MACHINES} enrolled machines. Revoke one first")
            code = secrets.token_urlsafe(32)
            self.store.execute("INSERT INTO enrollments(hash,expires,owner,name) VALUES(?,?,?,?)",
                               (digest(code), time.time() + ENROLLMENT_SECONDS, owner, name))
            self.store.audit("enrollment-issued", owner=owner)
            return web.json_response({"code": code, "expires_in": ENROLLMENT_SECONDS, "name": name})
        if action == "grants":
            machine = self.machine(owner, data.get("machine"))
            session = data.get("session", "")
            if not isinstance(session, str) or not NAME.fullmatch(session):
                raise web.HTTPBadRequest(text="Invalid session name")
            create = data.get("create", False)
            if not isinstance(create, bool):
                raise web.HTTPBadRequest(text="create must be true or false")
            open_grants = self.store.one("SELECT COUNT(*) AS n FROM grants g JOIN machines m ON m.id=g.machine "
                                         "WHERE g.revoked IS NULL AND m.owner=?", (owner,))["n"]
            if open_grants >= MAX_GRANTS:
                raise web.HTTPTooManyRequests(text=f"Grant limit reached: {MAX_GRANTS} open session grants. Grants do not "
                                                   "expire; revoke ones you no longer use (rt grants, then rt revoke ID)")
            protocol = self.agent_protocol(machine["id"])
            if protocol is not None and protocol < MIN_AGENT_PROTOCOL:
                raise web.HTTPConflict(text=f"Machine {machine['id']} runs an older agent (protocol {protocol}) than this "
                                            f"broker supports ({MIN_AGENT_PROTOCOL} or later); update its agent")
            if not create:
                # Zellij would quietly start a new, empty session for any other name.
                existing = await self.sessions(machine)
                if session not in existing:
                    raise web.HTTPNotFound(text=f"No session {session} on machine {machine['name'] or machine['id']}. "
                                                f"To start a new session named {session}, ask for it with --create "
                                                f"(rt attach, rt grant). Existing sessions: {', '.join(existing[:10]) or 'none'}.")
            gid, secret, now = "g_" + secrets.token_hex(12), secrets.token_urlsafe(32), time.time()
            token = await self.agent(machine, "/_agent/token", {"grant_id": gid})
            if not self.store.one("SELECT id FROM machines WHERE id=? AND enabled=1", (machine["id"],)):
                await self.agent(machine, "/_agent/revoke", {"grant_id": gid, "name": token["name"]})
                raise web.HTTPForbidden(text="Machine revoked during grant issuance")
            # The expires and idle columns record the operator limits in force at issuance (0: none); see Store.invalid.
            ends = self.store.ends({"issued": now})
            self.store.execute("INSERT INTO grants(id,hash,machine,session,issued,expires,idle,last_input,backend_name,backend_token) "
                               "VALUES(?,?,?,?,?,?,?,?,?,?)", (gid, digest(secret), machine["id"], session,
                                now, ends or 0, self.store.limits["idle"], now, token["name"],
                                self.store.seal(token["token"], "grants.backend_token", gid)))
            self.store.audit("grant-issued", machine=machine["id"], session=session, grant_id=gid)
            return web.json_response({"id": gid, "token": secret, "machine": machine["id"], **self.expiry({"issued": now}),
                                      "url": self.terminal_origin + "/g/" + gid + "/" + session})
        if action == "revoke":
            grant = self.store.grant(data.get("id", "")) if isinstance(data.get("id"), str) else None
            if not grant or grant["owner"] != owner:
                raise web.HTTPNotFound(text="No such session grant")
            await self.invalidate(grant, "revoked")
            return web.json_response({"ok": True})
        if action == "machine-revoke":
            machine = self.machine(owner, data.get("machine"))
            await self.revoke_machine(machine["id"])
            return web.json_response({"ok": True, "machine": machine["id"]})
        raise web.HTTPNotFound()

    async def revoke_machine(self, mid, actor="owner"):
        self.store.execute("UPDATE machines SET enabled=0 WHERE id=?", (mid,))
        for row in self.store.all("SELECT id FROM grants WHERE machine=? AND revoked IS NULL", (mid,)):
            await self.invalidate(self.store.grant(row["id"]), "machine-revoked", actor)
        self.store.audit("machine-revoked", actor=actor, machine=mid)

    def machine_config(self, mid, secret, name=None):
        """machine.json: the credential is valid only on this environment's relay; the agent dials exactly this target."""
        return {"id": mid, "name": name, "credential": secret, "relay_host": self.relay_host, "relay_port": self.port,
                "environment": self.environment, "origin": self.origin, "domain": mid + config.INTERNAL_SUFFIX,
                "proxy": mid + ".zellij"}

    async def enroll(self, request):
        data = await self.json_body(request)
        code = data.get("code", "")
        if not isinstance(code, str):
            raise web.HTTPBadRequest()
        record = self.store.one("SELECT * FROM enrollments WHERE hash=?", (digest(code),))
        if not record or record["used"] or record["expires"] <= time.time():
            self.store.audit("enrollment-denied", actor="machine", outcome="denied")
            raise web.HTTPUnauthorized(text="Enrollment code invalid or consumed")
        if self.name_taken(record["owner"], record["name"]):
            raise web.HTTPConflict(text=f"An enrolled machine is already named {record['name']}")
        mid, secret = "m_" + secrets.token_hex(12), secrets.token_urlsafe(32)
        with self.store.db:
            # Spent once, even by two racing requests.
            if self.store.db.execute("UPDATE enrollments SET used=1 WHERE hash=? AND used=0", (digest(code),)).rowcount != 1:
                raise web.HTTPUnauthorized(text="Enrollment code invalid or consumed")
            self.store.db.execute("INSERT INTO machines(id,secret,owner,name,enrolled) VALUES(?,?,?,?,?)",
                                  (mid, self.store.seal(secret, "machines.secret", mid), record["owner"], record["name"],
                                   time.time()))
        self.store.audit("machine-enrolled", actor="machine", machine=mid)
        return web.json_response(self.machine_config(mid, secret, record["name"]))

    async def policy(self, request):
        """Private listener only. Credentials establish identity; metadata names do not."""
        data = await self.json_body(request)
        content, op = data.get("content", {}), data.get("op")
        if not isinstance(content, dict):
            raise web.HTTPBadRequest()
        if op == "Login":
            meta = content.get("metas", {})
            meta = meta if isinstance(meta, dict) else {}
            mid = meta.get("machine", "")
            machine = self.store.one("SELECT * FROM machines WHERE id=? AND enabled=1", (mid,)) if isinstance(mid, str) else None
            credential = meta.get("credential", "")
            allowed = bool(machine and isinstance(credential, str)
                           and hmac.compare_digest(credential.encode(), machine["secret"].encode()))
            if allowed:
                content["run_id"] = secrets.token_hex(8)
                self.store.execute("DELETE FROM relay_logins WHERE machine=?", (machine["id"],))
                # The agent protocol it speaks; NULL when it announces none.
                protocol = meta.get("protocol")
                protocol = int(protocol) if isinstance(protocol, str) and re.fullmatch(r"[1-9][0-9]{0,3}", protocol) else None
                self.store.execute("INSERT INTO relay_logins(run_id,machine,protocol) VALUES(?,?,?)",
                                   (content["run_id"], machine["id"], protocol))
                self.store.audit("relay-login", actor="machine", machine=machine["id"])
                return web.json_response({"unchange": False, "content": content})
        else:
            user = content.get("user", {})
            user = user if isinstance(user, dict) else {}
            machine = self.store.one("SELECT m.* FROM machines m JOIN relay_logins l ON l.machine=m.id "
                                     "WHERE l.run_id=? AND m.enabled=1", (str(user.get("run_id", "")),))
            allowed = bool(machine)
            if allowed and op == "NewProxy":
                allowed = (content.get("proxy_type") == "http" and
                           content.get("proxy_name") == machine["id"] + ".zellij" and
                           content.get("custom_domains") == [machine["id"] + config.INTERNAL_SUFFIX] and
                           not any(content.get(k) for k in ("subdomain", "locations", "group", "group_key", "http_user", "http_pwd",
                                                          "host_header_rewrite", "headers", "remote_port")))
            elif op not in ("Ping", "NewWorkConn"):
                allowed = False
        if allowed:
            return web.json_response({"reject": False, "unchange": True})
        self.store.audit("relay-denied", actor="machine", outcome="denied")
        return web.json_response({"reject": True, "reject_reason": "Machine or route not authorized"})

    async def login(self, request):
        match = GRANT_PATH.fullmatch(request.path)
        if not match or match[2] != "/command/login":
            raise web.HTTPNotFound()
        data = await self.json_body(request)
        token = data.get("auth_token", "")
        if not isinstance(token, str):
            raise web.HTTPBadRequest()
        row = self.store.one("SELECT id FROM grants WHERE hash=?", (digest(token),))
        grant = self.store.grant(row["id"]) if row else None
        if self.store.invalid(grant) or grant["id"] != match[1]:
            self.store.audit("login-denied", outcome="denied")
            return web.json_response({"success": False, "message": "Grant invalid or ended"}, status=401)
        return await self.sign_in(grant, "browser" if request.headers.get("Origin") else "native", "login")

    async def sign_in(self, grant, kind, event):
        """A new cookie for this grant. Sign-ins never run out: beyond MAX_COOKIES the least recently used is evicted."""
        backend = await self.backend_login(grant)
        # Recheck after network awaits; revocation must win a racing login.
        if self.store.invalid(self.store.grant(grant["id"])):
            raise web.HTTPUnauthorized(text="Grant ended")
        # Keep sign-ins with an open socket, then the most recently used; evict the rest beyond the cap.
        stored = self.store.all("SELECT hash,used FROM cookies WHERE grant_id=?", (grant["id"],))
        stored.sort(key=lambda c: (any(t and not t.is_closing() for t in self.cookie_sockets.get(c["hash"], {}).values()), c["used"]),
                    reverse=True)
        for evicted in stored[MAX_COOKIES - 1:]:
            self.store.execute("DELETE FROM cookies WHERE hash=?", (evicted["hash"],))
            for transport in self.cookie_sockets.pop(evicted["hash"], {}).values():
                if transport:
                    transport.close()
            self.store.audit("login-evicted", machine=grant["machine"], session=grant["session"], grant_id=grant["id"])
        cookie = secrets.token_urlsafe(32)
        self.store.execute("INSERT INTO cookies(hash,grant_id,kind,backend_cookie,renewed,used) VALUES(?,?,?,?,?,?)",
                           (digest(cookie), grant["id"], kind, self.store.seal(backend, "cookies.backend_cookie", digest(cookie)),
                            time.time(), time.time()))
        self.store.audit(event, machine=grant["machine"], session=grant["session"], grant_id=grant["id"])
        response = web.json_response({"success": True, "message": "Login successful"})
        self.set_cookie(response, grant, cookie)
        return response

    def set_cookie(self, response, grant, value):
        """The grant's browser cookie, kept as long as the grant: until an operator lifetime limit, else COOKIE_AGE."""
        ends = self.store.ends(grant)
        response.set_cookie("session_token", value, secure=True, httponly=True, samesite="Strict", path="/g/" + grant["id"] + "/",
                            max_age=max(1, int(ends - time.time())) if ends is not None else COOKIE_AGE)

    def grant_cookie(self, request, gid):
        """The presented session_token bound to this grant (its value as "presented"); a browser may also send other paths' cookies."""
        for part in request.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "session_token" and value:
                cookie = self.store.one("SELECT * FROM cookies WHERE hash=? AND grant_id=?", (digest(value), gid))
                if cookie:
                    return dict(cookie, presented=value)
        return None

    async def authorize(self, request, stale=None):
        match = GRANT_PATH.fullmatch(request.path)
        cookie = self.grant_cookie(request, match[1]) if match else None
        grant = self.store.grant(cookie["grant_id"]) if cookie else None
        if self.store.invalid(grant):
            raise web.HTTPUnauthorized(text="Grant invalid or ended")
        if cookie["kind"] == "browser" and match[2].startswith("/ws/") and request.headers.get("Origin") != request["origin"]:
            raise web.HTTPForbidden(text="Exact browser Origin required")
        if len(self.sockets.get(grant["id"], {})) >= 8 and match[2].startswith("/ws/"):
            raise web.HTTPTooManyRequests(text="Connection limit reached")
        # Refresh backend's five-minute login without extending the broker grant,
        # or once Zellij has rejected it (agent restarts revoke every token).
        async with self.locks.setdefault(cookie["hash"], asyncio.Lock()):
            cookie = self.store.one("SELECT * FROM cookies WHERE hash=?", (cookie["hash"],))
            if not cookie:
                raise web.HTTPUnauthorized(text="Sign-in replaced by a newer one")
            self.store.execute("UPDATE cookies SET used=? WHERE hash=?", (time.time(), cookie["hash"]))
            if time.time() - cookie["renewed"] >= 240 or cookie["backend_cookie"] == stale:
                backend = await self.backend_login(grant)
                self.store.execute("UPDATE cookies SET backend_cookie=?,renewed=? WHERE hash=?",
                                   (self.store.seal(backend, "cookies.backend_cookie", cookie["hash"]), time.time(), cookie["hash"]))
                cookie = self.store.one("SELECT * FROM cookies WHERE hash=?", (cookie["hash"],))
        if self.store.invalid(self.store.grant(grant["id"])):
            raise web.HTTPUnauthorized(text="Grant ended")
        return grant, cookie

    async def proxy(self, request):
        if request.method == "GET" and request.path == "/_rt.css":
            return web.FileResponse(SHELL_CSS, headers={"Content-Type": "text/css"})
        match = GRANT_PATH.fullmatch(request.path)
        if not match:
            if request.method == "GET" and (request.path == "/" or NAME.fullmatch(request.path[1:])):
                return self.signin()
            raise web.HTTPUnauthorized(text="Open your grant URL to sign in")
        gid, path = match.groups()
        if request["terminal_owner"] is None:
            raise web.HTTPNotFound(text="Open the issued grant URL on your terminal hostname")
        if path == "/_rt_mobile.js" and request.method == "GET":
            return web.FileResponse(MOBILE_JS, headers={"Content-Type": "application/javascript"})
        if path == "/_rt_fullscreen.js" and request.method == "GET":
            return web.FileResponse(FULLSCREEN_JS, headers={"Content-Type": "application/javascript"})
        grant = self.store.grant(gid)
        if not grant or grant["owner"] != request["terminal_owner"]:
            if request.method == "GET" and (path == "/" or NAME.fullmatch(path[1:])):
                return self.signin()
            raise web.HTTPUnauthorized(text="Grant invalid or ended")
        request["terminal_path"] = path
        request["terminal_raw_path"] = request.raw_path[len("/g/" + gid):]
        if path == "/command/login" and request.method == "POST":
            return await self.login(request)
        if path == "/_signin.js" and request.method == "GET":
            return web.Response(text=SIGNIN_JS, content_type="application/javascript")
        page = request.method == "GET" and (path == "/" or path == "/" + grant["session"])
        presented = self.grant_cookie(request, gid) if page else None
        if page and (self.store.invalid(grant) or not presented):
            return self.signin(gid if not self.store.invalid(grant) else None)
        public = request.method == "GET" and (page or path == "/info/version" or path.startswith("/assets/"))
        if not public and path not in ("/session", "/session-list", "/ws/control") and not path.startswith("/ws/terminal/"):
            raise web.HTTPNotFound()
        grant, cookie = await self.authorize(request)
        if path.startswith("/ws/") and path != "/ws/control" and path != "/ws/terminal/" + grant["session"]:
            raise web.HTTPForbidden(text="Session differs from grant")
        if path == "/session":
            if request.method != "POST" or request.query.get("session") != grant["session"]:
                raise web.HTTPForbidden(text="Named session required")
        try:
            response = await self.attach(request, grant, cookie)
        except web.HTTPUnauthorized:
            grant, cookie = await self.authorize(request, stale=cookie["backend_cookie"])
            response = await self.attach(request, grant, cookie)
        if page and response.status == 200:
            # Zellij's client reloads this page to reconnect; renewing the cookie keeps an open-ended grant signed in.
            self.set_cookie(response, grant, presented["presented"])
            if response.content_type == "text/html" and b"<head>" in response.body:
                response.body = response.body.replace(b"<head>", b"<head>" + RELOAD_GUARD + MOBILE_SCRIPT + FULLSCREEN_SCRIPT, 1)
        return response

    def signin(self, gid=None):
        """The broker's own terminal pages: how to open a session, and the sign-in form of one grant."""
        heading, content = "Open a terminal session", (
            '<p>Issue a session grant with <code>rt grant SESSION</code> (or attach from a terminal with '
            '<code>rt attach SESSION</code>), then open the terminal URL it prints.</p>')
        if gid:
            heading, content = "Sign in to the terminal", (
                '<p class="muted">Paste the session credential that <code>rt grant</code> printed with this URL. '
                'This browser then stays signed in to this session until its grant is revoked.</p>'
                '<form><label>Session credential <input type="password" required autocomplete="off"></label>'
                '<button type="submit" class="primary">Sign in</button></form><p role="status" aria-live="polite"></p>'
                '<script src="./_signin.js" defer></script>')
        return web.Response(text='<!doctype html><html lang="en"><meta charset="utf-8">'
                            '<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light dark">'
                            '<title>Rootkeel sign-in</title><link rel="stylesheet" href="/_rt.css">'
                            '<header class="shell"><span class="brand"><span class="mark" aria-hidden="true">&gt;_</span>Rootkeel</span>'
                            '</header><main><section class="panel page"><h1>' + heading + '</h1>' + content
                            + '</section></main></html>',
                            content_type="text/html", headers={"Content-Security-Policy":
                            "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; "
                            "form-action 'self'; frame-ancestors 'none'"})

    async def attach(self, request, grant, cookie):
        """Raises HTTPUnauthorized when Zellij rejects the backend cookie."""
        path = request["terminal_path"]
        machine = {"id": grant["machine"], "secret": grant["secret"]}
        headers = self.headers(machine, cookie["backend_cookie"])
        if path.startswith("/ws/"):
            registered = []

            def register(socket, transport):
                registered.append(socket)
                self.sockets.setdefault(grant["id"], {})[socket] = transport
                self.cookie_sockets.setdefault(cookie["hash"], {})[socket] = transport
                # An expiry/revoke or eviction during the upstream handshake closes immediately.
                if self.store.invalid(self.store.grant(grant["id"])) or \
                        not self.store.one("SELECT hash FROM cookies WHERE hash=?", (cookie["hash"],)):
                    transport.close()
                self.store.audit("attach", machine=grant["machine"], session=grant["session"], grant_id=grant["id"])

            def touch():
                # Only inbound terminal bytes: not output, ping/pong or control traffic.
                if not self.store.invalid(self.store.grant(grant["id"])):
                    self.store.execute("UPDATE grants SET last_input=? WHERE id=? AND revoked IS NULL", (time.time(), grant["id"]))
                    self.store.execute("UPDATE cookies SET used=? WHERE hash=?", (time.time(), cookie["hash"]))
            try:
                terminal = path != "/ws/control"
                return await bridge(request, self.client, self.relay + request["terminal_raw_path"], headers,
                                    on_input=touch if terminal else None, register=register, split_input=terminal)
            finally:
                sockets = self.sockets.get(grant["id"], {})
                for sock, transport in list(sockets.items()):
                    if transport is None or transport.is_closing():
                        sockets.pop(sock, None)
                if not sockets:
                    self.sockets.pop(grant["id"], None)
                for sock in registered:
                    self.cookie_sockets.get(cookie["hash"], {}).pop(sock, None)
                if not self.cookie_sockets.get(cookie["hash"], True):
                    self.cookie_sockets.pop(cookie["hash"], None)
                if registered:
                    self.store.execute("UPDATE cookies SET used=? WHERE hash=?", (time.time(), cookie["hash"]))
                    self.store.audit("detach", machine=grant["machine"], session=grant["session"], grant_id=grant["id"])
        response = await forward(request, self.client, self.relay + request["terminal_raw_path"], headers)
        if response.status == 401:
            raise web.HTTPUnauthorized(text="Backend login required")
        if path == "/session-list" and response.status == 200:
            data = json.loads(response.body)
            data["sessions"] = [s for s in data.get("sessions", []) if s.get("name") == grant["session"]]
            response = web.json_response(data)
        return response

    async def invalidate(self, grant, reason, actor="owner"):
        if not grant["revoked"]:
            self.store.execute("UPDATE grants SET revoked=? WHERE id=?", (reason, grant["id"]))
            self.store.audit(reason, actor=actor, machine=grant["machine"], session=grant["session"], grant_id=grant["id"])
        for transport in self.sockets.get(grant["id"], {}).values():
            if transport:
                transport.close()

    async def sweep(self):
        """Close the sockets of every grant that ended, within half a second.

        Without operator time limits only a revoke or a machine revoke ends a
        grant. Open sockets are checked too, so a revoke written by another
        process (python3 -m rootkeel.admin) closes them as promptly as one made
        through the API.
        """
        while True:
            for row in self.store.all("SELECT id FROM grants WHERE revoked IS NULL"):
                grant = self.store.grant(row["id"])
                reason = self.store.invalid(grant)
                if reason:
                    await self.invalidate(grant, reason)
            for gid in list(self.sockets):
                grant = self.store.grant(gid)
                if self.store.invalid(grant):
                    if grant:
                        await self.invalidate(grant, self.store.invalid(grant))
                    else:
                        for transport in self.sockets.get(gid, {}).values():
                            if transport:
                                transport.close()
            await asyncio.sleep(0.5)

    async def cleanup_tokens(self):
        while True:
            # Cleanup cannot delay the independent policy sweep.
            for row in self.store.all("SELECT g.id FROM grants g JOIN machines m ON m.id=g.machine "
                                      "WHERE g.revoked IS NOT NULL AND g.cleaned=0 AND m.enabled=1 LIMIT 4"):
                grant = self.store.grant(row["id"])
                try:
                    await self.agent({"id": grant["machine"], "secret": grant["secret"]}, "/_agent/revoke",
                                     {"grant_id": grant["id"], "name": grant["backend_name"]})
                    self.store.execute("UPDATE grants SET cleaned=1 WHERE id=?", (grant["id"],))
                except web.HTTPException:
                    pass
            self.store.execute("DELETE FROM audit WHERE at<?", (time.time() - 30 * 86400,))
            self.store.execute("DELETE FROM enrollments WHERE expires<?", (time.time() - 86400,))
            await asyncio.sleep(2)

    async def start(self, app):
        self.client = ClientSession(cookie_jar=DummyCookieJar(), timeout=ClientTimeout(total=5),
                                    auto_decompress=False)
        self.tasks = [asyncio.create_task(self.sweep()), asyncio.create_task(self.cleanup_tokens())]

    async def stop(self, app):
        for sockets in self.sockets.values():
            for transport in sockets.values():
                if transport:
                    transport.close()
        for task in self.tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self.client.close()
        self.store.db.close()

    async def prepare(self, request, response):
        # Also cover upgrade handshakes and framework-generated error responses.
        response.headers.update(self.security_headers)

    async def client_address(self, request):
        """The client's address: the front door's X-Forwarded-For (it sets exactly $remote_addr) when the request
        comes from the front door, else the peer. An IPv6 client counts as its /64."""
        peer = request.remote or ""
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded and self.front_door:
            now = time.monotonic()
            if self.front_door_checked is None or now - self.front_door_checked >= FRONT_DOOR_SECONDS:
                try:
                    infos = await asyncio.get_running_loop().getaddrinfo(self.front_door, None)
                    self.front_door_addresses = {info[4][0] for info in infos}
                except OSError:
                    self.front_door_addresses = set()
                self.front_door_checked = now
            if peer in self.front_door_addresses:
                peer = forwarded.rsplit(",", 1)[-1].strip()
        try:
            address = ipaddress.ip_address(peer)
        except ValueError:
            return peer
        if address.version == 6 and address.ipv4_mapped:
            address = address.ipv4_mapped
        return str(ipaddress.ip_network(f"{address}/64", strict=False)) if address.version == 6 else str(address)

    def rate_limited(self, client):
        """Record an authentication attempt by this client; True when it has used its attempts for this minute."""
        now = time.monotonic()
        attempts = self.attempts.pop(client, None) or deque(maxlen=AUTH_ATTEMPTS)
        self.attempts[client] = attempts
        if len(self.attempts) > AUTH_CLIENTS:
            del self.attempts[next(iter(self.attempts))]
        if len(attempts) == AUTH_ATTEMPTS and now - attempts[0] < 60:
            return True
        attempts.append(now)
        return False

    @web.middleware
    async def security(self, request, handler):
        owner = request["terminal_owner"] = self.host_owner(request.host)
        request["origin"] = self.origin if owner is None else self.terminal_origin
        if owner is not None and request.path.startswith("/api/"):
            raise web.HTTPNotFound()
        if request.path.endswith("/command/login") or request.path in ("/api/enroll", "/api/enrollment"):
            if self.rate_limited(await self.client_address(request)):
                raise web.HTTPTooManyRequests(text="Authentication rate limit reached")
        origin = request.headers.get("Origin")
        if origin is not None and origin != request["origin"]:
            raise web.HTTPForbidden(text="Origin not allowed")
        try:
            response = await handler(request)
        except web.HTTPException as error:
            response = error
        if not response.prepared:
            response.headers.update(self.security_headers)
        return response


def application(broker):
    """The public listener's routes."""
    public = web.Application(middlewares=[broker.security], client_max_size=MAX_BODY)
    public.router.add_post("/api/enroll", broker.enroll)
    public.router.add_route("*", "/api/{action}", broker.admin)
    public.on_response_prepare.append(broker.prepare)
    public.on_startup.append(broker.start)
    public.on_cleanup.append(broker.stop)
    public.router.add_route("*", "/{path:.*}", broker.proxy)
    return public


async def main():
    try:
        broker = Broker()
    except config.ConfigError as error:
        raise SystemExit("Broker configuration invalid:\n  - " + "\n  - ".join(error.problems)) from None
    # Private policy has no public route. Network membership grants relay access.
    private = web.Application(client_max_size=16384)
    private.router.add_post("/_frp", broker.policy)
    runners = [web.AppRunner(application(broker), access_log=None), web.AppRunner(private, access_log=None)]
    for runner, port in zip(runners, (8080, 8081)):
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port).start()
    try:
        await asyncio.Event().wait()
    finally:
        for runner in reversed(runners):
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
