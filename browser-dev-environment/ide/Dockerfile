# Browser IDE image: code-server (VS Code in the browser) + developer tooling.
#
# This container IS the Linux development machine:
#   - VS Code UI served over HTTP (port 8080)
#   - integrated terminal = a real Linux shell
#   - git + python available for working on the company repo
FROM codercom/code-server:4.89.1

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       git curl ca-certificates \
       python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /usr/local/bin/dev-entrypoint.sh
RUN chmod +x /usr/local/bin/dev-entrypoint.sh

# code-server ships an unprivileged `coder` user (uid 1000). The workspace
# volume is chown'ed to this user by the repo-init job.
USER coder
WORKDIR /home/coder

ENTRYPOINT ["/usr/local/bin/dev-entrypoint.sh"]
