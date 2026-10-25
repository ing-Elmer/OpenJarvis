#!/usr/bin/env bash
# Fake `gh` inside the container: only `auth setup-git` is used there.
echo "gh $*" >> "$FAKE_LOG"
case "$1 $2" in
  "auth setup-git") exit 0 ;;
  *) exit 1 ;;
esac
