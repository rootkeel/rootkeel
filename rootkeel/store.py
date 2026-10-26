# SPDX-License-Identifier: AGPL-3.0-only
"""The broker's SQLite store; machine secrets, Zellij tokens and cookies are sealed with a key kept outside it."""
import base64
import hashlib
import os
import sqlite3
import time
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def digest(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


class Store:
    # Each value has its own data key, wrapped by a key kept outside SQLite.
    PREFIX = "rtenc1:"
    FIELDS = (("machines", "secret", "id"), ("grants", "backend_token", "id"),
              ("cookies", "backend_cookie", "hash"))

    def __init__(self, path, key_file=None, limits=None):
        # Operator time limits (config.grant_limits); none by default. They apply to
        # every grant as currently configured, including grants issued before a change.
        self.limits = {"idle": 0, "lifetime": 0, **(limits or {})}
        key_file = key_file or os.environ.get("RT_DB_KEY_FILE")
        if not key_file:
            raise ValueError("RT_DB_KEY_FILE is required; mount the encryption key outside the database volume")
        key_path = Path(key_file).resolve()
        if key_path.is_relative_to(Path(path).resolve().parent):
            raise ValueError("RT_DB_KEY_FILE must be outside the database directory")
        try:
            key = bytes.fromhex(key_path.read_text().strip())
        except ValueError:
            raise ValueError("RT_DB_KEY_FILE must contain a 32-byte hexadecimal key") from None
        if len(key) != 32:
            raise ValueError("RT_DB_KEY_FILE must contain a 32-byte hexadecimal key")
        self.cipher = AESGCM(key)
        os.umask(0o077)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS enrollments (
          hash TEXT PRIMARY KEY, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0,
          owner TEXT NOT NULL, name TEXT);
        CREATE TABLE IF NOT EXISTS machines (
          id TEXT PRIMARY KEY, secret TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
          owner TEXT NOT NULL, name TEXT, enrolled REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS relay_logins (
          run_id TEXT PRIMARY KEY, machine TEXT NOT NULL REFERENCES machines(id), protocol INTEGER);
        CREATE TABLE IF NOT EXISTS grants (
          id TEXT PRIMARY KEY, hash TEXT UNIQUE NOT NULL,
          machine TEXT NOT NULL REFERENCES machines(id), session TEXT NOT NULL,
          issued REAL NOT NULL, expires REAL NOT NULL, idle INTEGER NOT NULL,
          last_input REAL NOT NULL, revoked TEXT, backend_name TEXT NOT NULL,
          backend_token TEXT NOT NULL, cleaned INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS cookies (
          hash TEXT PRIMARY KEY, grant_id TEXT NOT NULL REFERENCES grants(id),
          kind TEXT NOT NULL, backend_cookie TEXT NOT NULL, renewed REAL NOT NULL,
          used REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS audit (
          seq INTEGER PRIMARY KEY, at REAL NOT NULL, actor TEXT NOT NULL,
          event TEXT NOT NULL, machine TEXT, session TEXT, grant_id TEXT, outcome TEXT NOT NULL,
          owner TEXT NOT NULL DEFAULT '');
        """)
        self.db.commit()
        try:
            self.migrate()
        except Exception:
            self.db.close()
            raise

    @staticmethod
    def context(field, identity):
        return ("rootkeel:v1:" + field + ":" + identity).encode()

    def seal(self, value, field, identity):
        aad = self.context(field, identity)
        data_key, wrap_nonce, nonce = os.urandom(32), os.urandom(12), os.urandom(12)
        wrapped = self.cipher.encrypt(wrap_nonce, data_key, aad + b":key")
        ciphertext = AESGCM(data_key).encrypt(nonce, value.encode(), aad)
        return self.PREFIX + base64.b64encode(wrap_nonce + wrapped + nonce + ciphertext).decode()

    def unseal(self, value, field, identity):
        try:
            if not value.startswith(self.PREFIX):
                raise ValueError()
            data = base64.b64decode(value[len(self.PREFIX):], validate=True)
            aad = self.context(field, identity)
            key = self.cipher.decrypt(data[:12], data[12:60], aad + b":key")
            return AESGCM(key).decrypt(data[60:72], data[72:], aad).decode()
        except (InvalidTag, ValueError, UnicodeError):
            raise ValueError("Database credential authentication failed; check the encryption key and database") from None

    SCHEMA_VERSION = 1

    def migrate(self):
        """Stamp a new database with the schema version; check that an existing one opens with this key."""
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            if any(self.db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() for table, _, _ in self.FIELDS):
                raise ValueError("Unsupported broker database: it holds rows but no schema version")
            self.db.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")
            self.db.commit()
            return
        if version != self.SCHEMA_VERSION:
            raise ValueError("Unsupported broker database version")
        for table, column, pk in self.FIELDS:
            for identity, value in self.db.execute(f"SELECT {pk},{column} FROM {table}").fetchall():
                self.unseal(value, table + "." + column, identity)

    def decode_row(self, row):
        if row is None:
            return None
        result = dict(row)
        if "secret" in result:
            result["secret"] = self.unseal(result["secret"], "machines.secret", result.get("machine", result["id"]))
        for field, table, pk in (("backend_token", "grants", "id"), ("backend_cookie", "cookies", "hash")):
            if field in result:
                result[field] = self.unseal(result[field], table + "." + field, result[pk])
        return result

    def one(self, sql, args=()):
        return self.decode_row(self.db.execute(sql, args).fetchone())

    def all(self, sql, args=()):
        return [self.decode_row(row) for row in self.db.execute(sql, args).fetchall()]

    def execute(self, sql, args=()):
        result = self.db.execute(sql, args)
        self.db.commit()
        return result

    def audit(self, event, *, actor="owner", machine=None, session=None, grant_id=None, outcome="ok", owner=None):
        if owner is None and machine:
            row = self.one("SELECT owner FROM machines WHERE id=?", (machine,))
            owner = row["owner"] if row else None
        self.execute("INSERT INTO audit(at,actor,event,machine,session,grant_id,outcome,owner) VALUES(?,?,?,?,?,?,?,?)",
                     (time.time(), actor, event, machine, session, grant_id, outcome, owner or ""))

    def grant(self, grant_id):
        return self.one("SELECT g.*,m.secret,m.enabled,m.owner,m.name AS machine_name "
                        "FROM grants g JOIN machines m ON m.id=g.machine WHERE g.id=?", (grant_id,))

    def ends(self, grant):
        """When an operator lifetime limit ends this grant, or None: by default a grant has no end."""
        return grant["issued"] + self.limits["lifetime"] if self.limits["lifetime"] else None

    def invalid(self, grant):
        """Why a grant no longer admits anyone, or None.

        Only administrative acts end a grant by default: a revoke, or its machine
        revoked. Time ends one only when the operator opted into a limit. The
        grants table's expires and idle columns record the limits at issuance
        and are not consulted.
        """
        if not grant or not grant["enabled"]:
            return "machine-revoked"
        if grant["revoked"]:
            return grant["revoked"]
        ends = self.ends(grant)
        if ends is not None and time.time() >= ends:
            return "expired"
        if self.limits["idle"] and time.time() >= grant["last_input"] + self.limits["idle"]:
            return "idle"
        return None
