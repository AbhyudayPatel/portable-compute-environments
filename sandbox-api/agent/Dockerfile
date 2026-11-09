# Builds the sbx-agent static binary. Output stage is scratch with ONLY the
# binary, so other images can `COPY --from=sbx-agent:local /sbx-agent`.
FROM golang:1.23-alpine AS build
WORKDIR /src
COPY go.mod ./
COPY *.go ./
RUN go mod tidy && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build -trimpath -ldflags="-s -w" -o /out/sbx-agent . \
    && /out/sbx-agent version

FROM scratch
COPY --from=build /out/sbx-agent /sbx-agent
