"""A deterministic stand-in for 'the internet' used by the egress tests.

Runs INSIDE dind on its own network (sbx-upstream) which the egress proxy
joins and sandboxes cannot reach directly. Serves HTTP on 80 and HTTPS on
443 (self-signed; the proxy verifies with ssl_insecure in this lab).

  GET  /            -> "upstream-ok <counter>" (counter proves replay vs live)
  GET  /headers     -> JSON echo of request headers (secret injection test)
  POST /echo        -> echoes the body
  GET  /secret      -> body contains the REAL token (scrub test)
"""
import http.server
import json
import os
import ssl
import threading

COUNTER = {"n": 0}
REAL = os.environ.get("REAL_TOKEN", "real-token-XYZ")


class H(http.server.BaseHTTPRequestHandler):
    def _send(self, code, body: bytes, ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            COUNTER["n"] += 1
            self._send(200, f"upstream-ok {COUNTER['n']}\n".encode())
        elif self.path == "/headers":
            self._send(200, json.dumps({k: v for k, v in self.headers.items()}).encode(), "application/json")
        elif self.path == "/secret":
            self._send(200, f"the token is {REAL}\n".encode())
        elif self.path == "/auth":
            # 200 only if the REAL token arrived: proves placeholder injection
            if self.headers.get("Authorization") == f"Bearer {REAL}":
                self._send(200, b"authorized\n")
            else:
                self._send(401, b"unauthorized\n")
        else:
            self._send(404, b"nope\n")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        self._send(200, b"echo:" + body)

    def log_message(self, *a):
        pass


def serve(port, tls):
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", port), H)
    if tls:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain("/certs/cert.pem", "/certs/key.pem")
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    srv.serve_forever()


if __name__ == "__main__":
    threading.Thread(target=serve, args=(80, False), daemon=True).start()
    serve(443, True)
