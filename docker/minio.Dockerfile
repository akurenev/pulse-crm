# syntax=docker/dockerfile:1.7

# Build the official source releases: historical MinIO images are no longer
# available for anonymous pulls. Keep checksum verification enabled in Go.
FROM golang:1.26.5-alpine@sha256:0178a641fbb4858c5f1b48e34bdaabe0350a330a1b1149aabd498d0699ff5fb2 AS build

ENV CGO_ENABLED=0 \
    GOTOOLCHAIN=local \
    GOPROXY=https://proxy.golang.org,direct \
    GOSUMDB=sum.golang.org

RUN apk add --no-cache ca-certificates git

FROM build AS minio-build
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    go install -trimpath -ldflags="-s -w" \
    github.com/minio/minio@v0.0.0-20251015172955-9e49d5e7a648

FROM build AS mc-build
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    go install -trimpath -ldflags="-s -w" \
    github.com/minio/mc@v0.0.0-20250813083541-7394ce0dd2a8

FROM alpine:3.23@sha256:85fe1e81d6758c208f3e1eed4338a1997e19d4be002d4dd32d3100c9a8c010a0 AS runtime
RUN apk add --no-cache ca-certificates curl

FROM runtime AS minio
COPY --from=minio-build /go/bin/minio /usr/local/bin/minio
EXPOSE 9000 9001
VOLUME ["/data"]
ENTRYPOINT ["/usr/local/bin/minio"]
CMD ["server", "/data"]

FROM runtime AS mc
COPY --from=mc-build /go/bin/mc /usr/local/bin/mc
ENTRYPOINT ["/usr/local/bin/mc"]
CMD ["--help"]
