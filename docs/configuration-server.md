# Configuration — Server Mode

Server-mode configuration for connecting to a central HSYNC Server.

> For shared options (sync, compression, delta, logging), see [Configuration](configuration.md).
> For LAN P2P options, see [Configuration — LAN P2P Mode](configuration-lan.md).

## Server Connection Options

| Option | Description | Default |
|---|---|---|
| `-s, --server` | Server URL (`https://…`; required in server mode) | `HSYNC_SERVER` |
| `-b, --bucket` | Bucket name | (required; CLI only) |
| `--ca-cert` | Path to CA certificate for self-signed server certs | — |
| `--insecure` | Skip SSL certificate verification | `false` |

There is **no** `--api-key` flag. The account token is read from the
`HSYNC_API_KEY` environment variable. The desktop GUI (and other parent
processes) may pass it on a pipe with the hidden `--api-key-fd` flag; that
option is not part of the interactive CLI.

## Environment Variables — Server Mode

| Variable | Description | Default |
|---|---|---|
| `HSYNC_SERVER` | Server URL (alternative to `-s`) | (required or use `-s`) |
| `HSYNC_API_KEY` | Account token (`hs_<id>_<secret>`) | (required in server mode) |

Bucket, local path, watch mode, and sync direction are **CLI flags**, not
environment variables. Putting `HSYNC_BUCKET` or `HSYNC_WATCH` in an env
file has no effect.

## Environment files

The client does **not** load `./.env` by itself. Pass a file explicitly:

```bash
export HSYNC_API_KEY="hs_…"
hsync --env-file ./hsync.env -s https://hsync.example.com:9753 -b documents -p "$HOME/Documents" -w
```

`--env-file` only applies `HSYNC_*` keys recognised by the client settings
model (for server mode: `HSYNC_SERVER`, `HSYNC_API_KEY`, plus the shared
keys in [Configuration](configuration.md)). Alternatively, `export` the
variables in the shell (or `source` a file) before running `hsync`.

## See also

- [Configuration](configuration.md) — shared options (sync, compression, delta, ignore, logging)
- [Server Mode Guide](server-mode.md) — detailed walkthrough
- [Authentication — Client-Side](authentication.md) — auth flow diagrams
