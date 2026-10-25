"""Tests for the coding crew tools.

``gh`` is a stateful fake. ``docker`` is a fake that runs the real container script
(deploy/coding-crew/crew-run.sh) with a fake ``claude``, against a real local
``origin``, so a run is exercised end to end: container, bundle, import on the host,
push, draft PR.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from openjarvis.core.config import CodingCrewConfig
from openjarvis.tools import coding_crew
from openjarvis.tools.coding_crew import (
    CodingCrewQueueTool,
    CodingCrewRunTool,
    load_settings,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "coding_crew"
SCRIPT = ROOT / "deploy" / "coding-crew" / "crew-run.sh"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}
READ_TOKEN = "ghr_read_only_value"
WRITE_TOKEN = "ghp_write_secret_value"
API_KEY = "sk-ant-secret-value"


def _script(path: Path, body: str, interpreter: str = sys.executable) -> str:
    path.write_text(f"#!{interpreter}\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


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
def crew(tmp_path, monkeypatch):
    if not (shutil.which("bash") and shutil.which("jq") and shutil.which("git")):
        pytest.skip("needs bash, jq and git")
    state = {
        "issues": {
            "acme/app": {
                "12": {
                    "state": "OPEN",
                    "title": "Login fails",
                    "createdAt": "2026-10-20T10:00:00Z",
                    "labels": [{"name": "agent-ready"}],
                },
                "13": {
                    "state": "OPEN",
                    "title": "Already taken",
                    "createdAt": "2026-10-19T10:00:00Z",
                    "labels": [{"name": "agent-ready"}, {"name": "agent-working"}],
                },
                "14": {
                    "state": "OPEN",
                    "title": "Not ready",
                    "createdAt": "2026-10-18T10:00:00Z",
                    "labels": [{"name": "bug"}],
                },
                "15": {
                    "state": "CLOSED",
                    "title": "Closed",
                    "createdAt": "2026-10-17T10:00:00Z",
                    "labels": [{"name": "agent-ready"}],
                },
                "16": {
                    "state": "OPEN",
                    "title": "Labelled by a stranger",
                    "createdAt": "2026-10-21T10:00:00Z",
                    "labels": [{"name": "agent-ready"}],
                },
            },
            "acme/api": {
                "3": {
                    "state": "OPEN",
                    "title": "Older one",
                    "createdAt": "2026-10-01T10:00:00Z",
                    "labels": [{"name": "agent-ready"}],
                },
                "4": {
                    "state": "OPEN",
                    "title": "Failed before",
                    "createdAt": "2026-10-02T10:00:00Z",
                    "labels": [{"name": "agent-ready"}, {"name": "agent-failed"}],
                },
            },
            "other/repo": {
                "1": {
                    "state": "OPEN",
                    "title": "x",
                    "createdAt": "2026-10-01T10:00:00Z",
                    "labels": [{"name": "agent-ready"}],
                }
            },
        },
        "labeled_by": {
            "acme/app#12": [["triager", "bug"], ["maintainer", "agent-ready"]],
            "acme/app#16": [["maintainer", "bug"], ["stranger", "agent-ready"]],
        },
        "permissions": {"maintainer": "write", "stranger": "triage"},
        "broken": [],
    }
    state_file = tmp_path / "gh-state.json"
    state_file.write_text(json.dumps(state))

    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "init", "-q", "-b", "main", str(seed))
    (seed / "README.md").write_text("app\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "init")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "main")

    container_bin = tmp_path / "container-bin"
    container_bin.mkdir()
    _script(
        container_bin / "gh", (FIXTURES / "fake_gh.sh").read_text(), "/usr/bin/env bash"
    )
    _script(
        container_bin / "claude",
        (FIXTURES / "fake_claude.sh").read_text(),
        "/usr/bin/env bash",
    )

    gh_log, docker_log, container_log = (
        tmp_path / "gh.log",
        tmp_path / "docker.log",
        tmp_path / "container.log",
    )
    for key, value in {
        **GIT_ENV,
        "FAKE_GH_STATE": str(state_file),
        "FAKE_GH_LOG": str(gh_log),
        "FAKE_DOCKER_LOG": str(docker_log),
        "FAKE_CONTAINER_LOG": str(container_log),
        "FAKE_CONTAINER_PATH": f"{container_bin}{os.pathsep}{os.environ['PATH']}",
        "FAKE_CREW_SCRIPT": str(SCRIPT),
        "FAKE_ORIGIN": str(origin),
        "FAKE_MODE": "commit",
        "ANTHROPIC_API_KEY": API_KEY,
        "GH_TOKEN": WRITE_TOKEN,
        "GH_READ_TOKEN": READ_TOKEN,
    }.items():
        monkeypatch.setenv(key, value)

    config = CodingCrewConfig(
        repos="acme/app, acme/api, not a repo",
        gh_bin=_script(tmp_path / "gh", (FIXTURES / "fake_gh.py").read_text()),
        docker_bin=_script(
            tmp_path / "docker", (FIXTURES / "fake_docker.py").read_text()
        ),
        push_url=str(origin),
        timeout_minutes=1,
    )

    class Crew:
        def __init__(self):
            self.config = config
            self.origin = origin

        @staticmethod
        def _lines(path):
            return (
                [json.loads(line) for line in path.read_text().splitlines()]
                if path.exists()
                else []
            )

        def gh_calls(self):
            return self._lines(gh_log)

        def docker_calls(self):
            return self._lines(docker_log)

        def container_log(self):
            return container_log.read_text() if container_log.exists() else ""

        def comments(self):
            return [
                c["stdin"]
                for c in self.gh_calls()
                if c["args"][:2] == ["issue", "comment"]
            ]

        def labels(self, repo, number):
            return [
                lb["name"]
                for lb in json.loads(state_file.read_text())["issues"][repo][
                    str(number)
                ]["labels"]
            ]

        def set_state(self, **values):
            data = json.loads(state_file.read_text())
            data.update(values)
            state_file.write_text(json.dumps(data))

        def remote_branches(self):
            return _git(
                origin, "for-each-ref", "--format=%(refname:short)", "refs/heads/"
            ).split()

        def run(self, ref, **overrides):
            for key, value in overrides.items():
                setattr(self.config, key, value)
            result = CodingCrewRunTool(config=self.config).execute(issue=ref)
            return result, json.loads(result.content)

    return Crew()


class TestSettings:
    def test_parses_lists_and_drops_invalid_values(self):
        settings = load_settings(
            CodingCrewConfig(
                repos="acme/app, ../evil, acme/api",
                env_passthrough="ANTHROPIC_API_KEY,bad name",
                unattended_env="CUADRILLA_UNATTENDED=1,bad name=x",
                branch_prefix="../",
            )
        )
        assert settings.repos == ["acme/app", "acme/api"]
        assert settings.env_passthrough == ["ANTHROPIC_API_KEY"]
        assert settings.unattended_env == {"CUADRILLA_UNATTENDED": "1"}
        assert settings.branch_prefix == "agent/"

    def test_github_write_tokens_never_reach_the_container(self):
        settings = load_settings(
            CodingCrewConfig(
                env_passthrough="ANTHROPIC_API_KEY,GH_TOKEN,GITHUB_TOKEN,GITHUB_PAT",
                unattended_env="GH_TOKEN=x,CUADRILLA_UNATTENDED=1",
            )
        )
        assert settings.env_passthrough == ["ANTHROPIC_API_KEY"]
        assert settings.unattended_env == {"CUADRILLA_UNATTENDED": "1"}

    def test_safe_defaults(self):
        settings = load_settings(CodingCrewConfig())
        assert settings.repos == []
        assert settings.prompt == "/cuadrilla:ticket #{issue}"
        assert settings.image == "openjarvis-coding-crew:local"
        assert settings.trusted_permissions == ["admin", "maintain", "write"]
        assert settings.env_passthrough == ["ANTHROPIC_API_KEY"]
        assert settings.read_token_env == "GH_READ_TOKEN"

    def test_run_tool_timeout_follows_config(self):
        spec = CodingCrewRunTool(
            config=CodingCrewConfig(timeout_minutes=30, budget_usd=4)
        ).spec
        assert spec.timeout_seconds == 30 * 60 + 600
        assert spec.cost_estimate == 4


def test_default_config_template_documents_the_section():
    """The template is an f-string: a stray brace would break `jarvis init`."""
    from openjarvis.core.config import HardwareInfo, generate_default_toml

    toml = generate_default_toml(HardwareInfo())
    assert "# [tools.coding_crew]" in toml
    assert '# prompt = "/cuadrilla:ticket #{issue}"' in toml
    assert '# image = "openjarvis-coding-crew:local"' in toml


class TestQueue:
    def test_lists_ready_unclaimed_issues_oldest_first_across_allowed_repos(self, crew):
        result = CodingCrewQueueTool(config=crew.config).execute()
        data = json.loads(result.content)
        assert result.success
        assert [item["ref"] for item in data["ready"]] == [
            "acme/api#3",
            "acme/app#12",
            "acme/app#16",
        ]
        assert data["errors"] == []
        assert not any(call["args"][3:4] == ["other/repo"] for call in crew.gh_calls())

    def test_reports_repo_errors_without_hiding_the_rest(self, crew):
        crew.set_state(broken=["acme/api"])
        data = json.loads(CodingCrewQueueTool(config=crew.config).execute().content)
        assert [item["ref"] for item in data["ready"]] == ["acme/app#12", "acme/app#16"]
        assert data["errors"] and data["errors"][0].startswith("acme/api")

    def test_needs_configured_repositories(self):
        result = CodingCrewQueueTool(config=CodingCrewConfig()).execute()
        assert not result.success
        assert "repos" in result.content


class TestRefusals:
    @pytest.mark.parametrize(
        ("ref", "outcome"),
        [
            ("acme/app#abc", "invalid"),
            ("acme/app 12", "invalid"),
            ("acme/app#12; rm -rf /", "invalid"),
            ("other/repo#1", "not-allowed"),
            ("acme/app#13", "not-ready"),
            ("acme/app#14", "not-ready"),
            ("acme/app#15", "not-ready"),
            ("acme/api#4", "not-ready"),
        ],
    )
    def test_refuses_without_touching_anything(self, crew, ref, outcome):
        result, data = crew.run(ref)
        assert not result.success
        assert data["outcome"] == outcome
        assert crew.docker_calls() == []
        assert not any(
            call["args"][:2] in (["issue", "edit"], ["issue", "comment"])
            for call in crew.gh_calls()
        )

    def test_only_a_trusted_labeller_starts_a_run(self, crew):
        result, data = crew.run("acme/app#16")
        assert data["outcome"] == "untrusted"
        assert "stranger" in data["message"] and "triage" in data["message"]
        assert crew.docker_calls() == []
        assert crew.labels("acme/app", 16) == ["agent-failed"]
        [comment] = crew.comments()
        assert "write access" in comment

    def test_unknown_labeller_is_not_trusted(self, crew):
        crew.set_state(labeled_by={})
        _, data = crew.run("acme/app#12")
        assert data["outcome"] == "untrusted"
        assert crew.docker_calls() == []

    def test_needs_the_container_credentials(self, crew, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY")
        _, data = crew.run("acme/app#12")
        assert data["outcome"] == "not-configured"
        assert "ANTHROPIC_API_KEY" in data["message"]
        assert crew.docker_calls() == []

    def test_one_run_at_a_time(self, crew):
        assert coding_crew._run_lock.acquire(blocking=False)
        try:
            _, data = crew.run("acme/app#12")
        finally:
            coding_crew._run_lock.release()
        assert data["outcome"] == "busy"
        assert crew.docker_calls() == []


class TestRun:
    def test_end_to_end_the_host_pushes_and_opens_a_draft_pr(self, crew):
        result, data = crew.run("acme/app#12")
        assert result.success, data
        assert data["outcome"] == "pr"
        assert data["pr_url"] == "https://github.com/acme/app/pull/40"
        assert data["branches"] == ["agent/12-fix-login"]
        assert data["cost_usd"] == 1.25 and data["turns"] == 14

        # The container: local image only, hardened, one empty output mount.
        [docker] = crew.docker_calls()
        args = docker["args"]
        assert args[:4] == ["run", "--rm", "--pull", "never"]
        for flag, value in (
            ("--cap-drop", "ALL"),
            ("--security-opt", "no-new-privileges"),
            ("--pids-limit", "1024"),
            ("--memory", "4g"),
        ):
            assert args[args.index(flag) + 1] == value
        assert args.count("--volume") == 1 and args[
            args.index("--volume") + 1
        ].endswith(":/out")
        assert (
            "CREW_PROMPT=/cuadrilla:ticket #12" in args
            and "CUADRILLA_UNATTENDED=1" in args
        )
        assert args[-1] == "openjarvis-coding-crew:local"
        assert not any(
            s in arg for arg in args for s in (API_KEY, READ_TOKEN, WRITE_TOKEN)
        ), "credential values must never be in argv"

        # Credentials: the API key and the READ token go in; the write token never does.
        assert docker["env"]["ANTHROPIC_API_KEY"] == API_KEY
        assert docker["env"]["GH_TOKEN"] == READ_TOKEN
        assert WRITE_TOKEN not in crew.container_log()
        assert "PUSHED" not in crew.container_log()

        # Published from the host: the agent branch only, main untouched.
        assert "agent/12-fix-login" in crew.remote_branches()
        assert _git(crew.origin, "log", "-1", "--format=%s", "main") == "init"
        [pr] = [c for c in crew.gh_calls() if c["args"][:2] == ["pr", "create"]]
        assert "--draft" in pr["args"]
        assert pr["args"][pr["args"].index("--head") + 1] == "agent/12-fix-login"
        assert pr["args"][pr["args"].index("--base") + 1] == "main"
        assert pr["args"][pr["args"].index("--title") + 1] == "#12: Login fails"
        assert "written by the coding agent" in pr["stdin"]
        assert "Fixes the login." in pr["stdin"] and "Refs #12" in pr["stdin"]

        assert crew.labels("acme/app", 12) == ["agent-done"]
        [comment] = crew.comments()
        assert "https://github.com/acme/app/pull/40" in comment
        assert "$1.25" in comment and "`agent/12-fix-login`" in comment

    def test_without_a_read_token_the_container_has_no_github_token(
        self, crew, monkeypatch
    ):
        monkeypatch.delenv("GH_READ_TOKEN")
        result, _ = crew.run("acme/app#12")
        assert result.success
        [docker] = crew.docker_calls()
        assert docker["env"]["GH_TOKEN"] is None
        assert "GH_TOKEN" not in docker["args"]

    def test_uncommitted_work_still_reaches_a_reviewable_branch(
        self, crew, monkeypatch
    ):
        monkeypatch.setenv("FAKE_MODE", "uncommitted")
        result, data = crew.run("acme/app#12")
        assert result.success
        assert data["branches"] == ["agent/12-crew"]
        assert "agent/12-crew" in crew.remote_branches()

    @pytest.mark.parametrize(
        ("mode", "outcome", "public"),
        [
            ("outside", "no-changes", "without committing any change"),
            ("nochange", "no-changes", "without committing any change"),
            ("budget", "budget", "spending limit"),
            ("garbage", "agent-error", "stopped with an error"),
        ],
    )
    def test_failures_label_failed_with_a_fixed_public_reason(
        self, crew, monkeypatch, mode, outcome, public
    ):
        monkeypatch.setenv("FAKE_MODE", mode)
        result, data = crew.run("acme/app#12")
        assert not result.success
        assert data["outcome"] == outcome
        assert crew.labels("acme/app", 12) == ["agent-failed"]
        assert crew.remote_branches() == ["main"]
        [comment] = crew.comments()
        assert public in comment and "To retry" in comment

    def test_clone_failure(self, crew, monkeypatch):
        monkeypatch.setenv("FAKE_ORIGIN", "/nonexistent/repo.git")
        _, data = crew.run("acme/app#12")
        assert data["outcome"] == "clone-failed"
        assert "could not be cloned" in crew.comments()[0]

    def test_container_errors_never_leak_raw_output_into_the_issue(
        self, crew, monkeypatch
    ):
        monkeypatch.setenv("FAKE_DOCKER_MODE", "silent")
        _, data = crew.run("acme/app#12")
        assert data["outcome"] == "container-error"
        assert "Unable to find image" in data["message"], (
            "details stay in the tool result"
        )
        [comment] = crew.comments()
        assert "Unable to find image" not in comment
        assert "did not report a result" in comment

    def test_pr_failure_keeps_the_branch_and_labels_failed(self, crew):
        crew.set_state(pr_fails=True)
        _, data = crew.run("acme/app#12")
        assert data["outcome"] == "pr-failed"
        assert "agent/12-fix-login" in crew.remote_branches()
        assert crew.labels("acme/app", 12) == ["agent-failed"]

    def test_never_overwrites_an_existing_remote_branch(self, crew, tmp_path):
        other = tmp_path / "other"
        _git(tmp_path, "clone", "-q", str(crew.origin), str(other))
        _git(other, "switch", "-q", "-c", "agent/12-fix-login")
        (other / "theirs.txt").write_text("someone else's work\n")
        _git(other, "add", ".")
        _git(other, "commit", "-q", "-m", "theirs")
        _git(other, "push", "-q", "origin", "agent/12-fix-login")
        _, data = crew.run("acme/app#12")
        assert data["outcome"] == "publish-failed"
        assert (
            _git(crew.origin, "log", "-1", "--format=%s", "agent/12-fix-login")
            == "theirs"
        )

    def test_kills_the_container_at_the_timeout(self, crew, monkeypatch):
        monkeypatch.setenv("FAKE_DOCKER_MODE", "sleep")
        monkeypatch.setattr(
            coding_crew.subprocess, "run", _short_timeout(coding_crew.subprocess.run)
        )
        _, data = crew.run("acme/app#12")
        assert data["outcome"] == "timeout"
        assert any(call["args"][0] == "kill" for call in crew.docker_calls())
        assert crew.labels("acme/app", 12) == ["agent-failed"]

    def test_an_unexpected_error_never_leaves_the_issue_claimed(
        self, crew, monkeypatch
    ):
        def boom(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(CodingCrewRunTool, "_publish", boom)
        _, data = crew.run("acme/app#12")
        assert data["outcome"] == "error"
        assert crew.labels("acme/app", 12) == ["agent-failed"]
        assert "disk full" not in crew.comments()[0]

    def test_comment_can_be_turned_off(self, crew):
        crew.run("acme/app#12", comment=False)
        assert crew.comments() == []


class TestUntrustedContainerOutput:
    """The container's result is produced next to an agent that may be misled."""

    def _forge(self, crew, monkeypatch, tmp_path, payload):
        docker = tmp_path / "forger"
        _script(
            docker,
            f"import json, sys\nprint('CREW_RESULT ' + json.dumps({payload!r}))\n",
        )
        crew.config.docker_bin = str(docker)

    @pytest.mark.parametrize(
        "branches",
        [
            ["main"],
            ["agent/../main"],
            ["agent/"],
            ["refs/heads/main"],
            ["feature/x"],
            [7],
        ],
    )
    def test_ignores_branches_outside_the_agent_prefix(
        self, crew, monkeypatch, tmp_path, branches
    ):
        self._forge(
            crew,
            monkeypatch,
            tmp_path,
            {"claude": {}, "branches": branches, "base_sha": "a" * 40},
        )
        _, data = crew.run("acme/app#12")
        assert data["outcome"] == "no-changes"
        assert crew.remote_branches() == ["main"]

    def test_missing_bundle_publishes_nothing(self, crew, monkeypatch, tmp_path):
        self._forge(
            crew,
            monkeypatch,
            tmp_path,
            {"claude": {}, "branches": ["agent/12-x"], "base_sha": "a" * 40},
        )
        _, data = crew.run("acme/app#12")
        assert data["outcome"] == "publish-failed"
        assert crew.remote_branches() == ["main"]


def _short_timeout(real_run):
    """Makes the docker run time out after 1 s instead of the configured minutes."""

    def run(cmd, *args, **kwargs):
        if kwargs.get("timeout", 0) >= 60 and "run" in cmd[1:2]:
            kwargs["timeout"] = 1
        return real_run(cmd, *args, **kwargs)

    return run
