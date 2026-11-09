# The fake internet for egress tests (built inside dind by sandbox-api).
FROM python:3.12-alpine
RUN apk add --no-cache openssl && mkdir -p /certs && \
    openssl req -x509 -newkey rsa:2048 -nodes -keyout /certs/key.pem -out /certs/cert.pem \
      -days 3650 -subj "/CN=upstream.test" -addext "subjectAltName=DNS:upstream.test"
COPY server.py /server.py
EXPOSE 80 443
CMD ["python", "/server.py"]
