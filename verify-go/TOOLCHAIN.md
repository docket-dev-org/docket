# Toolchain

| what | value |
| --- | --- |
| Go | `go1.27.2 linux/amd64` |
| download | `https://go.dev/dl/go1.27.2.linux-amd64.tar.gz` |
| sha256 | `ecbadb99091a3f46e31f5f934b068b1864eafa7995211b39eaddf76996045fe5` |
| size | 70,590,635 bytes |
| digest source | `https://go.dev/dl/?mode=json`, fetched 2026-10-17T12:36Z |
| extracted to | `/usr/local/go` (outside the repository; nothing in `verify-go/` depends on the path) |
| modules | none — `go list -m all` prints only `github.com/docket-dev-org/docket/verify-go` |

The digest is checked before extraction:

```console
$ curl -fsSLo go.tgz https://go.dev/dl/go1.27.2.linux-amd64.tar.gz
$ echo "ecbadb99091a3f46e31f5f934b068b1864eafa7995211b39eaddf76996045fe5  go.tgz" | sha256sum -c -
$ tar -C /usr/local -xzf go.tgz && /usr/local/go/bin/go version
go version go1.27.2 linux/amd64
```

`make verify-go` runs `go vet`, `gofmt -l` and `go test` with `GOPROXY=off`, so a machine without
a module proxy (or without a network) builds exactly what CI builds.
