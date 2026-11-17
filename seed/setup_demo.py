"""Load the fictional CareHub demo into a disposable Paperless-ngx instance.

Creates permission groups, demo users, tags, document types and suppliers,
then uploads the PDFs written by make_docs.py and restricts each one to the
groups allowed to see it.

What a re-run does (see README "Loading the demo" for the full contract):

* Every uploaded document carries a stable import key in the custom field
  "CareHub import key". Documents are matched by that key, never by title, so
  re-runs do not create duplicates and an import interrupted between upload
  and permission assignment is finished on the next run.
* Owner and view/change permissions of keyed documents are reconciled to the
  manifest on every run (direct grants not in the manifest are removed).
* Group permission sets and demo-user names, flags and group memberships are
  reconciled. Passwords are only set when a user is created, or with
  --reset-passwords.
* The target must look like a fresh instance or one this tool has seeded
  before (the custom field above exists). Anything else (existing documents,
  groups or users with the demo names) is refused unless --adopt-existing is
  given. Non-loopback API hosts are refused unless named with --allow-host.
* --dry-run performs read-only requests and prints what would change.

Env: CAREHUB_API (default http://127.0.0.1:${CAREHUB_PORT:-8040}),
PAPERLESS_ADMIN_USER, PAPERLESS_ADMIN_PASSWORD (a superuser),
DEMO_MANAGER_PASSWORD, DEMO_ADMIN_PASSWORD, DEMO_CARER_PASSWORD (required),
DEMO_TEST_PASSWORD (optional), CAREHUB_SEED_HTTP_TIMEOUT,
CAREHUB_SEED_TASK_TIMEOUT, CAREHUB_SEED_READY_TIMEOUT (seconds).

Usage: python setup_demo.py [--dry-run] [--adopt-existing] [--reset-passwords]
                            [--allow-host HOST] out/
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

# Paperless-ngx 3.4.2 serves API versions 9 and 10; 9 keeps the unpaginated
# task list with the `status`/`related_document` fields used below.
API_VERSION = "9"
IMPORT_KEY_FIELD = "CareHub import key"
IMPORT_KEY_MAX_LENGTH = 128  # CustomFieldInstance.value_text max_length upstream
DEFAULT_PORT = 8040
STATE_FILE_NAME = ".carehub-import-state.json"

READ_ONLY = ["view_document", "view_tag", "view_correspondent", "view_documenttype", "view_storagepath",
             "view_customfield", "view_savedview", "view_note", "view_sharelink",
             "add_uisettings", "change_uisettings", "view_uisettings", "add_savedview", "change_savedview",
             "delete_savedview", "add_note", "change_note"]
EDITOR = READ_ONLY + ["add_document", "change_document", "add_tag", "change_tag", "add_correspondent",
                      "change_correspondent", "add_documenttype", "change_documenttype", "view_paperlesstask",
                      "add_sharelink", "change_sharelink", "view_workflow"]

GROUPS = {
    "All Staff": READ_ONLY,
    "Admin Officers": EDITOR,
    "Care Managers": EDITOR,
    "Finance": EDITOR,
}


@dataclass(frozen=True)
class DemoUser:
    username: str
    first_name: str
    last_name: str
    groups: tuple[str, ...]
    password_env: str
    required: bool
    email: str = ""


USERS = [
    DemoUser("demo.manager", "Demo", "Facility Manager", ("All Staff", "Admin Officers", "Care Managers", "Finance"),
             "DEMO_MANAGER_PASSWORD", required=True),
    DemoUser("demo.admin", "Demo", "Admin Officer", ("All Staff", "Admin Officers"), "DEMO_ADMIN_PASSWORD",
             required=True),
    DemoUser("demo.carer", "Demo", "Care Worker", ("All Staff",), "DEMO_CARER_PASSWORD", required=True),
    # Optional tester account; created only when DEMO_TEST_PASSWORD is set.
    DemoUser("test.user", "Test", "User", ("All Staff", "Admin Officers", "Care Managers", "Finance"),
             "DEMO_TEST_PASSWORD", required=False, email="test.user@example.com"),
]

# access level -> (groups that can view, groups that can edit)
ACCESS = {
    "all": (["All Staff", "Admin Officers", "Care Managers", "Finance"], ["Admin Officers"]),
    "admin": (["Admin Officers", "Care Managers"], ["Admin Officers"]),
    "clinical": (["Care Managers"], ["Care Managers"]),
    "finance": (["Finance", "Admin Officers"], ["Finance", "Admin Officers"]),
    "finance_only": (["Finance"], ["Finance"]),
}

TAG_COLOURS = {
    "Current version": "#16a34a", "Superseded": "#dc2626", "Confidential": "#7c3aed",
    "Awaiting approval": "#f59e0b", "Paid": "#0ea5e9",
}

MANIFEST_FIELDS = ("key", "file", "title", "created", "type", "correspondent", "tags", "access")


# --------------------------------------------------------------------------- errors


class SeedError(RuntimeError):
    """Base class: reported to the user without a traceback."""


class ConfigError(SeedError):
    """Required configuration is missing or invalid. Nothing was sent."""


class SafetyError(SeedError):
    """The target is not one this tool may change without an explicit opt-in."""


class ApiError(SeedError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class TaskFailed(SeedError):
    """The consumption task finished without producing a document."""


class TaskTimeout(SeedError):
    """The consumption task did not finish before the deadline."""


class ImportConflict(SeedError):
    """More than one document matches an import identity. Nothing is deleted."""


# --------------------------------------------------------------------------- settings


@dataclass(frozen=True)
class Settings:
    api_root: str
    admin_user: str
    admin_password: str = field(repr=False)
    user_passwords: Mapping[str, str] = field(repr=False, default_factory=dict)
    connect_timeout: float = 5.0
    read_timeout: float = 30.0
    upload_timeout: float = 120.0
    ready_timeout: float = 300.0
    task_timeout: float = 300.0
    poll_interval: float = 2.0


def default_api_root(env: Mapping[str, str]) -> str:
    """CAREHUB_API if set, otherwise the loopback listener on CAREHUB_PORT."""
    api = (env.get("CAREHUB_API") or "").strip()
    if api:
        api = api.rstrip("/")
        if api.endswith("/api"):
            api = api[: -len("/api")]
        return api
    port = (env.get("CAREHUB_PORT") or "").strip() or str(DEFAULT_PORT)
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ConfigError(f"CAREHUB_PORT must be a port number (1-65535), got {port!r}")
    return f"http://127.0.0.1:{int(port)}"


def _positive_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number of seconds, got {raw!r}") from None
    if value <= 0:
        raise ConfigError(f"{name} must be greater than zero")
    return value


def load_settings(env: Mapping[str, str]) -> Settings:
    """Validate the environment before any request is made.

    Every missing variable is reported at once; values are never echoed.
    """
    missing = [name for name in ("PAPERLESS_ADMIN_USER", "PAPERLESS_ADMIN_PASSWORD") if not env.get(name)]
    missing += [u.password_env for u in USERS if u.required and not env.get(u.password_env)]
    if missing:
        raise ConfigError(
            "missing required settings: " + ", ".join(missing)
            + ". Set them in .env (see .env.example); the documented demo accounts need their passwords."
        )
    passwords = {u.username: env[u.password_env] for u in USERS if env.get(u.password_env)}
    read_timeout = _positive_float(env, "CAREHUB_SEED_HTTP_TIMEOUT", 30.0)
    return Settings(
        api_root=default_api_root(env),
        admin_user=env["PAPERLESS_ADMIN_USER"],
        admin_password=env["PAPERLESS_ADMIN_PASSWORD"],
        user_passwords=passwords,
        read_timeout=read_timeout,
        upload_timeout=max(read_timeout, 120.0),
        task_timeout=_positive_float(env, "CAREHUB_SEED_TASK_TIMEOUT", 300.0),
        ready_timeout=_positive_float(env, "CAREHUB_SEED_READY_TIMEOUT", 300.0),
    )


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_target(api_root: str, allowed_hosts: Iterable[str] = ()) -> list[str]:
    """Refuse non-loopback targets unless the host is named explicitly.

    Returns warnings for allowed-but-risky targets.
    """
    parts = urlsplit(api_root)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError(f"CAREHUB_API must be an http(s) URL, got {api_root!r}")
    host = parts.hostname
    if _is_loopback(host):
        return []
    if host not in set(allowed_hosts):
        raise SafetyError(
            f"refusing to seed non-loopback host {host!r}. The seeder changes users, groups and permissions; "
            f"only point it at a disposable instance. If that is what {host!r} is, pass --allow-host {host}."
        )
    if parts.scheme == "http":
        return [f"warning: {host} is reached over plain HTTP; admin and demo credentials are sent unencrypted"]
    return []


# --------------------------------------------------------------------------- HTTP client


class PaperlessClient:
    """Small Paperless-ngx REST client with bounded, explicit failure modes.

    Every request has a (connect, read) timeout, redirects are not followed,
    non-expected status codes and non-JSON bodies raise ApiError, and only
    GET requests are retried (connection errors and 502/503/504).
    """

    RETRY_STATUSES = (502, 503, 504)

    def __init__(self, settings: Settings, *, session: requests.Session | None = None,
                 get_retries: int = 2, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.settings = settings
        self.base = settings.api_root.rstrip("/") + "/api"
        self.session = session or requests.Session()
        self.session.auth = (settings.admin_user, settings.admin_password)
        self.session.headers["Accept"] = f"application/json; version={API_VERSION}"
        self.get_retries = get_retries
        self.sleep = sleep
        self.clock = clock
        self._origin = urlsplit(self.base)[:2]

    def _url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            if urlsplit(path)[:2] != self._origin:
                raise ApiError(f"refusing to follow link to a different origin: {path}")
            return path
        return f"{self.base}/{path.lstrip('/')}"

    def request(self, method: str, path: str, *, params: Mapping[str, Any] | None = None,
                json_body: Any = None, data: Any = None, files: Any = None,
                expect: tuple[int, ...] = (200,), timeout: float | None = None, retry: bool = True) -> Any:
        url = self._url(path)
        label = f"{method} {urlsplit(url).path}"
        read_timeout = timeout or self.settings.read_timeout
        attempts = 1 + (self.get_retries if method == "GET" and retry else 0)
        last_error: ApiError | None = None
        for attempt in range(attempts):
            try:
                resp = self.session.request(method, url, params=params, json=json_body, data=data, files=files,
                                            timeout=(self.settings.connect_timeout, read_timeout),
                                            allow_redirects=False)
            except requests.Timeout:
                last_error = ApiError(f"{label}: no response within {read_timeout:g}s")
            except requests.ConnectionError as exc:
                last_error = ApiError(f"{label}: connection failed ({type(exc).__name__})")
            else:
                if resp.status_code in self.RETRY_STATUSES and attempt < attempts - 1:
                    last_error = ApiError(f"{label}: HTTP {resp.status_code}", resp.status_code)
                else:
                    return self._decode(label, resp, expect)
            if attempt < attempts - 1:
                self.sleep(min(2.0 * (attempt + 1), 5.0))
        assert last_error is not None
        raise last_error

    @staticmethod
    def _decode(label: str, resp: requests.Response, expect: tuple[int, ...]) -> Any:
        if resp.status_code not in expect:
            if 300 <= resp.status_code < 400:
                raise ApiError(f"{label}: unexpected redirect to {resp.headers.get('Location')!r}", resp.status_code)
            snippet = " ".join((resp.text or "").split())[:200]
            raise ApiError(f"{label}: HTTP {resp.status_code}: {snippet}", resp.status_code)
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            ctype = resp.headers.get("Content-Type", "unknown")
            raise ApiError(f"{label}: expected JSON, got {ctype}", resp.status_code) from None

    def list_all(self, path: str, params: Mapping[str, Any] | None = None) -> list[dict]:
        query = {"page_size": 100, **(params or {})}
        url: str | None = path
        items: list[dict] = []
        seen: set[str] = set()
        while url:
            if url in seen:
                raise ApiError(f"GET {path}: pagination loop at {url}")
            seen.add(url)
            data = self.request("GET", url, params=query)
            query = None  # the `next` link already carries the query string
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise ApiError(f"GET {path}: malformed list response (no 'results' list)")
            items += data["results"]
            url = data.get("next")
        return items

    def wait_until_ready(self, timeout: float) -> None:
        deadline = self.clock() + timeout
        while True:
            try:
                self.request("GET", "documents/", params={"page_size": 1}, retry=False,
                             timeout=min(self.settings.read_timeout, 10.0))
                return
            except ApiError as exc:
                if exc.status in (401, 403):
                    raise ApiError(f"API rejected the admin credentials (HTTP {exc.status}); "
                                   "check PAPERLESS_ADMIN_USER / PAPERLESS_ADMIN_PASSWORD", exc.status) from None
                last = str(exc)
            if self.clock() >= deadline:
                raise ApiError(f"API at {self.base} not ready after {timeout:g}s (last error: {last})")
            self.sleep(min(self.settings.poll_interval * 2.5, max(0.0, deadline - self.clock())))

    def wait_for_task(self, task_id: str, timeout: float) -> int:
        """Poll a consumption task until it yields a document id.

        Raises TaskFailed (FAILURE, REVOKED, or SUCCESS without a document),
        TaskTimeout (deadline passed) or ApiError (HTTP or malformed data).
        """
        deadline = self.clock() + timeout
        status = "not yet visible"
        while True:
            data = self.request("GET", "tasks/", params={"task_id": task_id})
            if isinstance(data, dict) and isinstance(data.get("results"), list):
                tasks = data["results"]  # API v10 shape, tolerated
            elif isinstance(data, list):
                tasks = data
            else:
                raise ApiError(f"GET /api/tasks/: malformed response for task {task_id}")
            task = next((t for t in tasks if isinstance(t, dict) and t.get("task_id") == task_id), None)
            if task is not None:
                raw_status = task.get("status")
                if not isinstance(raw_status, str):
                    raise ApiError(f"GET /api/tasks/: task {task_id} has no status")
                status = raw_status.upper()
                if status == "SUCCESS":
                    doc_id = task.get("related_document")
                    if isinstance(doc_id, int) or (isinstance(doc_id, str) and doc_id.isdigit()):
                        return int(doc_id)
                    raise TaskFailed(f"task {task_id} succeeded but reported no document (result: {task.get('result')!r})")
                if status in ("FAILURE", "REVOKED"):
                    raise TaskFailed(f"task {task_id} {status.lower()}: {task.get('result')!r}")
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise TaskTimeout(f"task {task_id} not finished after {timeout:g}s (last status: {status})")
            self.sleep(min(self.settings.poll_interval, remaining))


# --------------------------------------------------------------------------- local import state


class ImportState:
    """Remembers task ids of uploads not yet confirmed, per API root.

    Lets a re-run wait for an upload that was still being consumed when the
    previous run stopped, instead of uploading the file a second time.
    """

    def __init__(self, path: Path | None, api_root: str) -> None:
        self.path = path
        self.api_root = api_root
        self._data: dict[str, dict[str, str]] = {}
        if path is not None and path.exists():
            try:
                loaded = json.loads(path.read_text())
            except ValueError:
                raise ConfigError(f"{path} is not valid JSON; remove it after checking for duplicate uploads") from None
            if isinstance(loaded, dict):
                self._data = {k: dict(v) for k, v in loaded.items() if isinstance(v, dict)}

    def pending(self, key: str) -> str | None:
        return self._data.get(self.api_root, {}).get(key)

    def record(self, key: str, task_id: str) -> None:
        self._data.setdefault(self.api_root, {})[key] = task_id
        self._save()

    def clear(self, key: str) -> None:
        if self._data.get(self.api_root, {}).pop(key, None) is not None:
            self._save()

    def _save(self) -> None:
        if self.path is None:
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True))
        os.replace(tmp, self.path)


# --------------------------------------------------------------------------- manifest


def validate_manifest(manifest: Any, out: Path) -> list[dict]:
    if not isinstance(manifest, list) or not manifest:
        raise ConfigError("manifest.json must be a non-empty list")
    keys: set[str] = set()
    problems: list[str] = []
    for i, doc in enumerate(manifest):
        if not isinstance(doc, dict):
            problems.append(f"entry {i} is not an object")
            continue
        absent = [f for f in MANIFEST_FIELDS if f not in doc]
        if absent:
            problems.append(f"entry {i} lacks {', '.join(absent)} (regenerate with make_docs.py)")
            continue
        key = doc["key"]
        if not isinstance(key, str) or not key or len(key) > IMPORT_KEY_MAX_LENGTH:
            problems.append(f"entry {i}: key must be a 1-{IMPORT_KEY_MAX_LENGTH} character string")
        elif key in keys:
            problems.append(f"entry {i}: duplicate key {key!r}")
        keys.add(key)
        if doc["access"] not in ACCESS:
            problems.append(f"entry {i}: unknown access level {doc['access']!r}")
        if not (out / doc["file"]).is_file():
            problems.append(f"entry {i}: file not found: {doc['file']}")
    if problems:
        raise ConfigError("invalid manifest: " + "; ".join(problems))
    return manifest


# --------------------------------------------------------------------------- seeding


@dataclass
class Options:
    dry_run: bool = False
    adopt_existing: bool = False
    reset_passwords: bool = False


@dataclass
class Summary:
    uploaded: list[str] = field(default_factory=list)
    reconciled: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    adopted: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _desired_permissions(access: str, group_ids: Mapping[str, int]) -> dict:
    view, change = ACCESS[access]
    # Upstream grants view along with change, so the readback view set is the union.
    view_ids = sorted({group_ids[g] for g in view} | {group_ids[g] for g in change})
    change_ids = sorted(group_ids[g] for g in change)
    return {"view": {"users": [], "groups": view_ids}, "change": {"users": [], "groups": change_ids}}


def _normalise_permissions(perms: Any) -> dict | None:
    if not isinstance(perms, dict):
        return None
    out = {}
    for action in ("view", "change"):
        entry = perms.get(action) or {}
        out[action] = {"users": sorted(entry.get("users") or []), "groups": sorted(entry.get("groups") or [])}
    return out


class Seeder:
    def __init__(self, client: PaperlessClient, settings: Settings, manifest: list[dict], out: Path,
                 options: Options | None = None, state: ImportState | None = None,
                 log: Callable[[str], None] = print) -> None:
        self.client = client
        self.settings = settings
        self.manifest = manifest
        self.out = out
        self.options = options or Options()
        self.state = state or ImportState(None, settings.api_root)
        self.log = log
        self.summary = Summary()
        self.field_id: int | None = None
        self.admin_id: int | None = None
        self._placeholder = 0

    # -- helpers

    def _fake_id(self) -> int:
        self._placeholder -= 1
        return self._placeholder

    def _mutate(self, method: str, path: str, what: str, **kwargs: Any) -> Any:
        if self.options.dry_run:
            self.log(f"[dry-run] would {what}")
            return None
        return self.client.request(method, path, **kwargs)

    # -- phases

    def run(self) -> Summary:
        self.client.wait_until_ready(self.settings.ready_timeout)
        snapshot = self.preflight()
        self.ensure_import_field(snapshot)
        group_ids = self.sync_groups(snapshot["groups"])
        self.sync_users(snapshot["users"], group_ids)
        for doc in self.manifest:
            self.sync_document(doc, group_ids)
        s = self.summary
        self.log(f"done: {len(s.uploaded)} uploaded, {len(s.adopted)} adopted, {len(s.reconciled)} permissions fixed, "
                 f"{len(s.unchanged)} unchanged, {len(s.skipped)} skipped")
        return s

    def preflight(self) -> dict:
        users = self.client.list_all("users/")
        groups = self.client.list_all("groups/")
        fields = self.client.list_all("custom_fields/")
        docs = self.client.request("GET", "documents/", params={"page_size": 1})
        doc_count = docs.get("count", 0) if isinstance(docs, dict) else 0

        admin = next((u for u in users if u.get("username") == self.settings.admin_user), None)
        if admin is None:
            raise SafetyError(f"admin user {self.settings.admin_user!r} is not listed by /api/users/")
        if not admin.get("is_superuser"):
            raise SafetyError(f"{self.settings.admin_user!r} is not a superuser; it must own the demo documents "
                              "and be allowed to set their permissions")
        self.admin_id = admin["id"]

        marker = next((f for f in fields if f.get("name") == IMPORT_KEY_FIELD), None)
        if marker is not None and marker.get("data_type") not in (None, "string"):
            raise SafetyError(f"custom field {IMPORT_KEY_FIELD!r} exists with type {marker.get('data_type')!r}, "
                              "expected 'string'")
        clashing_groups = sorted(g["name"] for g in groups if g.get("name") in GROUPS)
        clashing_users = sorted(u["username"] for u in users if u.get("username") in {d.username for d in USERS})
        if marker is None and (doc_count or clashing_groups or clashing_users) and not self.options.adopt_existing:
            raise SafetyError(
                "target is not empty and has not been seeded by this version of CareHub "
                f"(no {IMPORT_KEY_FIELD!r} custom field). Found {doc_count} document(s), groups {clashing_groups}, "
                f"users {clashing_users}. Nothing was changed. Re-run with --adopt-existing only if this is a "
                "CareHub demo instance whose groups, demo users and same-titled documents may be modified."
            )
        if marker is not None:
            self.field_id = marker["id"]
        return {"users": users, "groups": groups, "marker": marker}

    def ensure_import_field(self, snapshot: dict) -> None:
        if snapshot["marker"] is not None:
            return
        created = self._mutate("POST", "custom_fields/", f"create custom field {IMPORT_KEY_FIELD!r}",
                               json_body={"name": IMPORT_KEY_FIELD, "data_type": "string"}, expect=(201,))
        self.field_id = created["id"] if created else None

    def sync_groups(self, existing: list[dict]) -> dict[str, int]:
        by_name = {g["name"]: g for g in existing if "name" in g}
        ids: dict[str, int] = {}
        for name, perms in GROUPS.items():
            current = by_name.get(name)
            if current is None:
                created = self._mutate("POST", "groups/", f"create group {name!r}",
                                       json_body={"name": name, "permissions": perms}, expect=(201,))
                ids[name] = created["id"] if created else self._fake_id()
                self.log(f"group {name}: created")
                continue
            ids[name] = current["id"]
            have, want = set(current.get("permissions") or []), set(perms)
            if have != want:
                self._mutate("PATCH", f"groups/{current['id']}/",
                             f"set permissions of group {name!r} (+{sorted(want - have)} -{sorted(have - want)})",
                             json_body={"permissions": perms})
                self.log(f"group {name}: permissions reconciled")
        return ids

    def sync_users(self, existing: list[dict], group_ids: Mapping[str, int]) -> None:
        by_name = {u["username"]: u for u in existing if "username" in u}
        for user in USERS:
            password = self.settings.user_passwords.get(user.username)
            current = by_name.get(user.username)
            desired = {"first_name": user.first_name, "last_name": user.last_name, "email": user.email,
                       "is_active": True, "is_staff": False, "is_superuser": False,
                       "groups": sorted(group_ids[g] for g in user.groups)}
            if current is None:
                if not password:
                    self.log(f"user {user.username}: not created ({user.password_env} not set)")
                    continue
                self._mutate("POST", "users/", f"create user {user.username!r}",
                             json_body={"username": user.username, **desired, "password": password}, expect=(201,))
                self.log(f"user {user.username}: created")
                continue
            if not password and not user.required:
                self.log(f"user {user.username}: left unchanged ({user.password_env} not set)")
                continue
            changes = {k: v for k, v in desired.items()
                       if (sorted(current.get(k) or []) if k == "groups" else current.get(k)) != v}
            if self.options.reset_passwords and password:
                changes["password"] = password
            if changes:
                shown = sorted(k for k in changes)
                self._mutate("PATCH", f"users/{current['id']}/", f"update user {user.username!r} ({', '.join(shown)})",
                             json_body=changes)
                self.log(f"user {user.username}: updated {', '.join(shown)}")

    # -- documents

    def _has_key(self, doc: dict, key: str | None = None) -> bool:
        for cf in doc.get("custom_fields") or []:
            if cf.get("field") == self.field_id and (key is None or cf.get("value") == key):
                return True
        return False

    def find_by_key(self, key: str) -> list[dict]:
        if self.field_id is None or self.field_id < 0:
            return []
        found = self.client.list_all("documents/", {
            "custom_field_query": json.dumps([self.field_id, "exact", key]), "full_perms": "true"})
        # Re-check locally so a server that ignored the filter cannot cause a wrong match.
        return [d for d in found if self._has_key(d, key)]

    def find_unkeyed_by_title(self, title: str) -> list[dict]:
        found = self.client.list_all("documents/", {"title__iexact": title, "full_perms": "true"})
        return [d for d in found if d.get("title") == title and not self._has_key(d)]

    def get_document(self, doc_id: int) -> dict:
        doc = self.client.request("GET", f"documents/{doc_id}/", params={"full_perms": "true"})
        if not isinstance(doc, dict) or doc.get("id") != doc_id:
            raise ApiError(f"GET /api/documents/{doc_id}/: malformed document response")
        return doc

    def sync_document(self, doc: dict, group_ids: Mapping[str, int]) -> None:
        key, title = doc["key"], doc["title"]
        matches = self.find_by_key(key)
        if len(matches) > 1:
            raise ImportConflict(f"{len(matches)} documents carry import key {key!r} "
                                 f"(ids {sorted(d['id'] for d in matches)}); resolve manually, nothing was deleted")
        current = matches[0] if matches else None

        if current is None:
            task_id = self.state.pending(key)
            if task_id:
                self.log(f"waiting for earlier upload of {title!r} (task {task_id})")
                try:
                    current = self.get_document(self.client.wait_for_task(task_id, self.settings.task_timeout))
                except TaskFailed as exc:
                    self.log(f"earlier upload did not produce a document ({exc}); uploading again")
                    self.state.clear(key)
                except TaskTimeout as exc:
                    raise TaskTimeout(f"{exc}. The earlier upload of {title!r} may still be processing; re-run later. "
                                      f"If it can never finish, remove its entry from {STATE_FILE_NAME} "
                                      "after checking the instance for a duplicate.") from None
                else:
                    self.state.clear(key)
                    if not self._has_key(current, key):
                        raise ImportConflict(f"task {task_id} produced document #{current['id']} without import key {key!r}")

        if current is None:
            legacy = self.find_unkeyed_by_title(title)
            if legacy and not self.options.adopt_existing:
                self.log(f"skip (exists without import key, not modified): {title} "
                         f"(#{', #'.join(str(d['id']) for d in legacy)})")
                self.summary.skipped.append(key)
                return
            if len(legacy) > 1:
                raise ImportConflict(f"{len(legacy)} documents titled {title!r} have no import key "
                                     f"(ids {sorted(d['id'] for d in legacy)}); adopt one manually")
            if legacy:
                current = self.adopt(legacy[0], key)
                if current is None:  # dry run
                    return
            else:
                current = self.upload(doc)
                if current is None:  # dry run
                    return

        self.reconcile_permissions(current, doc, group_ids)

    def adopt(self, existing: dict, key: str) -> dict | None:
        fields = [{"field": cf["field"], "value": cf.get("value")} for cf in existing.get("custom_fields") or []]
        fields.append({"field": self.field_id, "value": key})
        self._mutate("PATCH", f"documents/{existing['id']}/",
                     f"tag document #{existing['id']} {existing.get('title')!r} with import key {key!r}",
                     json_body={"custom_fields": fields})
        self.summary.adopted.append(key)
        if self.options.dry_run:
            return None
        return self.get_document(existing["id"])

    def upload(self, doc: dict) -> dict | None:
        if self.options.dry_run:
            self.log(f"[dry-run] would upload {doc['file']!r} as {doc['title']!r} [{doc['access']}]")
            return None
        # owner None = a shared label everyone can see (otherwise it shows as "Private").
        tag_ids = [self.ensure("tags", t, {"color": TAG_COLOURS.get(t, "#1f5f5b"), "matching_algorithm": 0,
                                           "owner": None}) for t in doc["tags"]]
        type_id = self.ensure("document_types", doc["type"], {"matching_algorithm": 0, "owner": None})
        corr_id = self.ensure("correspondents", doc["correspondent"], {"matching_algorithm": 0, "owner": None})
        fields = [("title", doc["title"]), ("created", doc["created"]), ("document_type", type_id),
                  ("correspondent", corr_id),
                  ("custom_fields", json.dumps({str(self.field_id): doc["key"]}))] + [("tags", t) for t in tag_ids]
        with open(self.out / doc["file"], "rb") as fh:
            task_id = self.client.request("POST", "documents/post_document/", data=fields,
                                          files={"document": (doc["file"], fh, "application/pdf")},
                                          timeout=self.settings.upload_timeout)
        if isinstance(task_id, dict):
            task_id = task_id.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ApiError(f"POST /api/documents/post_document/: no task id returned for {doc['file']!r}")
        self.state.record(doc["key"], task_id)
        try:
            doc_id = self.client.wait_for_task(task_id, self.settings.task_timeout)
        except TaskFailed:
            self.state.clear(doc["key"])
            raise
        self.state.clear(doc["key"])
        self.summary.uploaded.append(doc["key"])
        self.log(f"uploaded #{doc_id} {doc['title']}")
        return self.get_document(doc_id)

    def ensure(self, path: str, name: str, extra: dict) -> int:
        for item in self.client.list_all(f"{path}/", {"name__iexact": name}):
            if item.get("name") == name:
                return item["id"]
        created = self.client.request("POST", f"{path}/", json_body={"name": name, **extra}, expect=(201,))
        return created["id"]

    def reconcile_permissions(self, current: dict, doc: dict, group_ids: Mapping[str, int]) -> None:
        desired = _desired_permissions(doc["access"], group_ids)
        have = _normalise_permissions(current.get("permissions"))
        if current.get("owner") == self.admin_id and have == desired:
            self.summary.unchanged.append(doc["key"])
            return
        self._mutate("PATCH", f"documents/{current['id']}/",
                     f"set owner and [{doc['access']}] permissions on #{current['id']} {doc['title']!r}",
                     json_body={"owner": self.admin_id, "set_permissions": desired})
        if self.options.dry_run:
            return
        after = self.get_document(current["id"])
        if after.get("owner") != self.admin_id or _normalise_permissions(after.get("permissions")) != desired:
            raise ApiError(f"permissions on document #{current['id']} did not take effect "
                           f"(wanted {desired}, server reports {after.get('permissions')})")
        self.summary.reconciled.append(doc["key"])
        self.log(f"permissions set #{current['id']} [{doc['access']}] {doc['title']}")


# --------------------------------------------------------------------------- CLI


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("out", nargs="?", default="out", help="directory written by make_docs.py")
    parser.add_argument("--dry-run", action="store_true", help="read-only: report what would change")
    parser.add_argument("--adopt-existing", action="store_true",
                        help="allow changes on a non-empty instance not marked as seeded by this tool")
    parser.add_argument("--reset-passwords", action="store_true",
                        help="also reset passwords of existing demo users to the configured values")
    parser.add_argument("--allow-host", action="append", default=[], metavar="HOST",
                        help="permit this non-loopback API host (repeatable)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None,
         session: requests.Session | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    env = os.environ if env is None else env
    out = Path(args.out)
    try:
        settings = load_settings(env)
        for warning in check_target(settings.api_root, args.allow_host):
            print(warning, file=sys.stderr)
        manifest_path = out / "manifest.json"
        if not manifest_path.is_file():
            raise ConfigError(f"{manifest_path} not found; run make_docs.py first")
        try:
            manifest = json.loads(manifest_path.read_text())
        except ValueError:
            raise ConfigError(f"{manifest_path} is not valid JSON") from None
        manifest = validate_manifest(manifest, out)
        options = Options(dry_run=args.dry_run, adopt_existing=args.adopt_existing,
                          reset_passwords=args.reset_passwords)
        state = ImportState(None if args.dry_run else out / STATE_FILE_NAME, settings.api_root)
        client = PaperlessClient(settings, session=session)
        Seeder(client, settings, manifest, out, options, state).run()
    except (ConfigError, SafetyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except SeedError as exc:
        print(f"error: {exc}\nRe-run the same command to resume; finished documents are not uploaded again.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
