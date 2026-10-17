from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from serve.api_keys import APIKeyStore  # noqa: E402
from serve.frontend import ChatTemplate  # noqa: E402
from serve.server import CTX_SLACK, ByteTokenizer, MockEngine, Service, serve  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class APIKeyStoreTests(unittest.TestCase):
    def test_keys_persist_for_reveal_and_revocation_removes_access(self):
        with tempfile.TemporaryDirectory(prefix="strata-api-keys-") as tmp:
            root = Path(tmp)
            now = 1_800_000_000
            keys = APIKeyStore("primary-secret", root / "keys.json", root / "usage.jsonl", clock=lambda: now)
            created = keys.create("Desktop app")
            self.assertTrue(created["token"].startswith("strata_"))
            self.assertEqual(keys.authenticate(created["token"]), created["id"])

            reloaded = APIKeyStore("primary-secret", root / "keys.json", root / "usage.jsonl", clock=lambda: now)
            listed = next(row for row in reloaded.list_keys() if row["id"] == created["id"])
            self.assertEqual(listed["token"], created["token"])
            self.assertEqual(reloaded.authenticate("primary-secret"), "primary")
            self.assertEqual((root / "keys.json").stat().st_mode & 0o777, 0o600)

            reloaded.record_usage("primary", 120, 22, at=now - 60)
            reloaded.record_usage(created["id"], 35, 8, at=now - 60)
            usage = reloaded.usage("1h")
            by_id = {row["id"]: row for row in usage["keys"]}
            self.assertEqual(by_id["primary"]["cached_tokens"], 120)
            self.assertEqual(by_id["primary"]["output_tokens"], 22)
            self.assertEqual(by_id[created["id"]]["cached_tokens"], 35)
            self.assertEqual(by_id[created["id"]]["output_tokens"], 8)
            self.assertEqual(by_id[created["id"]]["requests"], 1)
            self.assertTrue(all("token" not in row for row in usage["keys"]))
            after_restart = APIKeyStore("primary-secret", root / "keys.json", root / "usage.jsonl", clock=lambda: now)
            persisted = {row["id"]: row for row in after_restart.usage("1h")["keys"]}
            self.assertEqual(persisted[created["id"]]["output_tokens"], 8)

            reloaded.revoke(created["id"])
            self.assertIsNone(reloaded.authenticate(created["token"]))
            revoked = next(row for row in reloaded.list_keys() if row["id"] == created["id"])
            self.assertFalse(revoked["active"])
            self.assertEqual(revoked["token"], "")
            self.assertEqual(reloaded.usage("1h")["keys"][-1]["cached_tokens"], 35)

    def test_primary_key_lists_keep_authentication_compatible(self):
        with tempfile.TemporaryDirectory(prefix="strata-api-primary-keys-") as tmp:
            keys = APIKeyStore(["primary-one", "primary-two"], Path(tmp) / "keys.json",
                               Path(tmp) / "usage.jsonl")
            self.assertEqual(keys.authenticate("primary-one"), "primary")
            self.assertEqual(keys.authenticate("primary-two"), "primary")
            self.assertEqual(keys.list_keys()[0]["token"], "primary-one")

    def test_names_and_ranges_are_validated(self):
        with tempfile.TemporaryDirectory(prefix="strata-api-keys-") as tmp:
            keys = APIKeyStore("primary-secret", Path(tmp) / "keys.json", Path(tmp) / "usage.jsonl")
            for name in ("", "  ", "x" * 65, "bad\nname"):
                with self.assertRaises(ValueError):
                    keys.create(name)
            with self.assertRaises(ValueError):
                keys.usage("all")


class APIKeyRouteTests(unittest.TestCase):
    def test_admin_routes_require_primary_and_usage_does_not_return_secrets(self):
        tok = ByteTokenizer()
        svc = Service(MockEngine(tok, "ok", max_context=4096), tok,
                      ChatTemplate(ROOT / "serve" / "chat_template.jinja"), model_name="test")
        svc.api_key = "primary-secret"
        with tempfile.TemporaryDirectory(prefix="strata-api-routes-") as tmp:
            now = time.time()
            svc.api_key_store = APIKeyStore("primary-secret", Path(tmp) / "keys.json",
                                            Path(tmp) / "usage.jsonl", clock=lambda: now)
            httpd = serve(svc, port=0)
            base = f"http://127.0.0.1:{httpd.server_address[1]}"
            try:
                def request(path, key, data=None):
                    headers = {"Authorization": f"Bearer {key}"}
                    if data is not None:
                        headers["Content-Type"] = "application/json"
                    req = urllib.request.Request(base + path, data=data, headers=headers)
                    with urllib.request.urlopen(req, timeout=5) as response:
                        return response.status, json.loads(response.read())

                status, created = request("/api-keys", "primary-secret", b'{"name":"integration test"}')
                self.assertEqual(status, 201)
                api_key = created["token"]
                self.assertEqual(svc.api_key_store.authenticate(api_key), created["id"])
                status, listed = request("/api-keys", "primary-secret")
                self.assertEqual(status, 200)
                list_req = urllib.request.Request(base + "/api-keys",
                                                 headers={"Authorization": "Bearer primary-secret"})
                with urllib.request.urlopen(list_req, timeout=5) as response:
                    self.assertEqual(response.headers.get("Cache-Control"), "no-store")
                listed_key = next(item for item in listed["keys"] if item["id"] == created["id"])
                self.assertEqual(listed_key["token"], api_key)  # available for Show and Copy after reload
                status, result = request("/v1/chat/completions", api_key,
                                         json.dumps({"model": "test", "messages": [{"role": "user", "content": "hi"}],
                                                     "max_tokens": 16}).encode())
                self.assertEqual(status, 200)
                self.assertGreater(result["usage"]["completion_tokens"], 0)

                with self.assertRaises(urllib.error.HTTPError) as error:
                    request("/api-keys", api_key)
                self.assertEqual(error.exception.code, 403)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    request("/settings", api_key)
                self.assertEqual(error.exception.code, 403)

                status, usage = request("/api-key-usage?range=1h", "primary-secret")
                self.assertEqual(status, 200)
                row = next(item for item in usage["keys"] if item["id"] == created["id"])
                self.assertEqual(row["cached_tokens"], 0)
                self.assertGreater(row["output_tokens"], 0)
                self.assertEqual(row["requests"], 1)
                self.assertNotIn("token", row)

                status, revoked = request("/api-keys/revoke", "primary-secret",
                                          json.dumps({"id": created["id"]}).encode())
                self.assertEqual(status, 200)
                self.assertFalse(revoked["active"])
                self.assertIsNone(svc.api_key_store.authenticate(api_key))
            finally:
                httpd.shutdown()
                httpd.server_close()


if __name__ == "__main__":
    unittest.main()
