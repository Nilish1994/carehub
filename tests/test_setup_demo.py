"""Regression tests for seed/setup_demo.py against an in-memory Paperless fake.

These check the seeder's own behaviour (configuration, request bounds,
recovery, idempotency, refusal rules). They do not show that Paperless-ngx
enforces the permissions the seeder sets; that needs a real instance.
"""

from __future__ import annotations

import json
import time

import pytest
import requests

import setup_demo
from conftest import API, ENV, FakeClock, make_seeder, make_session
from fake_paperless import FakePaperless, Interrupt

S = setup_demo


def key_field_id(fake: FakePaperless) -> int:
    return next(f["id"] for f in fake.custom_fields.values() if f["name"] == S.IMPORT_KEY_FIELD)


def group_ids(fake: FakePaperless) -> dict[str, int]:
    return {g["name"]: g["id"] for g in fake.groups.values()}


def assert_demo_state(fake: FakePaperless, manifest: list[dict]) -> None:
    gids = group_ids(fake)
    fid = key_field_id(fake)
    assert len(fake.documents) == len(manifest)
    for entry in manifest:
        docs = fake.doc_by_key(entry["key"])
        assert len(docs) == 1, entry["key"]
        doc = docs[0]
        assert doc["custom_fields"] == [{"field": fid, "value": entry["key"]}]
        assert doc["owner"] == fake.admin_id
        view, change = S.ACCESS[entry["access"]]
        assert doc["perms"]["view"] == {"users": [], "groups": sorted(gids[g] for g in set(view) | set(change))}
        assert doc["perms"]["change"] == {"users": [], "groups": sorted(gids[g] for g in change)}


def run_main(fake, out_dir, argv=(), env=None, api=API):
    full_env = {**ENV, **(env or {})}
    return S.main([*argv, str(out_dir)], env=full_env, session=make_session(fake, api))


# --------------------------------------------------------------------------- configuration


def test_missing_demo_passwords_stop_before_any_request(fake, out_dir, capsys):
    env = {k: v for k, v in ENV.items() if not k.startswith("DEMO_")}
    code = S.main([str(out_dir)], env=env, session=make_session(fake))
    err = capsys.readouterr().err
    assert code == 2
    for name in ("DEMO_MANAGER_PASSWORD", "DEMO_ADMIN_PASSWORD", "DEMO_CARER_PASSWORD"):
        assert name in err
    assert "manager-test-password" not in err
    assert fake.requests == []


def test_load_settings_reports_every_missing_variable():
    with pytest.raises(S.ConfigError) as exc:
        S.load_settings({"DEMO_ADMIN_PASSWORD": "x"})
    msg = str(exc.value)
    for name in ("PAPERLESS_ADMIN_USER", "PAPERLESS_ADMIN_PASSWORD", "DEMO_MANAGER_PASSWORD", "DEMO_CARER_PASSWORD"):
        assert name in msg
    assert "DEMO_ADMIN_PASSWORD" not in msg
    assert "DEMO_TEST_PASSWORD" not in msg  # optional account


def test_env_example_documents_every_seeder_variable():
    from conftest import ROOT
    example = (ROOT / ".env.example").read_text()
    for user in S.USERS:
        assert user.password_env in example
    for name in ("PAPERLESS_ADMIN_USER", "PAPERLESS_ADMIN_PASSWORD", "CAREHUB_PORT", "CAREHUB_API"):
        assert name in example


@pytest.mark.parametrize("env,expected", [
    ({}, "http://127.0.0.1:8040"),
    ({"CAREHUB_PORT": "8123"}, "http://127.0.0.1:8123"),
    ({"CAREHUB_PORT": "8123", "CAREHUB_API": "http://localhost:9000/"}, "http://localhost:9000"),
    ({"CAREHUB_API": "http://127.0.0.1:9000/api/"}, "http://127.0.0.1:9000"),
])
def test_api_root_follows_carehub_port(env, expected):
    assert S.default_api_root(env) == expected


@pytest.mark.parametrize("port", ["", "abc", "0", "70000"])
def test_invalid_carehub_port_rejected(port):
    if port == "":
        assert S.default_api_root({"CAREHUB_PORT": port}).endswith(":8040")
        return
    with pytest.raises(S.ConfigError):
        S.default_api_root({"CAREHUB_PORT": port})


def test_seeder_targets_non_default_port_end_to_end(out_dir, manifest):
    fake = FakePaperless()
    api = "http://127.0.0.1:8123"
    code = run_main(fake, out_dir, env={"CAREHUB_PORT": "8123"}, api=api)
    assert code == 0
    assert_demo_state(fake, manifest)


def test_invalid_manifest_rejected_before_requests(fake, out_dir, capsys):
    manifest = json.loads((out_dir / "manifest.json").read_text())
    manifest[1]["key"] = manifest[0]["key"]
    del manifest[2]["key"]
    (out_dir / "manifest.json").write_text(json.dumps(manifest))
    assert run_main(fake, out_dir) == 2
    err = capsys.readouterr().err
    assert "duplicate key" in err and "lacks key" in err
    assert fake.requests == []


# --------------------------------------------------------------------------- target protection


def test_remote_host_refused_without_opt_in(fake, out_dir, capsys):
    code = run_main(fake, out_dir, env={"CAREHUB_API": "http://10.1.2.3:8040"})
    assert code == 2
    assert "--allow-host 10.1.2.3" in capsys.readouterr().err
    assert fake.requests == []


def test_named_remote_host_allowed_with_warning(out_dir, manifest, capsys):
    fake = FakePaperless()
    api = "http://carehub-test.internal:8040"
    code = run_main(fake, out_dir, argv=["--allow-host", "carehub-test.internal"], env={"CAREHUB_API": api}, api=api)
    assert code == 0
    assert "unencrypted" in capsys.readouterr().err
    assert_demo_state(fake, manifest)


def test_unmarked_instance_with_colliding_group_is_not_touched(fake, out_dir):
    gid = fake.add_group("Finance", ["view_document", "delete_document"])
    code = run_main(fake, out_dir)
    assert code == 2
    assert fake.mutations() == []
    assert fake.groups[gid]["permissions"] == ["view_document", "delete_document"]


def test_unmarked_instance_with_existing_documents_is_not_touched(fake, out_dir, capsys):
    fake.add_document("Somebody's real document", owner=fake.admin_id)
    assert run_main(fake, out_dir) == 2
    assert "--adopt-existing" in capsys.readouterr().err
    assert fake.mutations() == []


def test_existing_demo_user_on_unmarked_instance_is_not_touched(fake, out_dir):
    uid = fake.add_user("demo.admin", password="their-own-password")
    assert run_main(fake, out_dir) == 2
    assert fake.users[uid]["password"] == "their-own-password"
    assert fake.mutations() == []


def test_non_superuser_admin_refused(out_dir):
    fake = FakePaperless()
    fake.users[fake.admin_id]["is_superuser"] = False
    assert run_main(fake, out_dir) == 2
    assert fake.mutations() == []


def test_dry_run_makes_no_changes(fake, out_dir, manifest):
    seeder = make_seeder(fake, out_dir, manifest, options=S.Options(dry_run=True), state=False)
    seeder.run()
    assert fake.mutations() == []
    assert any("would upload" in line for line in seeder.lines)
    assert not (out_dir / S.STATE_FILE_NAME).exists()


# --------------------------------------------------------------------------- idempotency


def test_fresh_seed_creates_expected_state(fake, out_dir, manifest):
    make_seeder(fake, out_dir, manifest).run()
    assert sorted(group_ids(fake)) == sorted(S.GROUPS)
    usernames = {u["username"] for u in fake.users.values()}
    assert usernames == {"admin", "demo.manager", "demo.admin", "demo.carer"}  # test.user is optional
    assert_demo_state(fake, manifest)
    assert len(fake.uploads) == len(manifest)
    assert not json.loads((out_dir / S.STATE_FILE_NAME).read_text()).get(API)


def test_rerun_changes_nothing_and_uploads_nothing(fake, out_dir, manifest):
    make_seeder(fake, out_dir, manifest).run()
    before = len(fake.requests)
    summary = make_seeder(fake, out_dir, manifest).run()
    second_run = fake.requests[before:]
    assert [r for r in second_run if r[0] != "GET"] == []
    assert len(fake.uploads) == len(manifest)
    assert len(summary.unchanged) == len(manifest)
    assert_demo_state(fake, manifest)


def test_rerun_keeps_changed_passwords_unless_asked(fake, out_dir, manifest):
    make_seeder(fake, out_dir, manifest).run()
    uid = next(u["id"] for u in fake.users.values() if u["username"] == "demo.carer")
    fake.users[uid]["password"] = "changed-by-the-user"
    make_seeder(fake, out_dir, manifest).run()
    assert fake.users[uid]["password"] == "changed-by-the-user"
    make_seeder(fake, out_dir, manifest, options=S.Options(reset_passwords=True)).run()
    assert fake.users[uid]["password"] == ENV["DEMO_CARER_PASSWORD"]


def test_rerun_repairs_drifted_groups_and_permissions(fake, out_dir, manifest):
    make_seeder(fake, out_dir, manifest).run()
    gids = group_ids(fake)
    fake.groups[gids["All Staff"]]["permissions"].append("delete_document")
    budget = fake.doc_by_key(next(m["key"] for m in manifest if m["access"] == "finance_only"))[0]
    budget["perms"]["view"]["groups"].append(gids["All Staff"])
    budget["perms"]["view"]["users"].append(999)
    make_seeder(fake, out_dir, manifest).run()
    assert "delete_document" not in fake.groups[gids["All Staff"]]["permissions"]
    assert_demo_state(fake, manifest)


# --------------------------------------------------------------------------- interruption and recovery


def test_interrupt_between_upload_and_permissions_is_finished_on_rerun(fake, out_dir, manifest):
    state = {"armed": True}

    def die_on_first_permission_patch(request, method, path):
        if state["armed"] and method == "PATCH" and path.startswith("/api/documents/"):
            state["armed"] = False
            raise Interrupt("process killed")

    fake.interceptors.append(die_on_first_permission_patch)
    with pytest.raises(Interrupt):
        make_seeder(fake, out_dir, manifest).run()
    assert len(fake.documents) == 1
    stranded = next(iter(fake.documents.values()))
    # Upstream assigns the uploader as owner and no grants: visible to the admin only.
    assert stranded["owner"] == fake.admin_id
    assert stranded["perms"]["view"]["groups"] == []

    make_seeder(fake, out_dir, manifest).run()
    assert len(fake.uploads) == len(manifest)  # the stranded document was not uploaded again
    assert_demo_state(fake, manifest)


def test_interrupt_while_task_pending_waits_instead_of_reuploading(fake, out_dir, manifest):
    state = {"armed": True}

    def die_on_first_task_poll(request, method, path):
        if state["armed"] and path == "/api/tasks/":
            state["armed"] = False
            raise Interrupt("process killed")

    fake.interceptors.append(die_on_first_task_poll)
    with pytest.raises(Interrupt):
        make_seeder(fake, out_dir, manifest).run()
    pending = json.loads((out_dir / S.STATE_FILE_NAME).read_text())[API]
    assert len(pending) == 1 and len(fake.documents) == 0  # still being consumed

    seeder = make_seeder(fake, out_dir, manifest)
    seeder.run()
    assert len(fake.uploads) == len(manifest)
    assert any("waiting for earlier upload" in line for line in seeder.lines)
    assert_demo_state(fake, manifest)


def test_consumption_finished_between_runs_is_found_by_key(fake, out_dir, manifest):
    state = {"armed": True}

    def die_on_first_task_poll(request, method, path):
        if state["armed"] and path == "/api/tasks/":
            state["armed"] = False
            raise Interrupt("process killed")

    fake.interceptors.append(die_on_first_task_poll)
    with pytest.raises(Interrupt):
        make_seeder(fake, out_dir, manifest).run()
    fake.finish_pending_tasks()
    make_seeder(fake, out_dir, manifest).run()
    assert len(fake.uploads) == len(manifest)
    assert_demo_state(fake, manifest)


def test_pending_task_that_failed_is_uploaded_again(fake, out_dir, manifest):
    state = {"armed": True}

    def die_on_first_task_poll(request, method, path):
        if state["armed"] and path == "/api/tasks/":
            state["armed"] = False
            raise Interrupt("process killed")

    fake.interceptors.append(die_on_first_task_poll)
    with pytest.raises(Interrupt):
        make_seeder(fake, out_dir, manifest).run()
    for task in fake.tasks.values():
        task["status"] = "FAILURE"
    seeder = make_seeder(fake, out_dir, manifest)
    seeder.run()
    assert len(fake.uploads) == len(manifest) + 1
    assert any("uploading again" in line for line in seeder.lines)
    assert_demo_state(fake, manifest)


def test_rerun_after_http_failure_resumes(fake, out_dir, manifest, capsys):
    calls = {"n": 0}

    def fail_tenth_upload(request, method, path):
        if path == "/api/documents/post_document/":
            calls["n"] += 1
            if calls["n"] == 10:
                return FakePaperless.response(request, 500, "<html>Server Error</html>", "text/html")

    fake.interceptors.append(fail_tenth_upload)
    assert run_main(fake, out_dir) == 1
    assert "HTTP 500" in capsys.readouterr().err
    assert len(fake.documents) == 9
    assert run_main(fake, out_dir) == 0
    assert_demo_state(fake, manifest)


# --------------------------------------------------------------------------- legacy instances and conflicts


def seed_legacy(fake: FakePaperless, manifest: list[dict], titles: list[str]) -> None:
    """State left by the pre-import-key seeder: same groups/users, title-only documents."""
    for name, perms in S.GROUPS.items():
        fake.add_group(name, perms)
    for user in S.USERS[:3]:
        fake.add_user(user.username, password="old")
    for title in titles:
        fake.add_document(title, owner=fake.admin_id)


def test_legacy_instance_requires_adopt_flag(fake, out_dir, manifest):
    seed_legacy(fake, manifest, [manifest[0]["title"]])
    assert run_main(fake, out_dir) == 2
    assert fake.mutations() == []


def test_adopting_legacy_instance_keys_documents_without_duplicates(fake, out_dir, manifest):
    titles = [manifest[0]["title"], manifest[5]["title"]]
    seed_legacy(fake, manifest, titles)
    summary = make_seeder(fake, out_dir, manifest, options=S.Options(adopt_existing=True)).run()
    assert len(summary.adopted) == 2
    assert len(fake.uploads) == len(manifest) - 2
    assert_demo_state(fake, manifest)
    old_pw = [u["password"] for u in fake.users.values() if u["username"].startswith("demo.")]
    assert old_pw == ["old", "old", "old"]


def test_duplicate_legacy_titles_are_a_conflict_and_nothing_is_deleted(fake, out_dir, manifest):
    title = manifest[0]["title"]
    seed_legacy(fake, manifest, [title, title])
    with pytest.raises(S.ImportConflict):
        make_seeder(fake, out_dir, manifest, options=S.Options(adopt_existing=True)).run()
    assert len([d for d in fake.documents.values() if d["title"] == title]) == 2


def test_duplicate_import_keys_are_a_conflict(fake, out_dir, manifest):
    make_seeder(fake, out_dir, manifest).run()
    fid = key_field_id(fake)
    fake.add_document("copy", owner=fake.admin_id, custom_fields=[{"field": fid, "value": manifest[3]["key"]}])
    with pytest.raises(S.ImportConflict):
        make_seeder(fake, out_dir, manifest).run()


def test_unkeyed_same_title_document_on_seeded_instance_is_left_alone(fake, out_dir, manifest):
    make_seeder(fake, out_dir, manifest[:1]).run()
    other = fake.add_document(manifest[1]["title"], owner=None, view_groups=[])
    seeder = make_seeder(fake, out_dir, manifest)
    seeder.run()
    assert fake.documents[other]["owner"] is None and fake.documents[other]["custom_fields"] == []
    assert any(line.startswith("skip (exists without import key") for line in seeder.lines)
    assert fake.doc_by_key(manifest[1]["key"]) == []


def test_title_is_not_identity_for_keyed_documents(fake, out_dir, manifest):
    make_seeder(fake, out_dir, manifest).run()
    doc = fake.doc_by_key(manifest[0]["key"])[0]
    doc["title"] = "Renamed by a user"
    make_seeder(fake, out_dir, manifest).run()
    assert len(fake.uploads) == len(manifest)


# --------------------------------------------------------------------------- HTTP failure modes


def client_for(fake=None, api=API, clock=None, **settings_overrides):
    settings = S.Settings(api_root=api, admin_user="admin", admin_password="pw", **settings_overrides)
    clock = clock or FakeClock()
    session = make_session(fake, api) if fake is not None else requests.Session()
    return S.PaperlessClient(settings, session=session, sleep=clock.sleep, clock=clock), clock


def test_stalled_server_request_is_bounded(stalled_server):
    client, _ = client_for(api=stalled_server, read_timeout=0.3, connect_timeout=0.3)
    start = time.monotonic()
    with pytest.raises(S.ApiError, match="no response within"):
        client.request("GET", "groups/")
    assert time.monotonic() - start < 3  # 3 attempts x 0.3 s, fake sleeps


def test_stalled_server_upload_is_bounded(stalled_server):
    client, _ = client_for(api=stalled_server, read_timeout=0.3, connect_timeout=0.3)
    start = time.monotonic()
    with pytest.raises(S.ApiError, match="no response within 0.3s"):
        client.request("POST", "documents/post_document/", data={"title": "x"})
    assert time.monotonic() - start < 2


def test_readiness_wait_against_stalled_server_is_bounded(stalled_server):
    settings = S.Settings(api_root=stalled_server, admin_user="a", admin_password="b", read_timeout=0.2,
                          connect_timeout=0.2, poll_interval=0.05)
    client = S.PaperlessClient(settings)
    start = time.monotonic()
    with pytest.raises(S.ApiError, match="not ready after 0.5s"):
        client.wait_until_ready(0.5)
    assert time.monotonic() - start < 3


def test_bad_credentials_fail_fast(fake):
    fake.interceptors.append(lambda req, m, p: FakePaperless.response(req, 401, {"detail": "Invalid username/password."}))
    client, clock = client_for(fake)
    with pytest.raises(S.ApiError, match="rejected the admin credentials"):
        client.wait_until_ready(300)
    assert len(fake.requests) == 1 and clock.now == 0


def test_readiness_retries_until_deadline(fake):
    fake.interceptors.append(lambda req, m, p: FakePaperless.response(req, 503, "starting", "text/plain"))
    client, clock = client_for(fake)
    with pytest.raises(S.ApiError, match="not ready after 30s"):
        client.wait_until_ready(30)
    assert 30 <= clock.now <= 36


def test_transient_503_on_get_is_retried(fake):
    calls = {"n": 0}

    def flaky(req, method, path):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakePaperless.response(req, 503, "busy", "text/plain")

    fake.interceptors.append(flaky)
    client, _ = client_for(fake)
    assert client.list_all("groups/") == []


def test_post_is_not_retried(fake):
    fake.interceptors.append(lambda req, m, p: FakePaperless.response(req, 503, "busy", "text/plain"))
    client, _ = client_for(fake)
    with pytest.raises(S.ApiError, match="HTTP 503"):
        client.request("POST", "groups/", json_body={"name": "x"}, expect=(201,))
    assert len(fake.requests) == 1


def test_html_instead_of_json_is_reported(fake):
    fake.interceptors.append(lambda req, m, p: FakePaperless.response(req, 200, "<html>login</html>", "text/html"))
    client, _ = client_for(fake)
    with pytest.raises(S.ApiError, match="expected JSON, got text/html"):
        client.list_all("groups/")


def test_list_without_results_is_malformed(fake):
    fake.interceptors.append(lambda req, m, p: FakePaperless.response(req, 200, {"detail": "nope"}))
    client, _ = client_for(fake)
    with pytest.raises(S.ApiError, match="malformed list response"):
        client.list_all("groups/")


def test_redirects_are_not_followed(fake):
    def redirect(req, m, p):
        resp = FakePaperless.response(req, 302, "")
        resp.headers["Location"] = "https://login.example.com/"
        return resp

    fake.interceptors.append(redirect)
    client, _ = client_for(fake)
    with pytest.raises(S.ApiError, match="unexpected redirect"):
        client.request("GET", "groups/")
    assert len(fake.requests) == 1


def test_pagination_link_to_another_origin_is_refused(fake):
    fake.interceptors.append(lambda req, m, p: FakePaperless.response(
        req, 200, {"results": [], "next": "https://elsewhere.example.com/api/groups/?page=2"}))
    client, _ = client_for(fake)
    with pytest.raises(S.ApiError, match="different origin"):
        client.list_all("groups/")


@pytest.mark.parametrize("payload,error", [
    ({"detail": "Authentication credentials were not provided."}, S.ApiError),
    ([{"task_id": "t1"}], S.ApiError),
    ([{"task_id": "t1", "status": "FAILURE", "result": "parser error"}], S.TaskFailed),
    ([{"task_id": "t1", "status": "REVOKED", "result": None}], S.TaskFailed),
    ([{"task_id": "t1", "status": "SUCCESS", "related_document": None, "result": "dup"}], S.TaskFailed),
])
def test_task_poll_failure_modes(fake, payload, error):
    fake.interceptors.append(lambda req, m, p: FakePaperless.response(req, 200, payload))
    client, _ = client_for(fake)
    with pytest.raises(error):
        client.wait_for_task("t1", 60)


def test_task_poll_http_error_is_reported(fake):
    fake.interceptors.append(lambda req, m, p: FakePaperless.response(req, 500, "boom", "text/plain"))
    client, _ = client_for(fake)
    with pytest.raises(S.ApiError, match="HTTP 500"):
        client.wait_for_task("t1", 60)


def test_task_that_never_finishes_times_out(fake):
    fake.interceptors.append(lambda req, m, p: FakePaperless.response(
        req, 200, [{"task_id": "t1", "status": "STARTED"}]))
    client, clock = client_for(fake, poll_interval=2.0)
    with pytest.raises(S.TaskTimeout, match="last status: STARTED"):
        client.wait_for_task("t1", 20)
    assert clock.now == pytest.approx(20)
    assert len(fake.requests) == 11


def test_task_not_yet_visible_keeps_polling_then_succeeds(fake):
    calls = {"n": 0}

    def appear_later(req, m, p):
        calls["n"] += 1
        tasks = [] if calls["n"] < 3 else [{"task_id": "t1", "status": "SUCCESS", "related_document": 7}]
        return FakePaperless.response(req, 200, tasks)

    fake.interceptors.append(appear_later)
    client, _ = client_for(fake)
    assert client.wait_for_task("t1", 60) == 7


def test_upload_failure_task_is_reported_and_not_left_pending(fake, out_dir, manifest):
    fake.task_outcome = "failure"
    with pytest.raises(S.TaskFailed, match="parser error"):
        make_seeder(fake, out_dir, manifest).run()
    assert not json.loads((out_dir / S.STATE_FILE_NAME).read_text()).get(API)


def test_permissions_that_do_not_apply_are_detected(fake, out_dir, manifest):
    def ignore_document_patch(req, method, path):
        if method == "PATCH" and path.startswith("/api/documents/"):
            return FakePaperless.response(req, 200, {"id": int(path.split("/")[3])})

    fake.interceptors.append(ignore_document_patch)
    with pytest.raises(S.ApiError, match="did not take effect"):
        make_seeder(fake, out_dir, manifest).run()


def test_main_reports_runtime_errors_without_traceback(fake, out_dir, capsys):
    fake.interceptors.append(lambda req, m, p: FakePaperless.response(req, 500, "boom", "text/plain")
                             if m == "POST" and p == "/api/groups/" else None)
    assert run_main(fake, out_dir) == 1
    err = capsys.readouterr().err
    assert "HTTP 500" in err and "Traceback" not in err


# --------------------------------------------------------------------------- demo guide


def guide_view(fake: FakePaperless) -> dict:
    views = [v for v in fake.saved_views.values() if v["name"] == S.GUIDE_VIEW]
    assert len(views) == 1
    return views[0]


def assert_guide_pinned_first(fake: FakePaperless, username: str, view_id: int) -> dict:
    stored = fake.ui_settings[fake.user_id(username)]["saved_views"]
    for visible_key, order_key in S.PIN_LISTS:
        assert view_id in stored[visible_key], (username, visible_key)
        assert stored[order_key][0] == view_id, (username, order_key)
    return stored


def test_guide_document_is_readable_by_every_group_and_editable_by_none(fake, out_dir, manifest):
    make_seeder(fake, out_dir, manifest).run()
    guide = [m for m in manifest if S.GUIDE_TAG in m["tags"]]
    assert len(guide) == 1 and guide[0]["access"] == "guide"
    doc = fake.doc_by_key(guide[0]["key"])[0]
    gids = group_ids(fake)
    assert doc["perms"]["view"]["groups"] == sorted(gids[g] for g in S.GROUPS)
    assert doc["perms"]["change"] == {"users": [], "groups": []}


def test_guide_is_pinned_first_for_every_account(fake, out_dir, manifest):
    summary = make_seeder(fake, out_dir, manifest, env={"DEMO_TEST_PASSWORD": "tester-pw"}).run()
    view = guide_view(fake)
    tag_id = next(t["id"] for t in fake.objects["tags"].values() if t["name"] == S.GUIDE_TAG)
    assert view["filter_rules"] == [{"rule_type": S.HAS_TAG_RULE, "value": str(tag_id)}]
    assert view["owner"] == fake.admin_id
    assert view["perms"]["view"]["groups"] == sorted(group_ids(fake)[g] for g in S.GROUPS)
    assert view["perms"]["change"]["groups"] == []
    accounts = ["admin", "demo.manager", "demo.admin", "demo.carer", "test.user"]
    for username in accounts:
        assert_guide_pinned_first(fake, username, view["id"])
    assert sorted(summary.guide_pinned) == sorted(accounts)


def test_guide_is_pinned_again_after_a_user_unpins_it(fake, out_dir, manifest):
    make_seeder(fake, out_dir, manifest).run()
    view_id = guide_view(fake)["id"]
    carer = fake.user_id("demo.carer")
    fake.ui_settings[carer] = {"dark_mode": {"enabled": True},
                               "saved_views": {"dashboard_views_visible_ids": [555],
                                               "dashboard_views_sort_order": [555],
                                               "sidebar_views_visible_ids": [],
                                               "sidebar_views_sort_order": []}}
    make_seeder(fake, out_dir, manifest).run()
    stored = fake.ui_settings[carer]
    assert stored["dark_mode"] == {"enabled": True}  # the user's own settings survive
    pins = assert_guide_pinned_first(fake, "demo.carer", view_id)
    assert pins["dashboard_views_visible_ids"] == [555, view_id]
    assert pins["dashboard_views_sort_order"] == [view_id, 555]


def test_pinning_does_not_store_server_supplied_settings(fake, out_dir, manifest):
    make_seeder(fake, out_dir, manifest).run()
    for stored in fake.ui_settings.values():
        assert not set(stored) & S.SERVER_UI_KEYS
        assert "backend_setting" not in stored.get("update_checking", {})


def test_deleted_or_drifted_guide_view_is_restored(fake, out_dir, manifest):
    make_seeder(fake, out_dir, manifest).run()
    view = guide_view(fake)
    view["filter_rules"] = []
    view["perms"]["view"]["groups"] = []
    make_seeder(fake, out_dir, manifest).run()
    assert guide_view(fake)["filter_rules"] != []
    assert guide_view(fake)["perms"]["view"]["groups"] == sorted(group_ids(fake)[g] for g in S.GROUPS)
    del fake.saved_views[view["id"]]
    make_seeder(fake, out_dir, manifest).run()
    new_id = guide_view(fake)["id"]
    assert new_id != view["id"]
    for username in ("admin", "demo.manager", "demo.admin", "demo.carer"):
        assert_guide_pinned_first(fake, username, new_id)


def test_account_with_changed_password_is_reported_not_fatal(fake, out_dir, manifest):
    make_seeder(fake, out_dir, manifest).run()
    fake.users[fake.user_id("demo.carer")]["password"] = "changed-by-the-user"
    fake.ui_settings.pop(fake.user_id("demo.carer"))
    seeder = make_seeder(fake, out_dir, manifest)
    summary = seeder.run()
    assert summary.guide_not_pinned == ["demo.carer"]
    assert any("guide not pinned for 'demo.carer'" in line for line in seeder.lines)
    assert fake.user_id("demo.carer") not in fake.ui_settings


def test_dry_run_reports_guide_pinning_without_writing(fake, out_dir, manifest):
    make_seeder(fake, out_dir, manifest).run()
    fake.ui_settings.clear()
    before = len(fake.mutations())
    seeder = make_seeder(fake, out_dir, manifest, options=S.Options(dry_run=True), state=False)
    seeder.run()
    assert len(fake.mutations()) == before
    assert sum("would pin" in line for line in seeder.lines) == 4


def test_pin_first_keeps_other_views_and_is_idempotent():
    once = S.pin_first({"dashboard_views_visible_ids": [3, 1], "dashboard_views_sort_order": [1]}, 7)
    assert once["dashboard_views_visible_ids"] == [3, 1, 7]
    assert once["dashboard_views_sort_order"] == [7, 1, 3]
    assert once["sidebar_views_visible_ids"] == [7] and once["sidebar_views_sort_order"] == [7]
    assert S.pin_first(once, 7) == once


def test_guide_text_holds_no_credentials_or_answers():
    import make_docs
    guide = next(d for d in make_docs.DOCS if make_docs.GUIDE_TAG in d["tags"])
    text = json.dumps(guide)
    assert "Bluegum-" not in text  # live demo password prefix
    for spoiler in ("GST", "383", "K. Lee", "front car park", "3 February"):
        assert spoiler not in text, spoiler
