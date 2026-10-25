"""Fake `docker` for the coding crew tool tests.

`run` behaves like the real container: it runs deploy/coding-crew/crew-run.sh with the
environment docker would give it (`-e K=V`, and `-e K` inherited from this process), the
`--volume <host>:/out` folder as CREW_OUT, a local origin to clone, and the fake claude.
FAKE_DOCKER_MODE=sleep|silent simulate a hang and a missing image.
"""

import json
import os
import subprocess
import sys
import tempfile
import time

args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
    log.write(
        json.dumps(
            {
                "args": args,
                "env": {
                    k: os.environ.get(k)
                    for k in ("ANTHROPIC_API_KEY", "GH_TOKEN", "GH_READ_TOKEN")
                },
            }
        )
        + "\n"
    )
if args[0] == "kill":
    sys.exit(0)
mode = os.environ.get("FAKE_DOCKER_MODE", "script")
if mode == "sleep":
    time.sleep(30)
if mode == "silent":
    print(
        "Unable to find image 'openjarvis-coding-crew:local' locally", file=sys.stderr
    )
    sys.exit(125)

env = {
    "PATH": os.environ["FAKE_CONTAINER_PATH"],
    "HOME": tempfile.mkdtemp(),
    "FAKE_LOG": os.environ["FAKE_CONTAINER_LOG"],
    "FAKE_MODE": os.environ.get("FAKE_MODE", "commit"),
}
for k in (
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
):
    env[k] = os.environ[k]
i = 0
while i < len(args):
    if args[i] == "-e":
        key, eq, value = args[i + 1].partition("=")
        if eq:
            env[key] = value
        elif key in os.environ:
            env[key] = os.environ[key]
        i += 2
    elif args[i] == "--volume":
        env["CREW_OUT"] = args[i + 1].rsplit(":/out", 1)[0]
        i += 2
    else:
        i += 1
env["CREW_CLONE_URL"] = os.environ["FAKE_ORIGIN"]
env["CREW_WORKDIR"] = tempfile.mkdtemp(prefix="crew-work-")
proc = subprocess.run(
    ["bash", os.environ["FAKE_CREW_SCRIPT"]], env=env, capture_output=True, text=True
)
sys.stdout.write("cloning...\n" + proc.stdout)
sys.stderr.write(proc.stderr)
sys.exit(proc.returncode)
