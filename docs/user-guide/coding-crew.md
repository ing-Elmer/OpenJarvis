# Coding Crew

Label a GitHub issue `agent-ready` and come back to a draft pull request. The `coding_crew`
operator watches the repositories you allow, hands each ready issue to a coding agent running
in a disposable container, publishes its work as a draft pull request, and records the outcome
on the issue. A person reviews and merges; nothing in this flow merges, approves or deploys.

```
 issue labelled agent-ready (by someone with write access)
          │
  coding_crew operator ── every 15 min, local model: picks the oldest ready issue
          │
  coding_crew_run ──────── checks who added the label, claims the issue (agent-working)
          │
  container ────────────── clones the repo and runs the coding agent in unattended mode;
          │                it commits on an agent/* branch and hands the commits back
          │                as a git bundle. It holds no token that can write to GitHub.
          │
  host ─────────────────── imports the bundle into a fresh repository, pushes only the
          │                agent/* branch with your gh login and opens a draft PR
          │
  issue ────────────────── agent-done + link to the draft PR, or agent-failed + why
```

The split follows OpenJarvis's local-first idea: the operator only decides what goes next and
keeps state, which a small local model does well; the coding runs in the container with the
agent you choose, and everything that touches GitHub runs in plain code on your machine.

## What runs in the container

By default the image runs [Claude Code](https://claude.com/claude-code) with the
[Cuadrilla](https://github.com/ing-Elmer/cuadrilla) kit: an orchestrator that plans and
delegates to stack specialists, reviewers, QA and guardrail hooks. Its unattended mode
(`CUADRILLA_UNATTENDED=1`) is what lets it work with nobody watching: confirmations become
denials and nothing waits for an answer. The prompt `/cuadrilla:ticket #<issue>` reads the
issue, implements it, runs the project's checks and writes the pull request description with
the result of each acceptance criterion.

The container tells the agent that it can't push and where to write that description; any
other agent or kit works the same way. Work the agent leaves uncommitted, or commits straight
on the base branch, is moved to an `agent/<issue>-crew` branch so it can still be reviewed.

## Setup

### 1. Build the image

```bash
docker build -t openjarvis-coding-crew:local \
  --build-arg KIT_REF=<commit or tag of the kit> deploy/coding-crew
```

The tool runs it with `--pull never`: it only ever uses the image you built, never one pulled
from a registry. Pin `KIT_REF` so the agents' instructions can't change without a rebuild.
Other build arguments: `CLAUDE_CODE_VERSION`, `KIT_REPO`, `KIT_MARKETPLACE_NAME`,
`KIT_PLUGINS` (the core plus the Node, React and Python packs; empty to use only the
repository's own `.claude/`), `UV_VERSION`, `PYTHON_VERSIONS` and `KIT_LANGUAGE` (the
language the agents write commits and pull requests in, e.g. `Español`). For other stacks, build an
image `FROM openjarvis-coding-crew:local` that adds the toolchain (`dotnet`, a JDK…) and
installs the pack.

### 2. Credentials

```bash
gh auth login                      # on the host: pushes branches and opens draft PRs
export ANTHROPIC_API_KEY=...       # for the agent in the container
export GH_READ_TOKEN=...           # optional, recommended: read-only token for the agent
```

- **Writing to GitHub** uses the host's `gh` login, outside the container. No write token is
  ever passed to the container; GitHub token names in `env_passthrough` are refused.
- **`ANTHROPIC_API_KEY`** (or `CLAUDE_CODE_OAUTH_TOKEN`) goes into the container by name, so
  its value never appears in a command line, a log or `config.toml`. Use a dedicated key with a
  spending limit.
- **`GH_READ_TOKEN`** reaches the container as `GH_TOKEN`, so the agent can clone a private
  repository and read the issue. Make it a fine-grained token for the allowed repositories with
  **read-only** contents and issues. Without it, only public repositories can be cloned.

Protect the base branch so changes can only arrive through a reviewed pull request.

### 3. Allow the repositories

```toml
# ~/.openjarvis/config.toml
[tools.coding_crew]
repos = ["your-user/your-app"]        # the only repositories it may touch
budget_usd = 5.0                      # per issue
timeout_minutes = 45
```

| Setting | Default | Meaning |
|---|---|---|
| `repos` | — | Allowlist. Issues in any other repository are refused. |
| `label` | `agent-ready` | Label a person adds to hand an issue to the crew. |
| `trusted_permissions` | `admin,maintain,write` | Whoever added the label must have one of these permissions on the repository. |
| `working_label`, `done_label`, `failed_label` | `agent-working`, `agent-done`, `agent-failed` | Created on first use. An issue with any of them is never picked. |
| `image` | `openjarvis-coding-crew:local` | Local image; never pulled. |
| `prompt` | `/cuadrilla:ticket #{issue}` | What the agent is asked; `{issue}` and `{repo}` are replaced. |
| `branch_prefix` | `agent/` | The only branches published. |
| `unattended_env` | `CUADRILLA_UNATTENDED=1` | Non-secret `NAME=value` pairs for the container. |
| `env_passthrough` | `ANTHROPIC_API_KEY` | Names of variables the container inherits. All must be set. |
| `read_token_env` | `GH_READ_TOKEN` | Optional read-only token, passed to the container as `GH_TOKEN`. |
| `model` | (agent default) | Model for the coding agent. |
| `budget_usd` | `5.0` | Spending cap per issue, enforced by the agent. |
| `timeout_minutes` | `45` | The container is killed after this. |
| `memory`, `cpus` | `4g`, `2` | Container limits (also `--pids-limit 1024`). |
| `max_bundle_mb` | `200` | Larger results are refused. |
| `comment` | `true` | Comment the outcome on the issue. |

### Tickets from Jira

With `source = "jira"` the crew takes Jira tickets instead of GitHub issues; the code and the
pull requests stay on GitHub.

```toml
[tools.coding_crew]
source = "jira"
jira_url = "https://your-site.atlassian.net"
jira_projects = "PROJ=your-user/your-app"     # Jira project key = GitHub repository
jira_trusted_accounts = "712020:…"            # account IDs that may hand tickets to the crew
jira_status_working = "In Progress"           # your workflow's status names
jira_status_review = "In Review"
jira_status_failed = "To Do"
```

```bash
export JIRA_EMAIL=you@example.com
export JIRA_API_TOKEN=...      # https://id.atlassian.com/manage-profile/security/api-tokens
```

- The **Jira token stays on the host**, like the GitHub login. The host reads the ticket and
  writes its text (description and comments) to `/out/ticket.md`; the agent reads that file.
  `jira_prompt` is what the agent is asked (`{key}`, `{ticket_file}` and `{repo}` are replaced).
- **Trusted start**: the account that last added the ready label (from the ticket's history)
  must be in `jira_trusted_accounts`. Find your account ID in your Jira profile URL.
- **States**: claimed → `agent-working` label and *In Progress*; draft PR → `agent-done`,
  *In Review* and a comment with the link; failure → `agent-failed`, back to *To Do* and a
  comment with the reason. A status your workflow can't reach from the current one is skipped.
- The queue is `project in (…) AND labels = "agent-ready" AND statusCategory != Done`, oldest
  first, skipping tickets with any crew label. `jira_projects` is the allowlist: `repos` is
  only used for GitHub issues.
- To use the agents and rules the repository ships in its own `.claude/` instead of a kit,
  build the image with `--build-arg KIT_PLUGINS=""`. The image includes `uv` and Python 3.12
  and 3.13 for Python projects.

### 4. Activate the operator

```bash
jarvis scheduler start
jarvis operators activate coding_crew     # every 15 minutes
jarvis operators run coding_crew          # or one tick now
```

Then label an issue `agent-ready`.

## Outcomes

| Label | Comment on the issue | What to do |
|---|---|---|
| `agent-done` | Link to the draft pull request, minutes, turns, cost, branch. | Review the PR like any other; merge or close it. |
| `agent-failed` | A fixed reason: no changes, spending or time limit, clone failed, label added by someone without write access… | Improve the issue (acceptance criteria help most), remove `agent-failed` and add `agent-ready` again. |

The comment carries fixed messages and validated links only. Raw container and agent output
stays in the tool result on your machine.

## Safety

- **No write token in the container.** The agent works with, at most, a read-only token. Its
  commits come back as a git bundle, imported by the host into a repository it creates itself
  (no hooks, object checks on), and only `agent/*` branches that don't exist yet are pushed.
  An agent misled by a malicious issue or comment can't push, open pull requests or leak a
  write token: there is none in reach.
- **Trusted start.** The ready label must have been added by someone with `write`, `maintain`
  or `admin` permission; otherwise the issue is marked `agent-failed` and nothing runs.
- **Allowlist.** Only repositories in `repos`; only open issues with the ready label and none of
  the crew's labels. References are validated before anything runs, and the operator is told to
  copy them exactly from the queue.
- **Disposable, hardened container.** Local image only, fresh clone, non-root user,
  `--cap-drop ALL`, `no-new-privileges`, memory/CPU/PID limits, killed at the timeout. Its only
  mount is an empty per-run output folder.
- **Never stuck.** Whatever happens, the issue ends as `agent-done` or `agent-failed`.
- **One run at a time** per process. The `agent-working` label makes a second machine skip a
  claimed issue, but the claim is not atomic: run one crew per repository.
- **Nothing merges.** The only writes are an `agent/*` branch, a draft PR, labels and a comment.

What remains in the container is the Anthropic credential and, if you set it, the read-only
token. Issue text and comments reach the agent; a malicious one could try to get it to send
them out. Use a dedicated API key with a spending limit and a read-only token scoped to the
allowed repositories, and keep the crew on repositories whose issues you trust.
