# SPDX-License-Identifier: AGPL-3.0-only
"""Credential-at-rest contract of the broker's SQLite store."""
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rootkeel.store import Store, digest


class StoreEncryptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir()
        self.path = self.root / "data" / "broker.sqlite"
        self.key = self.root / "key"
        self.key.write_text(os.urandom(32).hex())
        self.store = Store(str(self.path), self.key)
        self.secrets = {"machines.secret": "machine-secret-" + "m" * 40,
                        "grants.backend_token": "backend-secret-" + "t" * 40,
                        "cookies.backend_cookie": "cookie-secret-" + "c" * 40}

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def populate(self):
        def value(field, identity):
            return self.store.seal(self.secrets[field], field, identity)
        self.store.execute("INSERT INTO machines(id,secret,owner) VALUES('m_test',?,'owner')", (value("machines.secret", "m_test"),))
        self.store.execute("INSERT INTO grants(id,hash,machine,session,issued,expires,idle,last_input,backend_name,backend_token) "
                           "VALUES('g_test',?,'m_test','main',0,1,900,0,'token_1',?)",
                           (digest("public-grant"), value("grants.backend_token", "g_test")))
        self.store.execute("INSERT INTO cookies(hash,grant_id,kind,backend_cookie,renewed) "
                           "VALUES('cookie-hash','g_test','native',?,0)", (value("cookies.backend_cookie", "cookie-hash"),))

    def assert_credentials(self, store):
        grant = store.grant("g_test")
        self.assertEqual(grant["secret"], self.secrets["machines.secret"])
        self.assertEqual(grant["backend_token"], self.secrets["grants.backend_token"])
        self.assertEqual(store.one("SELECT * FROM cookies")["backend_cookie"], self.secrets["cookies.backend_cookie"])

    def assert_no_plaintext(self):
        for path in self.path.parent.glob("broker.sqlite*"):
            data = path.read_bytes()
            for secret in (*self.secrets.values(), "public-grant"):
                self.assertNotIn(secret.encode(), data, path.name)

    def test_database_copy_has_no_usable_credentials(self):
        self.populate()
        self.assert_credentials(self.store)
        self.store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.assert_no_plaintext()
        # A copied DB with a different key fails closed; no fallback regenerates it.
        copy_dir = self.root / "copy"
        copy_dir.mkdir()
        copied = copy_dir / "broker.sqlite"
        shutil.copyfile(self.path, copied)
        wrong_key = self.root / "wrong-key"
        wrong_key.write_text(os.urandom(32).hex())
        with self.assertRaisesRegex(ValueError, "authentication failed"):
            Store(str(copied), wrong_key)
        raw = sqlite3.connect(copied)
        try:
            self.assertEqual(raw.execute("SELECT count(*) FROM machines").fetchone()[0], 1)
        finally:
            raw.close()
        second = Store(str(copied), self.key)
        self.assert_credentials(second)
        second.db.close()
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, "required"):
            Store(str(copied))

    def test_a_new_database_is_stamped_and_an_unknown_one_refused(self):
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], Store.SCHEMA_VERSION)
        self.populate()
        self.store.db.close()
        raw = sqlite3.connect(self.path)
        try:
            # Rows without a schema version: not a database this broker wrote.
            raw.execute("PRAGMA user_version=0")
            raw.commit()
        finally:
            raw.close()
        with self.assertRaisesRegex(ValueError, "no schema version"):
            Store(str(self.path), self.key)
        raw = sqlite3.connect(self.path)
        try:
            raw.execute("PRAGMA user_version=99")
            raw.commit()
        finally:
            raw.close()
        with self.assertRaisesRegex(ValueError, "Unsupported broker database version"):
            Store(str(self.path), self.key)
        raw = sqlite3.connect(self.path)
        try:
            raw.execute(f"PRAGMA user_version={Store.SCHEMA_VERSION}")
            raw.commit()
        finally:
            raw.close()
        self.store = Store(str(self.path), self.key)
        self.assert_credentials(self.store)

    def test_grants_name_their_machine(self):
        self.populate()
        self.assertIsNone(self.store.grant("g_test")["machine_name"])
        self.store.execute("UPDATE machines SET name='laptop' WHERE id='m_test'")
        self.assertEqual(self.store.grant("g_test")["machine_name"], "laptop")
        self.assertEqual(self.store.grant("g_test")["owner"], "owner")

    def test_tampering_and_swapping_values_is_rejected(self):
        sealed = self.store.seal("secret", "machines.secret", "m_a")
        for field, identity in (("machines.secret", "m_b"), ("grants.backend_token", "m_a")):
            with self.assertRaisesRegex(ValueError, "authentication failed"):
                self.store.unseal(sealed, field, identity)
        with self.assertRaisesRegex(ValueError, "authentication failed"):
            self.store.unseal(sealed[:-2] + "AA", "machines.secret", "m_a")
        with self.assertRaisesRegex(ValueError, "authentication failed"):
            self.store.unseal("plaintext", "machines.secret", "m_a")

    def test_key_must_be_separate_and_correct_size(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            Store(str(self.path), self.path.parent / "key")
        self.key.write_text("ab")
        with self.assertRaisesRegex(ValueError, "32-byte"):
            Store(str(self.path), self.key)
        self.key.write_text("not hex")
        with self.assertRaisesRegex(ValueError, "32-byte"):
            Store(str(self.path), self.key)
