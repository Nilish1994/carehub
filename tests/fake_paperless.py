"""In-memory stand-in for the parts of the Paperless-ngx 3.4.2 REST API the seeder uses.

Mounted on a requests.Session as a transport adapter, so the real HTTP client
code (timeouts aside) runs unchanged. Behaviour mirrors a reading of the
upstream v3.4.2 source, not a running server:

* post_document returns a task id; consumption later creates the document
  owned by the uploading user with no object permissions
  (documents/views.py PostDocumentView: owner_id=request.user.id) and with the
  custom field values given in the `custom_fields` JSON form field.
* PATCH set_permissions replaces direct grants per action and a change grant
  also grants view (documents/permissions.py set_permissions_for_objects).
* PATCH custom_fields replaces the document's custom field list.
* /api/tasks/ with API version 9 returns an unpaginated list with upper-case
  status and related_document (TaskSerializerV9).

It proves nothing about upstream enforcement; it lets tests drive the seeder
through interruptions and failures deterministically.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from email.parser import BytesParser
from email.policy import HTTP
from urllib.parse import parse_qs, urlsplit

import requests
from requests.adapters import BaseAdapter

MUTATING = {"POST", "PATCH", "PUT", "DELETE"}


class Interrupt(Exception):
    """Raised by an interceptor to simulate the seeder process dying mid-request."""


class FakePaperless(BaseAdapter):
    def __init__(self, admin: str = "admin", *, polls_to_consume: int = 1) -> None:
        super().__init__()
        self.next_id = 100
        self.users: dict[int, dict] = {}
        self.groups: dict[int, dict] = {}
        self.custom_fields: dict[int, dict] = {}
        self.documents: dict[int, dict] = {}
        self.tasks: dict[str, dict] = {}
        self.objects: dict[str, dict[int, dict]] = {"tags": {}, "document_types": {}, "correspondents": {}}
        self.uploads: list[dict] = []
        self.requests: list[tuple[str, str]] = []
        self.interceptors: list[Callable[[requests.PreparedRequest, str, str], requests.Response | None]] = []
        self.polls_to_consume = polls_to_consume
        self.task_outcome = "success"  # or "failure", "revoked", "never", "success_no_doc"
        self.admin_id = self.add_user(admin, is_superuser=True)

    # ---------------------------------------------------------------- setup helpers

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id

    def add_user(self, username: str, **fields) -> int:
        uid = self._id()
        self.users[uid] = {"id": uid, "username": username, "first_name": "", "last_name": "", "email": "",
                           "is_active": True, "is_staff": False, "is_superuser": False, "groups": [],
                           "password": None, **fields}
        return uid

    def add_group(self, name: str, permissions: list[str]) -> int:
        gid = self._id()
        self.groups[gid] = {"id": gid, "name": name, "permissions": list(permissions)}
        return gid

    def add_document(self, title: str, owner: int | None = None, custom_fields: list[dict] | None = None,
                     view_groups: list[int] | None = None) -> int:
        did = self._id()
        self.documents[did] = {"id": did, "title": title, "owner": owner, "custom_fields": custom_fields or [],
                               "perms": {"view": {"users": [], "groups": sorted(view_groups or [])},
                                         "change": {"users": [], "groups": []}}}
        return did

    def mutations(self) -> list[tuple[str, str]]:
        return [r for r in self.requests if r[0] in MUTATING]

    def doc_by_key(self, key: str) -> list[dict]:
        return [d for d in self.documents.values() if any(cf.get("value") == key for cf in d["custom_fields"])]

    # ---------------------------------------------------------------- transport

    def send(self, request: requests.PreparedRequest, **kwargs) -> requests.Response:
        parts = urlsplit(request.url)
        path = parts.path
        self.requests.append((request.method, path))
        for interceptor in self.interceptors:
            resp = interceptor(request, request.method, path)
            if resp is not None:
                return resp
        query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
        try:
            status, body = self.route(request, request.method, path, query)
        except KeyError:
            status, body = 404, {"detail": "Not found."}
        return self.response(request, status, body)

    def close(self) -> None:
        pass

    @staticmethod
    def response(request: requests.PreparedRequest, status: int, body, content_type: str = "application/json"
                 ) -> requests.Response:
        resp = requests.Response()
        resp.status_code = status
        resp.request = request
        resp.url = request.url
        if isinstance(body, (bytes, str)):
            resp._content = body.encode() if isinstance(body, str) else body
        else:
            resp._content = json.dumps(body).encode()
        resp.headers["Content-Type"] = content_type
        resp.encoding = "utf-8"
        return resp

    # ---------------------------------------------------------------- routing

    def page(self, request, items: list[dict], query: dict) -> dict:
        size = int(query.get("page_size", 25))
        page = int(query.get("page", 1))
        chunk = items[(page - 1) * size: page * size]
        nxt = None
        if page * size < len(items):
            parts = urlsplit(request.url)
            q = dict(query, page=str(page + 1))
            nxt = f"{parts.scheme}://{parts.netloc}{parts.path}?" + "&".join(f"{k}={v}" for k, v in q.items())
        return {"count": len(items), "next": nxt, "previous": None, "results": chunk}

    def route(self, request, method: str, path: str, query: dict):
        segs = [s for s in path.split("/") if s][1:]  # drop "api"
        body = json.loads(request.body) if request.body and "json" in request.headers.get("Content-Type", "") else None
        coll = segs[0]
        if coll == "documents":
            return self.route_documents(request, method, segs, query, body)
        if coll == "tasks":
            return self.route_tasks(query)
        store = {"users": self.users, "groups": self.groups, "custom_fields": self.custom_fields,
                 **self.objects}[coll]
        if len(segs) == 1 and method == "GET":
            items = list(store.values())
            if "name__iexact" in query:
                items = [i for i in items if i["name"].lower() == query["name__iexact"].lower()]
            return 200, self.page(request, [self.public(coll, i) for i in items], query)
        if len(segs) == 1 and method == "POST":
            oid = self._id()
            obj = {"id": oid, **body}
            if coll == "users":
                obj = {"id": oid, "first_name": "", "last_name": "", "email": "", "is_active": True,
                       "is_staff": False, "is_superuser": False, "groups": [], **body}
            if coll == "custom_fields" and any(f["name"] == body["name"] for f in store.values()):
                return 400, {"name": ["custom field with this name already exists."]}
            store[oid] = obj
            return 201, self.public(coll, obj)
        oid = int(segs[1])
        if method == "PATCH":
            store[oid].update(body)
            return 200, self.public(coll, store[oid])
        if method == "GET":
            return 200, self.public(coll, store[oid])
        raise KeyError(path)

    @staticmethod
    def public(coll: str, obj: dict) -> dict:
        if coll == "users":
            return {k: v for k, v in obj.items() if k != "password"} | {"password": "**********"}
        return dict(obj)

    def doc_json(self, doc: dict, full_perms: bool) -> dict:
        out = {k: v for k, v in doc.items() if k != "perms"}
        if full_perms:
            out["permissions"] = json.loads(json.dumps(doc["perms"]))
        return out

    def route_documents(self, request, method, segs, query, body):
        full = query.get("full_perms") == "true"
        if len(segs) == 1 and method == "GET":
            docs = list(self.documents.values())
            if "custom_field_query" in query:
                field_id, op, value = json.loads(query["custom_field_query"])
                assert op == "exact"
                docs = [d for d in docs if any(cf["field"] == field_id and cf.get("value") == value
                                               for cf in d["custom_fields"])]
            if "title__iexact" in query:
                docs = [d for d in docs if d["title"].lower() == query["title__iexact"].lower()]
            return 200, self.page(request, [self.doc_json(d, full) for d in docs], query)
        if segs[1] == "post_document" and method == "POST":
            return self.post_document(request)
        doc = self.documents[int(segs[1])]
        if method == "GET":
            return 200, self.doc_json(doc, full)
        if method == "PATCH":
            if "owner" in body:
                doc["owner"] = body["owner"]
            if "set_permissions" in body:
                for action in ("view", "change"):
                    entry = body["set_permissions"].get(action)
                    if entry is not None:
                        doc["perms"][action] = {"users": sorted(entry.get("users", [])),
                                                "groups": sorted(entry.get("groups", []))}
                change = doc["perms"]["change"]
                view = doc["perms"]["view"]
                view["groups"] = sorted(set(view["groups"]) | set(change["groups"]))
                view["users"] = sorted(set(view["users"]) | set(change["users"]))
            if "custom_fields" in body:
                doc["custom_fields"] = [{"field": cf["field"], "value": cf.get("value")} for cf in body["custom_fields"]]
            return 200, self.doc_json(doc, False)
        raise KeyError(segs)

    def post_document(self, request):
        ctype = request.headers["Content-Type"]
        msg = BytesParser(policy=HTTP).parsebytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + request.body)
        fields: dict[str, list] = {}
        filename = None
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if name == "document":
                filename = part.get_filename()
                continue
            fields.setdefault(name, []).append(part.get_content().strip() if part.get_content_maintype() == "text"
                                              else part.get_payload(decode=True).decode())
        task_id = str(uuid.uuid4())
        custom = {int(k): v for k, v in json.loads(fields.get("custom_fields", ["{}"])[0]).items()}
        self.uploads.append({"filename": filename, "title": fields["title"][0], "task_id": task_id})
        self.tasks[task_id] = {"task_id": task_id, "status": "PENDING", "polls": 0, "result": None,
                               "related_document": None, "title": fields["title"][0], "custom": custom}
        return 200, json.dumps(task_id)  # DRF Response(str) is a JSON string

    def consume(self, task: dict) -> None:
        if self.task_outcome == "failure":
            task.update(status="FAILURE", result="Not consuming: parser error")
            return
        if self.task_outcome == "revoked":
            task.update(status="REVOKED")
            return
        if self.task_outcome == "never":
            task.update(status="STARTED")
            return
        did = self.add_document(task["title"], owner=self.admin_id,
                                custom_fields=[{"field": f, "value": v} for f, v in task["custom"].items()])
        if self.task_outcome == "success_no_doc":
            task.update(status="SUCCESS", result="ok")
            return
        task.update(status="SUCCESS", related_document=did, result=f"Success. New document id {did} created")

    def finish_pending_tasks(self) -> None:
        for task in self.tasks.values():
            if task["status"] in ("PENDING", "STARTED") and self.task_outcome != "never":
                self.consume(task)

    def route_tasks(self, query):
        task = self.tasks.get(query.get("task_id", ""))
        if task is None:
            return 200, []
        if task["status"] in ("PENDING", "STARTED"):
            task["polls"] += 1
            if task["polls"] >= self.polls_to_consume:
                self.consume(task)
        public = {k: task[k] for k in ("task_id", "status", "result", "related_document")}
        return 200, [public]
