"""Stateful fake `gh` on the host, for the coding crew tool tests."""

import json
import os
import sys

state_file = os.environ["FAKE_GH_STATE"]
state = json.load(open(state_file))
args = sys.argv[1:]
stdin = sys.stdin.read() if "--body-file" in args else ""
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(json.dumps({"args": args, "stdin": stdin}) + "\n")


def opt(name):
    return args[args.index(name) + 1] if name in args else None


def save():
    json.dump(state, open(state_file, "w"))


repo = opt("-R")
if args[:2] == ["issue", "list"]:
    if repo in state.get("broken", []):
        print("HTTP 404", file=sys.stderr)
        sys.exit(1)
    label = opt("--label")
    issues = [
        dict(i, number=int(n))
        for n, i in state["issues"].get(repo, {}).items()
        if i["state"] == "OPEN" and label in [lb["name"] for lb in i["labels"]]
    ]
    print(json.dumps(issues))
elif args[:2] == ["issue", "view"]:
    issue = state["issues"].get(repo, {}).get(args[2])
    if issue is None:
        print("not found", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(issue))
elif args[:2] == ["issue", "edit"]:
    issue = state["issues"][repo][args[2]]
    names = [lb["name"] for lb in issue["labels"]]
    for i, a in enumerate(args):
        if a == "--add-label" and args[i + 1] not in names:
            names.append(args[i + 1])
        if a == "--remove-label" and args[i + 1] in names:
            names.remove(args[i + 1])
    issue["labels"] = [{"name": n} for n in names]
    save()
elif args[:2] in (["issue", "comment"], ["label", "create"]):
    pass
elif args[0] == "api":
    path = next(a for a in args[1:] if a.startswith("repos/"))
    parts = path.split("/")
    full = f"{parts[1]}/{parts[2]}"
    if parts[3] == "issues" and parts[5] == "events":
        for login, label in state.get("labeled_by", {}).get(f"{full}#{parts[4]}", []):
            print(f"{login}\t{label}")
    elif parts[3] == "collaborators":
        print(state.get("permissions", {}).get(parts[4], "none"))
    else:
        sys.exit(1)
elif args[:2] == ["repo", "view"]:
    print(state.get("default_branch", "main"))
elif args[:2] == ["pr", "create"]:
    if state.get("pr_fails"):
        print("GraphQL: something went wrong", file=sys.stderr)
        sys.exit(1)
    print("https://github.com/acme/app/pull/40")
elif args[:2] == ["auth", "git-credential"]:
    sys.exit(0)
else:
    sys.exit(2)
