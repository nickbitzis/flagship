import copy
import socket
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient

from flagship.app import create_app
from flagship.store import Store

BOOL = [{"value": True, "name": "on"}, {"value": False, "name": "off"}]


class Clock:
    def __init__(self, t=1_780_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def bool_flag(key="f", prod=None, tags=None, **extra):
    env = {"on": True, "off_variation": 1, "rules": [], "fallthrough": {"variation": 0}}
    flag = {
        "key": key,
        "kind": "boolean",
        "variations": copy.deepcopy(BOOL),
        "tags": tags or [],
        "environments": {"dev": copy.deepcopy(env), "staging": copy.deepcopy(env), "prod": prod or copy.deepcopy(env)},
    }
    flag.update(extra)
    return flag


def ruleset(*flags, segments=()):
    return {
        "env": "prod",
        "version": 1,
        "flags": {f["key"]: f for f in flags},
        "segments": {s["key"]: s for s in segments},
    }


def flat(key="f", **cfg):
    """A flag already flattened for one environment, for pure engine tests."""
    base = {
        "key": key,
        "variations": copy.deepcopy(BOOL),
        "salt": "s1",
        "on": True,
        "off_variation": 1,
        "rules": [],
        "fallthrough": {"variation": 0},
    }
    base.update(cfg)
    return base


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(clock):
    return Store(":memory:", clock=clock)


@pytest.fixture
def client(store):
    app = create_app(store=store, scheduler_interval=0)
    with TestClient(app) as c:
        yield c


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LiveServer:
    """A real uvicorn server on a thread, so the SDK talks HTTP and SSE for real and we can kill it."""

    def __init__(self, store, port=None):
        self.store = store
        self.port = port or free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.server = None
        self.thread = None

    def start(self):
        app = create_app(store=self.store, scheduler_interval=0)
        cfg = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error", timeout_graceful_shutdown=1)
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.02)
        return self

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture
def live(tmp_path):
    s = Store(str(tmp_path / "live.db"))
    server = LiveServer(s).start()
    yield server
    if server.thread.is_alive():
        server.stop()


def wait_for(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False
