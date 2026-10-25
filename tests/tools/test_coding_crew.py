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


# ---------------------------------------------------------------------------
# Jira as the source of tickets
# ---------------------------------------------------------------------------

TRUSTED = "712020:owner"
JIRA_TOKEN = "jira-secret-token-value"


@pytest.fixture()
def jira(crew, monkeypatch):
    import base64
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    def ticket(summary, labels, status="Por hacer", category="new"):
        return {
            "summary": summary,
            "labels": labels,
            "created": "2026-10-24T10:00:00.000+0000",
            "status": {"name": status, "statusCategory": {"key": category}},
            "issuetype": {"name": "Subtarea"},
            "description": {
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": f"Do: {summary}"}],
                    }
                ],
            },
            "comment": {"comments": []},
        }

    state = {
        "issues": {
            "SCRUM-68": ticket(
                "/health returns the version", ["backend", "agent-ready"]
            ),
            "SCRUM-69": ticket("Labelled by a stranger", ["agent-ready"]),
            "SCRUM-70": ticket("Claimed", ["agent-ready", "agent-working"]),
            "SCRUM-71": ticket("Finished", ["agent-ready"], "Hecho", "done"),
            "OTHER-1": ticket("Other project", ["agent-ready"]),
        },
        "labeled_by": {
            "SCRUM-68": [
                ("712020:stranger", "", "agent-ready"),  # removed later, then re-added
                (TRUSTED, "agent-ready", ""),
                (TRUSTED, "backend", "agent-ready backend"),
            ],
            "SCRUM-69": [
                (TRUSTED, "", "backend"),
                ("712020:stranger", "", "agent-ready"),
            ],
        },
        "requests": [],
        "comments": [],
    }
    expected = "Basic " + base64.b64encode(f"me@x.com:{JIRA_TOKEN}".encode()).decode()
    transitions = [
        {"id": "21", "name": "Iniciar", "to": {"name": "En curso"}},
        {"id": "31", "name": "Revisar", "to": {"name": "En revisión"}},
        {"id": "11", "name": "Reabrir", "to": {"name": "Por hacer"}},
    ]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _reply(self, code, body=None):
            data = json.dumps(body).encode() if body is not None else b""
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _handle(self, method):
            url = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            state["requests"].append((method, url.path, body))
            if self.headers.get("Authorization") != expected:
                return self._reply(401, {"errorMessages": ["unauthorized"]})
            parts = url.path.split("/")
            if method == "POST" and url.path == "/rest/api/3/search/jql":
                jql = body["jql"]
                found = [
                    {"key": key, "fields": fields}
                    for key, fields in state["issues"].items()
                    if key.split("-")[0] in jql
                    and "agent-ready" in fields["labels"]
                    and fields["status"]["statusCategory"]["key"] != "done"
                ]
                return self._reply(200, {"issues": found})
            key = parts[5] if len(parts) > 5 else ""
            issue = state["issues"].get(key)
            if issue is None:
                return self._reply(404, {"errorMessages": ["not found"]})
            tail = parts[6] if len(parts) > 6 else ""
            if method == "GET" and not tail:
                return self._reply(200, {"key": key, "fields": issue})
            if method == "GET" and tail == "changelog":
                start = int(parse_qs(url.query).get("startAt", ["0"])[0])
                history = [
                    {
                        "author": {"accountId": who},
                        "items": [
                            {"field": "labels", "fromString": old, "toString": new}
                        ],
                    }
                    for who, old, new in state["labeled_by"].get(key, [])
                ]
                page = history[start : start + 2]  # small pages exercise pagination
                return self._reply(
                    200, {"values": page, "isLast": start + 2 >= len(history)}
                )
            if method == "PUT" and not tail:
                for op in body["update"]["labels"]:
                    if "add" in op and op["add"] not in issue["labels"]:
                        issue["labels"].append(op["add"])
                    if "remove" in op and op["remove"] in issue["labels"]:
                        issue["labels"].remove(op["remove"])
                return self._reply(204)
            if method == "GET" and tail == "transitions":
                return self._reply(200, {"transitions": transitions})
            if method == "POST" and tail == "transitions":
                chosen = next(
                    t for t in transitions if t["id"] == body["transition"]["id"]
                )
                issue["status"]["name"] = chosen["to"]["name"]
                return self._reply(204)
            if method == "POST" and tail == "comment":
                state["comments"].append((key, body["body"]))
                return self._reply(201, {"id": "1"})
            return self._reply(400, {"errorMessages": ["unexpected"]})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_PUT(self):
            self._handle("PUT")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("JIRA_EMAIL", "me@x.com")
    monkeypatch.setenv("JIRA_API_TOKEN", JIRA_TOKEN)
    monkeypatch.setenv("FAKE_MODE", "jira")
    for key, value in {
        "source": "jira",
        "jira_url": f"http://127.0.0.1:{server.server_address[1]}",
        "jira_projects": "SCRUM=acme/app",
        "jira_trusted_accounts": TRUSTED,
        "jira_status_working": "En curso",
        "jira_status_review": "En revisión",
        "jira_status_failed": "Por hacer",
    }.items():
        setattr(crew.config, key, value)
    crew.jira = state
    yield crew
    server.shutdown()


def _comment_text(adf):
    return coding_crew._adf_text(adf)


class TestJira:
    def test_queue_lists_ready_unclaimed_tickets_of_mapped_projects(self, jira):
        result = CodingCrewQueueTool(config=jira.config).execute()
        data = json.loads(result.content)
        assert result.success
        assert [item["ref"] for item in data["ready"]] == ["SCRUM-68", "SCRUM-69"]
        jql = next(r[2]["jql"] for r in jira.jira["requests"] if r[0] == "POST")
        assert "project in (SCRUM)" in jql and 'labels = "agent-ready"' in jql

    def test_runs_a_ticket_end_to_end(self, jira):
        result, data = jira.run("SCRUM-68")
        assert data["outcome"] == "pr", data
        assert data["branches"] == ["agent/SCRUM-68-health-version"]
        assert "agent/SCRUM-68-health-version" in jira.remote_branches()
        issue = jira.jira["issues"]["SCRUM-68"]
        assert issue["status"]["name"] == "En revisión"
        assert "agent-done" in issue["labels"]
        assert "agent-ready" not in issue["labels"]
        assert "agent-working" not in issue["labels"]
        key, adf = jira.jira["comments"][-1]
        assert key == "SCRUM-68" and data["pr_url"] in _comment_text(adf)
        links = [
            m["attrs"]["href"]
            for p in adf["content"]
            for c in p["content"]
            for m in c.get("marks", [])
        ]
        assert links == [data["pr_url"]]
        # the agent got the ticket as a file and the prompt points to it
        log = jira.container_log()
        assert "ticket: # SCRUM-68: /health returns the version" in log
        assert "/out/ticket.md" in log
        # the PR is titled with the key and links back to Jira
        create = next(c for c in jira.gh_calls() if c["args"][:2] == ["pr", "create"])
        assert create["args"][create["args"].index("--title") + 1].startswith(
            "SCRUM-68: /health returns the version"
        )
        assert f"{jira.config.jira_url}/browse/SCRUM-68" in create["stdin"]
        # claimed (label + In progress) before the container ran
        moves = [
            r[2]["transition"]["id"]
            for r in jira.jira["requests"]
            if r[0] == "POST" and r[1].endswith("/transitions")
        ]
        assert moves == ["21", "31"]

    def test_jira_and_github_secrets_never_reach_the_container(self, jira):
        jira.run("SCRUM-68")
        call = jira.docker_calls()[0]
        assert JIRA_TOKEN not in json.dumps(call["args"])
        assert "JIRA_API_TOKEN" not in call["args"]
        assert call["env"]["GH_TOKEN"] == READ_TOKEN

    def test_refuses_a_ticket_labelled_by_an_untrusted_account(self, jira):
        result, data = jira.run("SCRUM-69")
        assert data["outcome"] == "untrusted"
        assert jira.docker_calls() == []
        issue = jira.jira["issues"]["SCRUM-69"]
        assert "agent-failed" in issue["labels"]
        assert "agent-ready" not in issue["labels"]
        assert "does not trust" in _comment_text(jira.jira["comments"][-1][1])

    @pytest.mark.parametrize(
        ("ref", "outcome"),
        [
            ("SCRUM-70", "not-ready"),
            ("SCRUM-71", "not-ready"),
            ("OTHER-1", "not-allowed"),
            ("acme/app#12", "invalid"),
        ],
    )
    def test_refuses_what_is_not_ready_or_allowed(self, jira, ref, outcome):
        _, data = jira.run(ref)
        assert data["outcome"] == outcome
        assert jira.docker_calls() == []

    def test_a_failed_run_goes_back_to_to_do(self, jira, monkeypatch):
        monkeypatch.setenv("FAKE_MODE", "nochange")
        _, data = jira.run("SCRUM-68")
        assert data["outcome"] == "no-changes"
        issue = jira.jira["issues"]["SCRUM-68"]
        assert issue["status"]["name"] == "Por hacer"
        assert "agent-failed" in issue["labels"]
        assert "agent-working" not in issue["labels"]

    def test_needs_jira_credentials_and_trusted_accounts(self, jira, monkeypatch):
        monkeypatch.delenv("JIRA_API_TOKEN")
        _, data = jira.run("SCRUM-68")
        assert data["outcome"] == "not-configured"
        monkeypatch.setenv("JIRA_API_TOKEN", JIRA_TOKEN)
        _, data = jira.run("SCRUM-68", jira_trusted_accounts="")
        assert data["outcome"] == "not-configured"

    def test_a_wrong_token_is_reported_without_leaking_it(self, jira, monkeypatch):
        monkeypatch.setenv("JIRA_API_TOKEN", "wrong")
        result, data = jira.run("SCRUM-68")
        assert data["outcome"] == "error" and "401" in data["message"]
        assert "wrong" not in result.content


def test_jira_settings_are_validated():
    settings = load_settings(
        CodingCrewConfig(
            source="jira",
            jira_url="http://evil.example",
            jira_projects="SCRUM=acme/app, bad=acme/x, OK=../evil",
            env_passthrough="ANTHROPIC_API_KEY,JIRA_API_TOKEN,MY_JIRA",
            jira_token_env="MY_JIRA",
        )
    )
    assert settings.source == "jira"
    assert settings.jira_url == ""
    assert settings.jira_projects == {"SCRUM": "acme/app"}
    assert settings.env_passthrough == ["ANTHROPIC_API_KEY"]


def test_scoped_tokens_use_the_atlassian_gateway(jira):
    port = jira.config.jira_url.rsplit(":", 1)[1]
    jira.config.jira_api_url = f"http://localhost:{port}"
    jira.config.jira_url = "https://agrovision.atlassian.net"
    _, data = jira.run("SCRUM-68")
    assert data["outcome"] == "pr"
    create = next(c for c in jira.gh_calls() if c["args"][:2] == ["pr", "create"])
    assert "https://agrovision.atlassian.net/browse/SCRUM-68" in create["stdin"]
    cloud = "3aacc152-153c-4440-ab43-17b531b1c3d5"
    gateway = load_settings(
        CodingCrewConfig(jira_api_url=f"https://api.atlassian.com/ex/jira/{cloud}/")
    )
    assert gateway.jira_api_url.endswith(cloud)
    evil = load_settings(CodingCrewConfig(jira_api_url="https://evil.example"))
    assert evil.jira_api_url == ""
