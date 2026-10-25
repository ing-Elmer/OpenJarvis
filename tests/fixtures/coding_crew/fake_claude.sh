#!/usr/bin/env bash
# Fake `claude -p` inside the container. Logs its argv, prompt and the GitHub token it sees.
prompt="$(cat)"
printf 'claude %s :: %s :: GH_TOKEN=%s\n' "$*" "${prompt}" "${GH_TOKEN:-}" >> "$FAKE_LOG"
ok() { echo "{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,\"total_cost_usd\":$1,\"num_turns\":$2,\"result\":\"done\"}"; }
case "${FAKE_MODE}" in
  commit)
    git switch -q -c agent/12-fix-login
    echo "fixed" > fix.txt
    git add fix.txt && git commit -q -m "fix: login"
    if git push -q origin agent/12-fix-login 2>/dev/null; then echo "PUSHED" >> "$FAKE_LOG"; fi
    printf '## Summary\nFixes the login.\n' > "${CREW_OUT}/pr-body.md"
    ok 1.25 14 ;;
  uncommitted)
    echo "half done" > wip.txt
    ok 0.9 8 ;;
  onbase)
    echo "on main" > main.txt
    git add main.txt && git commit -q -m "fix on main"
    ok 0.7 6 ;;
  outside)
    git switch -q -c feature/not-agent
    echo "x" > x.txt && git add x.txt && git commit -q -m "x"
    ok 0.5 4 ;;
  jira)
    echo "ticket: $(head -n 1 "${CREW_OUT}/ticket.md" 2>/dev/null)" >> "$FAKE_LOG"
    git switch -q -c "agent/${CREW_ISSUE}-health-version"
    echo "version" > version.txt
    git add version.txt && git commit -q -m "feat: version in /health"
    printf '## Summary\nAdds the version.\n' > "${CREW_OUT}/pr-body.md"
    ok 0.8 9 ;;
  nochange) ok 0.4 3 ;;
  budget) echo '{"type":"result","subtype":"error_max_budget_usd","is_error":true,"total_cost_usd":5.1,"num_turns":30,"errors":["Reached maximum budget ($5)"]}' ;;
  garbage) echo "segfault" ;;
esac
