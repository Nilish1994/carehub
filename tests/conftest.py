from __future__ import annotations

import json
import shutil
import socket
import sys
import threading
from pathlib import Path

import pytest
import requests
from requests.adapters import BaseAdapter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "seed"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import make_docs  # noqa: E402
import setup_demo  # noqa: E402
from fake_paperless import FakePaperless  # noqa: E402

API = "http://127.0.0.1:8040"
ENV = {
    "PAPERLESS_ADMIN_USER": "admin",
    "PAPERLESS_ADMIN_PASSWORD": "admin-test-password",
    "DEMO_MANAGER_PASSWORD": "manager-test-password",
    "DEMO_ADMIN_PASSWORD": "admin-officer-test-password",
    "DEMO_CARER_PASSWORD": "carer-test-password",
}


class DenyAdapter(BaseAdapter):
    """Fails any request that is not routed to a fake, so tests never touch a real host."""

    def send(self, request, **kwargs):
        raise AssertionError(f"unexpected request to {request.url}")

    def close(self):
        pass


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture(scope="session")
def demo_docs(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("demo-docs")
    make_docs.main([str(out)])
    return out


@pytest.fixture
def out_dir(demo_docs, tmp_path) -> Path:
    out = tmp_path / "out"
    shutil.copytree(demo_docs, out)
    return out


@pytest.fixture
def manifest(out_dir) -> list[dict]:
    return json.loads((out_dir / "manifest.json").read_text())


@pytest.fixture
def fake() -> FakePaperless:
    return FakePaperless()


def make_session(fake: FakePaperless, api: str = API) -> requests.Session:
    session = requests.Session()
    session.mount("http://", DenyAdapter())
    session.mount("https://", DenyAdapter())
    session.mount(api + "/", fake)
    return session


def make_seeder(fake, out_dir, manifest, *, options=None, env=None, state=True, clock=None, log=None,
                api: str = API):
    settings = setup_demo.load_settings({**ENV, "CAREHUB_API": api, **(env or {})})
    clock = clock or FakeClock()
    client = setup_demo.PaperlessClient(settings, session=make_session(fake, api), sleep=clock.sleep, clock=clock)
    state_obj = setup_demo.ImportState(out_dir / setup_demo.STATE_FILE_NAME if state else None, settings.api_root)
    lines: list[str] = [] if log is None else log
    seeder = setup_demo.Seeder(client, settings, manifest, out_dir, options or setup_demo.Options(), state_obj,
                               log=lines.append)
    seeder.lines = lines
    return seeder


@pytest.fixture
def stalled_server():
    """A TCP server that accepts connections and never answers."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    held: list[socket.socket] = []
    stop = threading.Event()

    def accept():
        srv.settimeout(0.1)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
                held.append(conn)
            except OSError:
                continue

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.getsockname()[1]}"
    stop.set()
    thread.join(timeout=2)
    for conn in held:
        conn.close()
    srv.close()
