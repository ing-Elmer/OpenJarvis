"""Coding crew tools — GitHub issues solved by a coding agent inside a container.

A person with write access labels an issue ``agent-ready``. The ``coding_crew``
operator finds it with ``coding_crew_queue`` and hands it to ``coding_crew_run``,
which claims the issue, starts a disposable container where a coding agent (Claude
Code with the Cuadrilla kit by default) solves it in unattended mode, then publishes
the agent's commits as an ``agent/*`` branch and a **draft** pull request, and records
the outcome on the issue. A person reviews and merges; nothing here merges.

The work is deterministic and lives in these tools. The operator's model only decides
what to pick next and keeps state between ticks, which a small local model can do.

Trust boundaries:

- **The container never holds a token that can write to GitHub.** The agent reads the
  issue and the code (with an optional read-only token) and commits locally. The
  container hands its commits back as a git bundle; this process imports them into an
  empty repository it creates, keeps only ``agent/*`` branches, and pushes them with the
  host's own ``gh`` login. An agent misled by a malicious issue or comment can't push,
  open pull requests or leak a write token, because there is none to leak.
- **Only trusted people start a run.** The ready label must have been added by someone
  whose permission on the repository is in ``trusted_permissions``.
- **Only allowed repositories**, only open issues with the ready label and none of the
  crew's labels, references validated before anything runs.
- **Local image only** (``--pull never``), hardened container (non-root, ``--cap-drop
  ALL``, ``no-new-privileges``, memory/CPU/PID limits), killed at the timeout. Its only
  mount is an empty, per-run output folder.
- **Public comments carry fixed messages and validated links only.** Raw output stays in
  the tool result.
- One run at a time per process. The working label makes a second machine skip a
  claimed issue, but the claim is not atomic: run one crew per repository.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

logger = logging.getLogger(__name__)

_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
_ISSUE_REF = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100})#(\d{1,7})\s*$"
)
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?$")
_BRANCH_CHARS = re.compile(r"^[A-Za-z0-9._/-]{1,120}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_RESULT_MARKER = "CREW_RESULT "
# Names that must never reach the container: they would let the agent write to GitHub.
_WRITE_TOKENS = {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_PAT"}
_MAX_BODY_CHARS = 60_000
_run_lock = threading.Lock()

# Public explanations, keyed by outcome. Nothing else is written on the issue.
_PUBLIC_REASON = {
    "untrusted": (
        "the ready label was added by someone without write access to this repository"
    ),
    "clone-failed": "the repository could not be cloned inside the container",
    "no-changes": "the agent finished without committing any change",
    "budget": "the agent reached its spending limit",
    "turns": "the agent reached its turn limit",
    "agent-error": "the coding agent stopped with an error",
    "timeout": "the run reached its time limit",
    "container-error": "the container did not report a result",
    "publish-failed": "the agent's commits could not be pushed as an agent branch",
    "pr-failed": "the branch was pushed but the draft pull request could not be opened",
    "error": "the run stopped unexpectedly",
}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class CrewSettings:
    """Validated view of ``[tools.coding_crew]``."""

    repos: List[str]
    label: str
    working_label: str
    done_label: str
    failed_label: str
    trusted_permissions: List[str]
    image: str
    prompt: str
    branch_prefix: str
    unattended_env: Dict[str, str]
    env_passthrough: List[str]
    read_token_env: str
    model: str
    budget_usd: float
    timeout_minutes: int
    memory: str
    cpus: str
    max_bundle_mb: int
    comment: bool
    push_url: str
    docker_bin: str
    gh_bin: str
    git_bin: str


def _split(value: Any) -> List[str]:
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = str(value or "").split(",")
    return [str(item).strip() for item in items if str(item).strip()]


def load_settings(config: Any = None) -> CrewSettings:
    """Builds settings from a ``CodingCrewConfig`` (or the user's config.toml)."""
    if config is None:
        try:
            from openjarvis.core.config import load_config

            config = load_config().tools.coding_crew
        except Exception:  # pragma: no cover - config unavailable
            config = None

    def get(name: str, default: Any) -> Any:
        return getattr(config, name, default) if config is not None else default

    unattended: Dict[str, str] = {}
    for pair in _split(get("unattended_env", "CUADRILLA_UNATTENDED=1")):
        name, _, value = pair.partition("=")
        if _ENV_NAME.match(name.strip()) and name.strip() not in _WRITE_TOKENS:
            unattended[name.strip()] = value.strip()
    passthrough = []
    for name in _split(get("env_passthrough", "ANTHROPIC_API_KEY")):
        if name in _WRITE_TOKENS:
            logger.warning(
                "coding_crew: %s is never passed to the container; the host pushes.",
                name,
            )
        elif _ENV_NAME.match(name):
            passthrough.append(name)
    read_token_env = str(get("read_token_env", "GH_READ_TOKEN") or "")
    prefix = str(get("branch_prefix", "agent/"))
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*/$", prefix):
        prefix = "agent/"
    return CrewSettings(
        repos=[r for r in _split(get("repos", "")) if _REPO.match(r)],
        label=str(get("label", "agent-ready")),
        working_label=str(get("working_label", "agent-working")),
        done_label=str(get("done_label", "agent-done")),
        failed_label=str(get("failed_label", "agent-failed")),
        trusted_permissions=[
            p.lower()
            for p in _split(get("trusted_permissions", "admin,maintain,write"))
        ],
        image=str(get("image", "openjarvis-coding-crew:local")),
        prompt=str(get("prompt", "/cuadrilla:ticket #{issue}")),
        branch_prefix=prefix,
        unattended_env=unattended,
        env_passthrough=passthrough,
        read_token_env=read_token_env if _ENV_NAME.match(read_token_env) else "",
        model=str(get("model", "") or ""),
        budget_usd=float(get("budget_usd", 5.0) or 5.0),
        timeout_minutes=max(1, int(get("timeout_minutes", 45) or 45)),
        memory=str(get("memory", "4g")),
        cpus=str(get("cpus", "2")),
        max_bundle_mb=max(1, int(get("max_bundle_mb", 200) or 200)),
        comment=bool(get("comment", True)),
        push_url=str(get("push_url", "https://github.com/{repo}.git")),
        docker_bin=str(get("docker_bin", "docker")),
        gh_bin=str(get("gh_bin", "gh")),
        git_bin=str(get("git_bin", "git")),
    )


# ---------------------------------------------------------------------------
# GitHub (through the gh CLI on the host)
# ---------------------------------------------------------------------------


def _gh(
    settings: CrewSettings,
    args: List[str],
    stdin: Optional[str] = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [settings.gh_bin, *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _label_names(issue: Dict[str, Any]) -> List[str]:
    return [
        str(label.get("name", ""))
        for label in issue.get("labels", [])
        if isinstance(label, dict)
    ]


def _blocking_labels(settings: CrewSettings) -> set:
    return {settings.working_label, settings.done_label, settings.failed_label}


def _ensure_labels(settings: CrewSettings, repo: str) -> None:
    """Creates the crew's labels if missing; an existing label is left as it is."""
    for name, color, description in (
        (settings.working_label, "fbca04", "A coding agent is working on this issue"),
        (settings.done_label, "0e8a16", "A coding agent opened a draft pull request"),
        (settings.failed_label, "d93f0b", "A coding agent could not finish this issue"),
    ):
        _gh(
            settings,
            [
                "label",
                "create",
                name,
                "-R",
                repo,
                "--color",
                color,
                "--description",
                description,
            ],
        )


def _edit_labels(
    settings: CrewSettings, repo: str, number: int, add: List[str], remove: List[str]
) -> None:
    args = ["issue", "edit", str(number), "-R", repo]
    for label in add:
        args += ["--add-label", label]
    for label in remove:
        args += ["--remove-label", label]
    try:
        result = _gh(settings, args)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(
            "coding_crew: could not update labels on %s#%s: %s", repo, number, exc
        )
        return
    if result.returncode != 0:
        logger.warning(
            "coding_crew: could not update labels on %s#%s: %s",
            repo,
            number,
            result.stderr.strip(),
        )


def _labeler(
    settings: CrewSettings, repo: str, number: int
) -> Tuple[Optional[str], str]:
    """Who added the ready label last, and their permission on the repository."""
    events = _gh(
        settings,
        [
            "api",
            "--paginate",
            f"repos/{repo}/issues/{number}/events",
            "--jq",
            '.[] | select(.event == "labeled") | [.actor.login, .label.name] | @tsv',
        ],
    )
    if events.returncode != 0:
        return None, "unknown"
    login = None
    for line in events.stdout.splitlines():
        actor, _, label = line.partition("\t")
        if label == settings.label:
            login = actor
    if not login or not _LOGIN.match(login):
        return login, "unknown"
    permission = _gh(
        settings,
        [
            "api",
            f"repos/{repo}/collaborators/{login}/permission",
            "--jq",
            ".permission",
        ],
    )
    if permission.returncode != 0:
        return login, "none"
    return login, permission.stdout.strip().lower() or "none"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@ToolRegistry.register("coding_crew_queue")
class CodingCrewQueueTool(BaseTool):
    """Lists issues ready for the coding crew in the allowed repositories."""

    tool_id = "coding_crew_queue"

    def __init__(self, config: Any = None) -> None:
        self._config = config

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="coding_crew_queue",
            description=(
                "List open GitHub issues that are ready for the coding crew "
                "(labelled agent-ready, not yet claimed) in the configured "
                "repositories, oldest first. Returns issue "
                "references like owner/repo#12 to pass to coding_crew_run."
            ),
            parameters={"type": "object", "properties": {}, "required": []},
            category="vcs",
            timeout_seconds=120,
            required_capabilities=["network:fetch"],
        )

    def execute(self, **params: Any) -> ToolResult:
        settings = load_settings(self._config)
        if not settings.repos:
            return ToolResult(
                tool_name=self.tool_id,
                content=(
                    "No repositories configured. "
                    "Set [tools.coding_crew] repos in config.toml."
                ),
                success=False,
            )
        ready: List[Dict[str, Any]] = []
        errors: List[str] = []
        for repo in settings.repos:
            try:
                result = _gh(
                    settings,
                    [
                        "issue",
                        "list",
                        "-R",
                        repo,
                        "--state",
                        "open",
                        "--label",
                        settings.label,
                        "--json",
                        "number,title,labels,createdAt,url",
                        "--limit",
                        "50",
                    ],
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                errors.append(f"{repo}: {exc}")
                continue
            if result.returncode != 0:
                errors.append(f"{repo}: {result.stderr.strip()[:200]}")
                continue
            try:
                issues = json.loads(result.stdout or "[]")
            except json.JSONDecodeError:
                errors.append(f"{repo}: unexpected gh output")
                continue
            for issue in issues:
                if _blocking_labels(settings) & set(_label_names(issue)):
                    continue
                ready.append(
                    {
                        "ref": f"{repo}#{issue.get('number')}",
                        "title": str(issue.get("title", ""))[:200],
                        "created_at": issue.get("createdAt", ""),
                    }
                )
        ready.sort(key=lambda item: item["created_at"])
        return ToolResult(
            tool_name=self.tool_id,
            content=json.dumps({"ready": ready, "errors": errors}, ensure_ascii=False),
            success=not errors or bool(ready),
            metadata={"count": len(ready)},
        )


@ToolRegistry.register("coding_crew_run")
class CodingCrewRunTool(BaseTool):
    """Solves one ready issue in a disposable container; records the outcome."""

    tool_id = "coding_crew_run"

    def __init__(self, config: Any = None) -> None:
        self._config = config

    @property
    def spec(self) -> ToolSpec:
        settings = load_settings(self._config)
        return ToolSpec(
            name="coding_crew_run",
            description=(
                "Hand one issue returned by coding_crew_queue (e.g. owner/repo#12) "
                "to the coding crew: it is claimed, solved by a coding agent in a "
                "disposable container, published as a draft pull request, and "
                "labelled with the result. Returns the outcome and the pull request "
                "link. Takes up to the configured timeout; run one issue at a time."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "issue": {
                        "type": "string",
                        "description": (
                            "Issue reference exactly as returned by "
                            "coding_crew_queue, e.g. owner/repo#12."
                        ),
                    }
                },
                "required": ["issue"],
            },
            category="vcs",
            cost_estimate=settings.budget_usd,
            latency_estimate=settings.timeout_minutes * 60 / 2,
            timeout_seconds=settings.timeout_minutes * 60 + 600,
            required_capabilities=["network:fetch", "code:execute"],
        )

    def execute(self, **params: Any) -> ToolResult:
        if not _run_lock.acquire(blocking=False):
            return self._result(
                "busy", "Another coding crew run is in progress; try again next tick."
            )
        try:
            return self._run(str(params.get("issue", "")))
        except (OSError, subprocess.TimeoutExpired) as exc:
            return self._result(
                "error", f"GitHub CLI unavailable or not responding: {exc}"
            )
        finally:
            _run_lock.release()

    # -- helpers -------------------------------------------------------------

    def _result(
        self, outcome: str, message: str, success: bool = False, **extra: Any
    ) -> ToolResult:
        payload = {"outcome": outcome, "message": message, **extra}
        return ToolResult(
            tool_name=self.tool_id,
            content=json.dumps(payload, ensure_ascii=False),
            success=success,
            metadata={
                "outcome": outcome,
                **{k: v for k, v in extra.items() if k in ("pr_url", "cost_usd")},
            },
        )

    def _run(self, ref: str) -> ToolResult:
        settings = load_settings(self._config)
        match = _ISSUE_REF.match(ref)
        if not match:
            return self._result(
                "invalid", f"'{ref}' is not an issue reference like owner/repo#12."
            )
        repo, number = match.group(1), int(match.group(2))
        if repo.lower() not in {r.lower() for r in settings.repos}:
            return self._result(
                "not-allowed", f"{repo} is not in [tools.coding_crew] repos."
            )
        missing = [
            name for name in settings.env_passthrough if not os.environ.get(name)
        ]
        if missing:
            return self._result(
                "not-configured",
                "Missing environment variables for the container: "
                f"{', '.join(missing)}.",
            )

        view = _gh(
            settings,
            ["issue", "view", str(number), "-R", repo, "--json", "state,labels,title"],
        )
        if view.returncode != 0:
            return self._result(
                "error", f"Could not read {ref}: {view.stderr.strip()[:200]}"
            )
        try:
            issue = json.loads(view.stdout)
        except json.JSONDecodeError:
            return self._result("error", f"Unexpected gh output for {ref}.")
        labels = set(_label_names(issue))
        if (
            str(issue.get("state", "")).upper() != "OPEN"
            or settings.label not in labels
        ):
            return self._result(
                "not-ready", f"{ref} is not open with the '{settings.label}' label."
            )
        if _blocking_labels(settings) & labels:
            return self._result(
                "not-ready",
                f"{ref} is already claimed or finished "
                f"({', '.join(sorted(_blocking_labels(settings) & labels))}).",
            )

        _ensure_labels(settings, repo)
        login, permission = _labeler(settings, repo, number)
        if permission not in settings.trusted_permissions:
            refused = {
                "outcome": "untrusted",
                "detail": f"labelled by {login or 'unknown'} ({permission})",
            }
            self._finish(settings, repo, number, refused, minutes=0.0)
            return self._result(
                "untrusted",
                f"{ref}: the '{settings.label}' label was added by "
                f"{login or 'an unknown user'} with '{permission}' permission; "
                f"only {', '.join(settings.trusted_permissions)} may start a run.",
            )

        _edit_labels(settings, repo, number, add=[settings.working_label], remove=[])
        started = time.time()
        run: Dict[str, Any] = {
            "outcome": "error",
            "detail": "the run stopped unexpectedly",
        }
        try:
            with tempfile.TemporaryDirectory(prefix="oj-crew-") as scratch:
                out = Path(scratch) / "out"
                out.mkdir()
                out.chmod(
                    0o777
                )  # the container user writes here; the parent is private
                run = self._container(settings, repo, number, out)
                if run.get("outcome") == "committed":
                    run = self._publish(settings, repo, number, issue, out, run)
        except Exception as exc:  # never leave the issue stuck in agent-working
            logger.exception("coding_crew: run for %s failed", ref)
            run = {"outcome": "error", "detail": f"{type(exc).__name__}: {exc}"}
        finally:
            minutes = round((time.time() - started) / 60, 1)
            self._finish(settings, repo, number, run, minutes)

        pr_url = run.get("pr_url")
        outcome = "pr" if pr_url else run.get("outcome", "error")
        return self._result(
            outcome,
            f"Draft pull request opened for {ref}: {pr_url}"
            if pr_url
            else f"No pull request for {ref}: "
            f"{run.get('detail') or _PUBLIC_REASON.get(outcome, outcome)}",
            success=bool(pr_url),
            ref=ref,
            pr_url=pr_url,
            branches=run.get("branches", []),
            cost_usd=run.get("cost_usd"),
            turns=run.get("turns"),
            minutes=minutes,
        )

    def _finish(
        self,
        settings: CrewSettings,
        repo: str,
        number: int,
        run: Dict[str, Any],
        minutes: float,
    ) -> None:
        pr_url = run.get("pr_url")
        _edit_labels(
            settings,
            repo,
            number,
            add=[settings.done_label if pr_url else settings.failed_label],
            remove=[settings.working_label, settings.label],
        )
        if settings.comment:
            try:
                _gh(
                    settings,
                    ["issue", "comment", str(number), "-R", repo, "--body-file", "-"],
                    stdin=self._comment(run, minutes),
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                logger.warning(
                    "coding_crew: could not comment on %s#%s: %s", repo, number, exc
                )

    # -- the container -------------------------------------------------------

    def _docker_args(
        self, settings: CrewSettings, repo: str, number: int, name: str, out: Path
    ) -> List[str]:
        prompt = settings.prompt.replace("{issue}", str(number)).replace("{repo}", repo)
        env = {
            "CREW_REPO": repo,
            "CREW_ISSUE": str(number),
            "CREW_PROMPT": prompt,
            "CREW_BUDGET_USD": str(settings.budget_usd),
            "CREW_MODEL": settings.model,
            "CREW_BRANCH_PREFIX": settings.branch_prefix,
            **settings.unattended_env,
        }
        args = [
            settings.docker_bin,
            "run",
            "--rm",
            "--pull",
            "never",
            "--name",
            name,
            "--memory",
            settings.memory,
            "--cpus",
            settings.cpus,
            "--pids-limit",
            "1024",
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
            "--volume",
            f"{out}:/out",
        ]
        for key, value in env.items():
            args += ["-e", f"{key}={value}"]
        for key in settings.env_passthrough:
            args += ["-e", key]  # value inherited from this process, never in argv
        if settings.read_token_env and os.environ.get(settings.read_token_env):
            args += ["-e", "GH_TOKEN"]  # the read-only token, set in _container_env
        return [*args, settings.image]

    @staticmethod
    def _container_env(settings: CrewSettings) -> Dict[str, str]:
        """The docker client's environment: no write token, read token as GH_TOKEN."""
        env = {k: v for k, v in os.environ.items() if k not in _WRITE_TOKENS}
        if settings.read_token_env and os.environ.get(settings.read_token_env):
            env["GH_TOKEN"] = os.environ[settings.read_token_env]
        return env

    def _container(
        self, settings: CrewSettings, repo: str, number: int, out: Path
    ) -> Dict[str, Any]:
        slug = re.sub(r"[^a-z0-9]+", "-", repo.lower())
        name = f"oj-crew-{slug}-{number}-{int(time.time())}"
        try:
            proc = subprocess.run(
                self._docker_args(settings, repo, number, name, out),
                capture_output=True,
                text=True,
                timeout=settings.timeout_minutes * 60,
                env=self._container_env(settings),
            )
        except subprocess.TimeoutExpired:
            try:
                subprocess.run(
                    [settings.docker_bin, "kill", name],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except (OSError, subprocess.TimeoutExpired):
                logger.warning("coding_crew: could not kill container %s", name)
            return {
                "outcome": "timeout",
                "detail": f"timed out after {settings.timeout_minutes} minutes",
            }
        except OSError as exc:
            return {
                "outcome": "container-error",
                "detail": f"could not start docker: {exc}",
            }

        data: Dict[str, Any] = {}
        for line in reversed((proc.stdout or "").splitlines()):
            if line.startswith(_RESULT_MARKER):
                try:
                    data = json.loads(line[len(_RESULT_MARKER) :])
                except json.JSONDecodeError:
                    data = {}
                break
        if not isinstance(data, dict) or not data:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [
                "no output"
            ]
            return {
                "outcome": "container-error",
                "detail": (
                    f"container exited {proc.returncode} without a result "
                    f"({tail[0][:200]})"
                ),
            }
        claude = data.get("claude") if isinstance(data.get("claude"), dict) else {}
        common = {
            "cost_usd": claude.get("total_cost_usd")
            if isinstance(claude.get("total_cost_usd"), (int, float))
            else None,
            "turns": claude.get("num_turns")
            if isinstance(claude.get("num_turns"), int)
            else None,
        }
        if data.get("error"):
            outcome = (
                "clone-failed" if data["error"] == "clone-failed" else "container-error"
            )
            return {
                "outcome": outcome,
                "detail": str(data.get("detail") or data["error"])[:300],
                **common,
            }
        branches = [
            b
            for b in data.get("branches", [])
            if isinstance(b, str) and self._valid_branch(settings, b)
        ]
        if not branches:
            if claude.get("is_error"):
                subtype = str(claude.get("subtype", ""))
                outcome = {
                    "error_max_budget_usd": "budget",
                    "error_max_turns": "turns",
                }.get(subtype, "agent-error")
                errors = (
                    claude.get("errors")
                    if isinstance(claude.get("errors"), list)
                    else []
                )
                return {
                    "outcome": outcome,
                    "detail": "; ".join(map(str, errors))[:300] or subtype,
                    **common,
                }
            return {
                "outcome": "no-changes",
                "detail": "no agent branch with new commits",
                **common,
            }
        base_sha = str(data.get("base_sha", ""))
        if not _SHA.match(base_sha):
            return {
                "outcome": "container-error",
                "detail": "invalid base commit in the result",
                **common,
            }
        return {
            "outcome": "committed",
            "branches": branches,
            "base_sha": base_sha,
            **common,
        }

    @staticmethod
    def _valid_branch(settings: CrewSettings, branch: str) -> bool:
        return (
            branch.startswith(settings.branch_prefix)
            and len(branch) > len(settings.branch_prefix)
            and bool(_BRANCH_CHARS.match(branch))
            and ".." not in branch
            and "//" not in branch
            and not branch.endswith((".lock", "/"))
            and not re.search(r"(^|/)[.-]", branch)
        )

    # -- publishing, on the host ---------------------------------------------

    def _git(
        self, settings: CrewSettings, args: List[str], cwd: Path, timeout: int = 600
    ) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        return subprocess.run(
            [
                settings.git_bin,
                "-c",
                "credential.helper=",
                "-c",
                f"credential.helper=!{settings.gh_bin} auth git-credential",
                "-c",
                "transfer.fsckObjects=true",
                "-c",
                "core.hooksPath=/dev/null",
                *args,
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )

    def _publish(
        self,
        settings: CrewSettings,
        repo: str,
        number: int,
        issue: Dict[str, Any],
        out: Path,
        run: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Imports the bundle into a fresh repository and pushes only agent branches."""
        bundle = out / "changes.bundle"
        failed = lambda detail: {**run, "outcome": "publish-failed", "detail": detail}  # noqa: E731
        if not bundle.is_file() or bundle.is_symlink():
            return failed("the container did not leave a bundle")
        if bundle.stat().st_size > settings.max_bundle_mb * 1024 * 1024:
            return failed(f"the bundle is larger than {settings.max_bundle_mb} MB")

        default = _gh(
            settings,
            [
                "repo",
                "view",
                repo,
                "--json",
                "defaultBranchRef",
                "--jq",
                ".defaultBranchRef.name",
            ],
        )
        base = default.stdout.strip()
        if default.returncode != 0 or not _BRANCH_CHARS.match(base):
            return failed("could not read the default branch")

        url = settings.push_url.replace("{repo}", repo)
        with tempfile.TemporaryDirectory(prefix="oj-crew-publish-") as tmp:
            work = Path(tmp)
            # A fresh, history-only copy of the base branch: no blobs, no hooks, no
            # config from anyone but this process.
            cloned = self._git(
                settings,
                [
                    "clone",
                    "-q",
                    "--bare",
                    "--filter=blob:none",
                    "--single-branch",
                    "--branch",
                    base,
                    url,
                    ".",
                ],
                work,
            )
            if cloned.returncode != 0:
                return failed(
                    f"could not clone the base branch: {cloned.stderr.strip()[-200:]}"
                )
            fetched = self._git(
                settings,
                [
                    "fetch",
                    "-q",
                    str(bundle),
                    f"refs/heads/{settings.branch_prefix}*:refs/heads/{settings.branch_prefix}*",
                ],
                work,
            )
            if fetched.returncode != 0:
                return failed(
                    f"the bundle could not be imported: {fetched.stderr.strip()[-200:]}"
                )
            pushed: List[str] = []
            for branch in run["branches"]:
                exists = self._git(
                    settings,
                    ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
                    work,
                )
                if exists.returncode != 0:
                    continue
                result = self._git(
                    settings,
                    [
                        "push",
                        "-q",
                        "origin",
                        f"refs/heads/{branch}:refs/heads/{branch}",
                    ],
                    work,
                )
                if result.returncode == 0:
                    pushed.append(branch)
                else:
                    logger.warning(
                        "coding_crew: push of %s failed: %s",
                        branch,
                        result.stderr.strip()[-200:],
                    )
        if not pushed:
            return failed("no agent branch could be pushed")

        head = next(
            (b for b in pushed if re.search(rf"(^|[/-]){number}([/-]|$)", b)), pushed[0]
        )
        body = self._pr_body(out, repo, number)
        title = f"#{number}: {str(issue.get('title', '')).strip()[:200]}".strip()
        pr = _gh(
            settings,
            [
                "pr",
                "create",
                "-R",
                repo,
                "--draft",
                "--base",
                base,
                "--head",
                head,
                "--title",
                title,
                "--body-file",
                "-",
            ],
            stdin=body,
            timeout=120,
        )
        pr_url = None
        for line in reversed(pr.stdout.splitlines()):
            if re.fullmatch(
                rf"https://github\.com/{re.escape(repo)}/pull/\d+",
                line.strip(),
                flags=re.IGNORECASE,
            ):
                pr_url = line.strip()
                break
        if pr.returncode != 0 or not pr_url:
            return {
                **run,
                "outcome": "pr-failed",
                "branches": pushed,
                "detail": pr.stderr.strip()[-200:],
            }
        return {**run, "outcome": "pr", "branches": pushed, "pr_url": pr_url}

    @staticmethod
    def _pr_body(out: Path, repo: str, number: int) -> str:
        header = (
            f"Opened by the OpenJarvis coding crew for #{number}. "
            "The description below was written by the coding agent: "
            "review the diff before merging.\n\n"
        )
        written = out / "pr-body.md"
        text = ""
        if written.is_file() and not written.is_symlink():
            text = written.read_text(encoding="utf-8", errors="replace")[
                :_MAX_BODY_CHARS
            ]
        return (
            header
            + (text.strip() or "_The agent did not write a description._")
            + f"\n\nRefs #{number}\n"
        )

    @staticmethod
    def _comment(run: Dict[str, Any], minutes: float) -> str:
        """Fixed messages and validated values only: no agent or container text."""
        lines = ["**OpenJarvis coding crew**", ""]
        if run.get("pr_url"):
            lines.append(
                f"Draft pull request: {run['pr_url']} — "
                "please review it before merging."
            )
        else:
            reason = _PUBLIC_REASON.get(
                str(run.get("outcome")), _PUBLIC_REASON["error"]
            )
            lines.append(f"No pull request this time: {reason}.")
            if run.get("outcome") == "untrusted":
                lines.append(
                    "Someone with write access must add the ready label "
                    "for the crew to start."
                )
            else:
                lines.append(
                    "To retry, remove the failure label and add the ready label again."
                )
        details = [f"{minutes} min"] if minutes else []
        if isinstance(run.get("turns"), int):
            details.append(f"{run['turns']} turns")
        if isinstance(run.get("cost_usd"), (int, float)):
            details.append(f"${run['cost_usd']:.2f}")
        if run.get("branches") and run.get("pr_url"):
            details.append("branch " + ", ".join(f"`{b}`" for b in run["branches"][:3]))
        if details:
            lines += ["", " · ".join(details)]
        return "\n".join(lines)


__all__ = ["CodingCrewQueueTool", "CodingCrewRunTool", "CrewSettings", "load_settings"]
