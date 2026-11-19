"""Load build.py and serve.py as modules (they are scripts, not a package) and give
tests a tiny backlog builder. Standard library + pytest only."""
import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def build():
    return _load("backlog_build", "skills/backlog/build.py")


@pytest.fixture(scope="session")
def serve():
    return _load("backlog_serve", "server/serve.py")


@pytest.fixture
def backlog(tmp_path):
    d = tmp_path / "backlog"
    (d / "tasks").mkdir(parents=True)
    (d / "epics").mkdir()
    return d


@pytest.fixture
def put():
    """put(backlog, "tasks", "T-001-x.md", id=..., status=..., body=...) writes one file."""
    def write(d, kind, name, **fields):
        body = fields.pop("body", "")
        front = "\n".join(f"{k}: {v}" for k, v in fields.items())
        (d / kind / name).write_text(f"---\n{front}\n---\n{body}")
    return write


@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    """git identity via env so commits work on CI machines with no global config."""
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}.items():
        monkeypatch.setenv(k, v)

    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    git("init", "-q")
    return git
