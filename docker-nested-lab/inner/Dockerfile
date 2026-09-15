# ─────────────────────────────────────────────────────────────────────────────
# INNER container — the web application at the very bottom of the nesting.
#
# Base: alpine (small, ~8 MB, has apk + a real shell). We install:
#   busybox-extras -> provides `httpd`, the tiny static web server
#   iptables       -> lets this image ALSO act as a NAT/relay container for the
#                     dynamic-port-range feature (see outer/entrypoint.sh)
#   curl, netcat   -> handy for poking around during experiments
#
#   GET /        -> index.html (styled by style.css)
#   GET /health  -> JSON {"status":"ok",...}
# ─────────────────────────────────────────────────────────────────────────────
FROM alpine:3.20

RUN apk add --no-cache busybox-extras iptables curl netcat-openbsd

WORKDIR /www
COPY index.html /www/index.html
COPY style.css  /www/style.css

# /health as a static JSON file (busybox httpd serves it directly).
RUN printf '%s\n' \
      '{"status":"ok","environment":"nested-docker","layer":"inner-container","server":"busybox-httpd"}' \
      > /www/health

EXPOSE 80

# -f : foreground, -p : port, -h : docroot
CMD ["httpd", "-f", "-p", "80", "-h", "/www"]
