# Minimal git tooling image, used by:
#   - gitserver  (runs `git daemon`, hosts the company repo)
#   - repo-init  (one-shot clone into the workspace volume)
FROM alpine:3.20

RUN apk add --no-cache git git-daemon

COPY gitserver-entrypoint.sh /usr/local/bin/gitserver-entrypoint.sh
RUN chmod +x /usr/local/bin/gitserver-entrypoint.sh
