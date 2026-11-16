"""Load the fictional demo into a running CareHub (Paperless-ngx) instance.

Creates permission groups, demo users, tags, document types and suppliers,
then uploads the PDFs from make_docs.py with per-group access. Safe to re-run:
existing objects and documents (matched by title) are left alone.

Env: CAREHUB_API (default http://127.0.0.1:8040), PAPERLESS_ADMIN_USER,
PAPERLESS_ADMIN_PASSWORD, DEMO_MANAGER_PASSWORD, DEMO_ADMIN_PASSWORD,
DEMO_CARER_PASSWORD.

Usage: python setup_demo.py out/
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests

API = os.environ.get("CAREHUB_API", "http://127.0.0.1:8040").rstrip("/") + "/api"
S = requests.Session()
S.auth = (os.environ["PAPERLESS_ADMIN_USER"], os.environ["PAPERLESS_ADMIN_PASSWORD"])
S.headers["Accept"] = "application/json; version=9"

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

USERS = [
    # username, first, last, groups, password env var
    ("demo.manager", "Demo", "Facility Manager", ["All Staff", "Admin Officers", "Care Managers", "Finance"],
     "DEMO_MANAGER_PASSWORD"),
    ("demo.admin", "Demo", "Admin Officer", ["All Staff", "Admin Officers"], "DEMO_ADMIN_PASSWORD"),
    ("demo.carer", "Demo", "Care Worker", ["All Staff"], "DEMO_CARER_PASSWORD"),
    # Optional tester account; created only when DEMO_TEST_PASSWORD is set.
    ("test.user", "Test", "User", ["All Staff", "Admin Officers", "Care Managers", "Finance"],
     "DEMO_TEST_PASSWORD"),
]
USER_EMAILS = {"test.user": "test.user@example.com"}

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


def all_items(path: str) -> list[dict]:
    items, url = [], f"{API}/{path}/?page_size=100"
    while url:
        r = S.get(url)
        r.raise_for_status()
        data = r.json()
        items += data["results"]
        url = data.get("next")
    return items


def ensure(path: str, name: str, extra: dict | None = None, key: str = "name") -> int:
    for item in all_items(path):
        if item[key] == name:
            return item["id"]
    r = S.post(f"{API}/{path}/", json={key: name, **(extra or {})})
    r.raise_for_status()
    return r.json()["id"]


def wait_for_api() -> None:
    for _ in range(60):
        try:
            if S.get(f"{API}/documents/?page_size=1", timeout=5).ok:
                return
        except requests.RequestException:
            pass
        time.sleep(5)
    sys.exit("API not reachable")


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "out")
    manifest = json.loads((out / "manifest.json").read_text())
    wait_for_api()

    group_ids = {name: ensure("groups", name, {"permissions": perms}) for name, perms in GROUPS.items()}
    for name, perms in GROUPS.items():  # keep permissions in sync on re-runs
        S.patch(f"{API}/groups/{group_ids[name]}/", json={"permissions": perms}).raise_for_status()

    for username, first, last, groups, pw_env in USERS:
        if not os.environ.get(pw_env):
            print(f"skip user {username}: {pw_env} not set")
            continue
        body = {"first_name": first, "last_name": last, "is_active": True, "is_staff": False,
                "is_superuser": False, "groups": [group_ids[g] for g in groups],
                "email": USER_EMAILS.get(username, ""), "password": os.environ[pw_env]}
        uid = ensure("users", username, {k: v for k, v in body.items()}, key="username")
        S.patch(f"{API}/users/{uid}/", json=body).raise_for_status()

    owner_id = next(u["id"] for u in all_items("users") if u["username"] == S.auth[0])
    existing_titles = {d["title"] for d in all_items("documents")}

    for doc in manifest:
        if doc["title"] in existing_titles:
            print("skip (exists):", doc["title"])
            continue
        # owner None = a shared label everyone can see (otherwise it shows as "Private").
        tag_ids = [ensure("tags", t, {"color": TAG_COLOURS.get(t, "#1f5f5b"), "matching_algorithm": 0, "owner": None})
                   for t in doc["tags"]]
        type_id = ensure("document_types", doc["type"], {"matching_algorithm": 0, "owner": None})
        corr_id = ensure("correspondents", doc["correspondent"], {"matching_algorithm": 0, "owner": None})
        fields = [("title", doc["title"]), ("created", doc["created"]), ("document_type", type_id),
                  ("correspondent", corr_id)] + [("tags", t) for t in tag_ids]
        with open(out / doc["file"], "rb") as fh:
            r = S.post(f"{API}/documents/post_document/", data=fields,
                       files={"document": (doc["file"], fh, "application/pdf")})
        r.raise_for_status()
        task_id = r.json() if isinstance(r.json(), str) else r.json().get("task_id")

        doc_id = None
        for _ in range(120):
            tasks = S.get(f"{API}/tasks/", params={"task_id": task_id}).json()
            task = tasks[0] if isinstance(tasks, list) and tasks else None
            if task and task["status"] == "SUCCESS":
                doc_id = task["related_document"]
                break
            if task and task["status"] == "FAILURE":
                sys.exit(f"failed: {doc['file']}: {task.get('result')}")
            time.sleep(2)
        if not doc_id:
            sys.exit(f"timed out: {doc['file']}")

        view, change = ACCESS[doc["access"]]
        S.patch(f"{API}/documents/{doc_id}/", json={
            "owner": owner_id,
            "set_permissions": {
                "view": {"users": [], "groups": [group_ids[g] for g in view]},
                "change": {"users": [], "groups": [group_ids[g] for g in change]},
            },
        }).raise_for_status()
        print(f"added #{doc_id} [{doc['access']}] {doc['title']}")


if __name__ == "__main__":
    main()
