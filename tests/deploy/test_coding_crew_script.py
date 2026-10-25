"""Tests for deploy/coding-crew/crew-run.sh, run inside the coding crew container.

It runs on the host with fake ``gh`` and ``claude`` executables and a real local git
``origin``, so branch detection, the bundle and the disabled push are exercised for
real.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "coding-crew" / "crew-run.sh"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "coding_crew"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}

pytestmark = pytest.mark.skipif(
    not shutil.which("bash") or not shutil.which("jq") or not shutil.which("git"),
    reason="needs bash, jq and git",
)


def _executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_ENV},
    ).stdout.strip()


@pytest.fixture()
def sandbox(tmp_path: Path):
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "init", "-q", "-b", "main", str(seed))
    (seed / "README.md").write_text("app\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "init")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "main")
    _git(seed, "push", "-q", "origin", "main:agent/old-work")  # an earlier run's branch

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir / "gh", (FIXTURES / "fake_gh.sh").read_text())
    _executable(bin_dir / "claude", (FIXTURES / "fake_claude.sh").read_text())
    log = tmp_path / "calls.log"
    env = {
        **os.environ,
        **GIT_ENV,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_LOG": str(log),
        "CREW_REPO": "acme/app",
        "CREW_ISSUE": "12",
        "CREW_PROMPT": "/cuadrilla:ticket #12",
        "CREW_BUDGET_USD": "5",
        "CREW_BRANCH_PREFIX": "agent/",
        "CREW_CLONE_URL": str(origin),
        "ANTHROPIC_API_KEY": "fake-key",
    }
    env.pop("GH_TOKEN", None)
    return env, log, origin


def _run(env: dict, **overrides: str):
    """One container run: empty work folder and an empty, writable output folder."""
    work = tempfile.mkdtemp(prefix="crew-work-")
    out = Path(tempfile.mkdtemp(prefix="crew-out-"))
    try:
        proc = subprocess.run(
            ["bash", str(SCRIPT)],
            env={**env, "CREW_WORKDIR": work, "CREW_OUT": str(out), **overrides},
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        lines = [
            line for line in proc.stdout.splitlines() if line.startswith("CREW_RESULT ")
        ]
        assert len(lines) == 1, proc.stdout
        files = {
            p.name: (p.read_bytes() if p.suffix == ".bundle" else p.read_text())
            for p in out.iterdir()
        }
        bundle_heads = ""
        if (out / "changes.bundle").exists():
            bundle_heads = _git(out, "bundle", "list-heads", "changes.bundle")
        return json.loads(lines[0][len("CREW_RESULT ") :]), files, bundle_heads
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)


def test_bundles_the_agent_branch_and_cannot_push(sandbox):
    env, log, origin = sandbox
    result, files, heads = _run(env, FAKE_MODE="commit")
    assert result["branches"] == ["agent/12-fix-login"]
    assert result["committed_by_runner"] is False
    assert len(result["base_sha"]) == 40
    assert result["claude"]["total_cost_usd"] == 1.25
    assert "refs/heads/agent/12-fix-login" in heads
    assert files["pr-body.md"].startswith("## Summary")
    assert "PUSHED" not in log.read_text(), "the container must not be able to push"
    remote = _git(origin, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    assert "agent/12-fix-login" not in remote


def test_hands_only_the_read_token_to_the_agent(sandbox):
    env, log, _ = sandbox
    _run(env, FAKE_MODE="nochange", GH_TOKEN="read-only-token")
    assert "GH_TOKEN=read-only-token" in log.read_text()
    assert "gh auth setup-git" in log.read_text()


def test_tells_the_agent_how_to_finish_with_headless_flags(sandbox):
    env, log, _ = sandbox
    _run(env, FAKE_MODE="nochange", CREW_MODEL="sonnet")
    call = next(
        line for line in log.read_text().splitlines() if line.startswith("claude ")
    )
    assert ":: /cuadrilla:ticket #12 ::" in call
    for flag in (
        "-p",
        "--output-format json",
        "--permission-mode dontAsk",
        "--max-budget-usd 5",
        "--model sonnet",
        "--append-system-prompt",
        "can't push or open pull requests",
        "pr-body.md",
        "agent/12-",
    ):
        assert flag in call, flag
    assert "dangerously" not in call and "bypassPermissions" not in call


@pytest.mark.parametrize("mode", ["uncommitted", "onbase"])
def test_work_left_on_the_base_branch_goes_to_an_agent_branch(sandbox, mode):
    env, _, _ = sandbox
    result, files, heads = _run(env, FAKE_MODE=mode)
    assert result["branches"] == ["agent/12-crew"]
    assert result["committed_by_runner"] is True
    assert "refs/heads/agent/12-crew" in heads


@pytest.mark.parametrize("mode", ["nochange", "outside"])
def test_no_bundle_without_new_agent_commits(sandbox, mode):
    env, _, _ = sandbox
    result, files, _ = _run(env, FAKE_MODE=mode)
    assert result["branches"] == []
    assert "changes.bundle" not in files


def test_reports_agent_errors(sandbox):
    env, _, _ = sandbox
    budget, _, _ = _run(env, FAKE_MODE="budget")
    assert budget["claude"]["is_error"] is True
    assert budget["claude"]["errors"] == ["Reached maximum budget ($5)"]
    garbage, _, _ = _run(env, FAKE_MODE="garbage")
    assert garbage["claude"]["is_error"] is True


@pytest.mark.parametrize(
    ("override", "error", "detail"),
    [
        ({"ANTHROPIC_API_KEY": ""}, "config", "no Claude credentials"),
        ({"CREW_REPO": ""}, "config", "missing CREW_REPO"),
        ({"CREW_OUT": "/nonexistent/out"}, "config", "not writable"),
        (
            {"CREW_CLONE_URL": "/nonexistent/repo.git"},
            "clone-failed",
            "could not clone",
        ),
    ],
)
def test_fails_fast_with_a_category(sandbox, override, error, detail):
    env, _, _ = sandbox
    work = tempfile.mkdtemp()
    out = tempfile.mkdtemp()
    try:
        proc = subprocess.run(
            ["bash", str(SCRIPT)],
            env={
                **env,
                "FAKE_MODE": "commit",
                "CREW_WORKDIR": work,
                "CREW_OUT": out,
                **override,
            },
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)
    result = json.loads(proc.stdout.strip().splitlines()[-1][len("CREW_RESULT ") :])
    assert result["error"] == error
    assert detail in result["detail"]
