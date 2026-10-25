# Coding crew image: a disposable environment where a coding agent solves one GitHub issue.
# Used by the coding_crew_run tool (docs/user-guide/coding-crew.md). It holds no credentials,
# and no token that can write to GitHub is ever passed to it: the host pushes the result.
#
#   docker build -t openjarvis-coding-crew:local --build-arg KIT_REF=<commit> deploy/coding-crew
#
# The tool runs it with `--pull never`, so it only ever uses the image you built.
# Toolchains for other stacks (dotnet, java…) go in an image built FROM this one.

# Same pinned base as deploy/docker/Dockerfile's frontend stage (#563).
FROM node:22.23.0-slim@sha256:d9f850096136edbc402debdd8729579a288aac64574ada0ff4db26b6ae58b0b2

ARG CLAUDE_CODE_VERSION=2.1.310
# The kit the agents work with: any Claude Code plugin marketplace. Pin KIT_REF to a commit
# or tag so the image is reproducible and a later change to the kit can't reach it unreviewed.
ARG KIT_REPO=https://github.com/ing-Elmer/cuadrilla.git
ARG KIT_REF=main
ARG KIT_MARKETPLACE_NAME=cuadrilla
ARG KIT_PLUGINS="cuadrilla cuadrilla-node-typescript cuadrilla-react cuadrilla-python-fastapi"
# The kit's core plugin takes the language the agents write in (answers, commits, PR text).
ARG KIT_CORE_PLUGIN=cuadrilla
ARG KIT_LANGUAGE=English

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl git jq python3 python3-venv && \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list && \
    apt-get update && apt-get install -y --no-install-recommends gh && \
    rm -rf /var/lib/apt/lists/*

RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" && npm cache clean --force

# The kit is fetched once, at the pinned ref, and owned by root: the agent can read it, not change it.
RUN git clone --quiet "${KIT_REPO}" /opt/kit && \
    git -C /opt/kit checkout --quiet "${KIT_REF}" && \
    rm -rf /opt/kit/.git

RUN useradd --create-home --shell /bin/bash crew && mkdir -p /work /out && chown crew:crew /work
COPY --chown=root:root --chmod=0755 crew-run.sh /usr/local/bin/crew-run
USER crew
WORKDIR /work

RUN claude plugin marketplace add /opt/kit && \
    for plugin in ${KIT_PLUGINS}; do \
      if [ "${plugin}" = "${KIT_CORE_PLUGIN}" ]; then \
        claude plugin install "${plugin}@${KIT_MARKETPLACE_NAME}" --config "response_language=${KIT_LANGUAGE}" || exit 1; \
      else \
        claude plugin install "${plugin}@${KIT_MARKETPLACE_NAME}" || exit 1; \
      fi; \
    done

ENTRYPOINT ["/usr/local/bin/crew-run"]
