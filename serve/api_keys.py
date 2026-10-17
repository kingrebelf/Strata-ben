from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
import uuid
from pathlib import Path


class APIKeyStore:
    """Local API key management and prompt-free token usage history."""

    RANGES = {
        "1h": (3600, 60),
        "24h": (86400, 48),
        "7d": (7 * 86400, 42),
        "30d": (30 * 86400, 30),
    }
    RETENTION_SECONDS = 90 * 86400
    MAX_EVENTS = 100_000

    def __init__(self, primary_key="", keys_path=None, usage_path=None, clock=None):
        if isinstance(primary_key, (list, tuple)):
            self.primary_keys = [str(key).strip() for key in primary_key if str(key).strip()]
        else:
            self.primary_keys = [key.strip() for key in str(primary_key or "").split(",") if key.strip()]
        self.primary_key = self.primary_keys[0] if self.primary_keys else ""
        state_home = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
        state_dir = state_home / "strata"
        self.keys_path = Path(keys_path) if keys_path else state_dir / "api-keys.json"
        self.usage_path = Path(usage_path) if usage_path else state_dir / "api-usage.jsonl"
        self.clock = clock or time.time
        self.lock = threading.RLock()
        self.keys = []
        self.events = []
        self._last_prune = 0.0
        self._load_keys()
        self._load_usage()

    @staticmethod
    def _private_parent(path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass

    @staticmethod
    def _private_file(path: Path):
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _load_keys(self):
        try:
            payload = json.loads(self.keys_path.read_text(encoding="utf-8"))
            rows = payload.get("keys", []) if isinstance(payload, dict) else []
        except (OSError, ValueError, TypeError):
            rows = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            key_id = row.get("id")
            label = row.get("label")
            active = row.get("active") is True
            token = row.get("token") if active else ""
            if (not isinstance(key_id, str) or not key_id or key_id == "primary" or key_id in seen or
                    not isinstance(label, str) or not label.strip() or not isinstance(token, str) or
                    (active and not token)):
                continue
            seen.add(key_id)
            self.keys.append({
                "id": key_id,
                "label": label.strip()[:64],
                "token": token,
                "active": active,
                "created_at": int(row.get("created_at") or 0),
                "revoked_at": int(row.get("revoked_at") or 0) or None,
            })
        if self.keys_path.exists():
            self._private_file(self.keys_path)

    def _load_usage(self):
        now = self.clock()
        rows = []
        try:
            with self.usage_path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                        key_id = event.get("key_id")
                        at = float(event.get("time"))
                        cached = int(event.get("cached_tokens", 0))
                        output = int(event.get("output_tokens", 0))
                        if (isinstance(key_id, str) and key_id and at >= now - self.RETENTION_SECONDS and
                                cached >= 0 and output >= 0):
                            rows.append({"time": at, "key_id": key_id,
                                         "cached_tokens": cached, "output_tokens": output})
                    except (ValueError, TypeError, AttributeError):
                        continue
        except OSError:
            pass
        self.events = rows[-self.MAX_EVENTS:]
        self._last_prune = now
        if len(rows) != len(self.events):
            self._rewrite_usage()

    def _save_keys(self):
        self._private_parent(self.keys_path)
        temp = self.keys_path.with_name(self.keys_path.name + f".{os.getpid()}.tmp")
        payload = {"version": 1, "keys": self.keys}
        try:
            with temp.open("w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            self._private_file(temp)
            os.replace(temp, self.keys_path)
            self._private_file(self.keys_path)
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    def _rewrite_usage(self):
        self._private_parent(self.usage_path)
        temp = self.usage_path.with_name(self.usage_path.name + f".{os.getpid()}.tmp")
        try:
            with temp.open("w", encoding="utf-8") as stream:
                for event in self.events:
                    stream.write(json.dumps(event, separators=(",", ":")) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            self._private_file(temp)
            os.replace(temp, self.usage_path)
            self._private_file(self.usage_path)
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    def _prune(self, now):
        if now - self._last_prune < 3600 and len(self.events) <= self.MAX_EVENTS + 1000:
            return
        before = len(self.events)
        cutoff = now - self.RETENTION_SECONDS
        self.events = [event for event in self.events if event["time"] >= cutoff][-self.MAX_EVENTS:]
        self._last_prune = now
        if len(self.events) != before:
            self._rewrite_usage()

    def list_keys(self):
        with self.lock:
            rows = []
            if self.primary_keys:
                rows.append({"id": "primary", "label": "Primary API key", "token": self.primary_key,
                             "active": True, "primary": True, "created_at": None, "last_four": self.primary_key[-4:]})
            for row in sorted(self.keys, key=lambda item: (item["created_at"], item["id"]), reverse=True):
                rows.append({"id": row["id"], "label": row["label"],
                             "token": row["token"] if row["active"] else "",
                             "active": row["active"], "primary": False,
                             "created_at": row["created_at"],
                             "revoked_at": row["revoked_at"],
                             "last_four": row["token"][-4:] if row["active"] else ""})
            return rows

    def create(self, label):
        if not isinstance(label, str):
            raise ValueError("Enter a name for this API key.")
        label = label.strip()
        if not label or len(label) > 64 or any(ord(char) < 32 or ord(char) == 127 for char in label):
            raise ValueError("Use a name from 1 to 64 characters without control characters.")
        with self.lock:
            if sum(row["active"] for row in self.keys) >= 100:
                raise ValueError("The limit of 100 active extra API keys has been reached.")
            token = "strata_" + secrets.token_urlsafe(32)
            row = {"id": uuid.uuid4().hex, "label": label, "token": token, "active": True,
                   "created_at": int(self.clock()), "revoked_at": None}
            self.keys.append(row)
            self._save_keys()
            return {"id": row["id"], "label": label, "token": token, "active": True,
                    "primary": False, "created_at": row["created_at"], "last_four": token[-4:]}

    def revoke(self, key_id):
        if not isinstance(key_id, str) or not key_id or key_id == "primary":
            raise ValueError("The primary API key cannot be revoked here.")
        with self.lock:
            row = next((item for item in self.keys if item["id"] == key_id), None)
            if row is None:
                raise KeyError("API key not found.")
            if not row["active"]:
                return {"id": key_id, "active": False}
            row["token"] = ""
            row["active"] = False
            row["revoked_at"] = int(self.clock())
            self._save_keys()
            return {"id": key_id, "active": False}

    @staticmethod
    def _matches(given, expected):
        if not isinstance(given, str) or not given:
            return False
        raw = given.encode("latin-1", "replace")
        try:
            return hmac.compare_digest(raw, expected.encode()) or hmac.compare_digest(
                raw, expected.encode("latin-1"))
        except UnicodeEncodeError:
            return hmac.compare_digest(raw, expected.encode())

    def authenticate(self, given):
        with self.lock:
            primary_match = False
            for primary_key in self.primary_keys:
                primary_match |= self._matches(given, primary_key)
            if primary_match:
                return "primary"
            for row in self.keys:
                if row["active"] and self._matches(given, row["token"]):
                    return row["id"]
        return None

    def record_usage(self, key_id, cached_tokens, output_tokens, at=None):
        if not isinstance(key_id, str) or not key_id:
            return
        try:
            cached = max(0, int(cached_tokens or 0))
            output = max(0, int(output_tokens or 0))
            now = float(self.clock() if at is None else at)
        except (TypeError, ValueError, OverflowError):
            return
        event = {"time": now, "key_id": key_id,
                 "cached_tokens": cached, "output_tokens": output}
        with self.lock:
            self._private_parent(self.usage_path)
            with self.usage_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, separators=(",", ":")) + "\n")
            self._private_file(self.usage_path)
            self.events.append(event)
            self._prune(now)

    def usage(self, range_name="24h"):
        if range_name not in self.RANGES:
            raise ValueError("Choose 1h, 24h, 7d, or 30d.")
        duration, count = self.RANGES[range_name]
        interval = duration // count
        now = int(self.clock())
        end = now - (now % interval)
        start = end - duration
        with self.lock:
            rows = [{key: value for key, value in row.items() if key != "token"}
                    for row in self.list_keys()]
            by_id = {row["id"]: row for row in rows}
            grouped = {key_id: [0, 0, 0, [[0, 0] for _ in range(count)]] for key_id in by_id}
            for event in self.events:
                at = event["time"]
                key_id = event["key_id"]
                if at < start or at >= end + interval:
                    continue
                if key_id not in grouped:
                    by_id[key_id] = {"id": key_id, "label": "Other API key", "active": False,
                                     "primary": False, "created_at": None, "last_four": ""}
                    grouped[key_id] = [0, 0, 0, [[0, 0] for _ in range(count)]]
                bucket = min(count - 1, max(0, int((at - start) // interval)))
                item = grouped[key_id]
                item[0] += 1
                item[1] += event["cached_tokens"]
                item[2] += event["output_tokens"]
                item[3][bucket][0] += event["cached_tokens"]
                item[3][bucket][1] += event["output_tokens"]
            keys = []
            for key_id, row in by_id.items():
                item = grouped[key_id]
                keys.append({
                    **row,
                    "requests": item[0],
                    "cached_tokens": item[1],
                    "output_tokens": item[2],
                    "series": [{"time": start + i * interval,
                                "cached_tokens": values[0], "output_tokens": values[1]}
                               for i, values in enumerate(item[3])],
                })
            return {"range": range_name, "since": start, "until": end + interval,
                    "bucket_seconds": interval, "keys": keys}
