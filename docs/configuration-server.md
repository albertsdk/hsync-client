# Configuration — Server Mode

Server-mode configuration for connecting to a central HSYNC Server.

> For shared options (sync, compression, delta, logging), see [Configuration](configuration.md).
> For p2p options, see [Configuration — p2p Mode](configuration-p2p.md).

## Server Connection Options

| Option | Description | Default |
|---|---|---|
| `--server-url` | Sync-server URL: a full `https://…[/path]` URL or a bare `HOST[:PORT]` (HTTPS implied, default port `9753`); required in server mode unless `HSYNC_SERVER_URL` is set | `HSYNC_SERVER_URL` |
| `-b, --bucket` | Bucket name | (required; CLI only) |
| `--ca-cert` | Path to CA certificate for self-signed server certs | — |
| `--server-trust` | TLS trust policy: `chain` (default), `ask`, `accept-new`, `strict`, `none` | `chain` |
| `--server-fingerprint` | Pin the server certificate fingerprint (`sha256:<hex>` or bare hex; repeat up to two for a rotation window) | — |
| `--server-tofu` | Trust the server certificate on first use (TOFU) | `false` |
| `--insecure` | Skip SSL certificate verification | `false` |

There is **no** `--api-key` flag. The account token is read from the
`HSYNC_API_KEY` environment variable. The desktop GUI (and other parent
processes) may pass it on a pipe with the hidden `--api-key-fd` flag; that
option is not part of the interactive CLI.

## Server certificate trust

By default the client validates the server certificate with the standard CA
chain (`chain`); a reverse proxy with a public-CA certificate works with no extra
configuration. For a self-signed server (the `hsync-server` built-in TLS, common
on a LAN) choose one of the SSH-style policies:

| Policy | Behaviour |
|---|---|
| `chain` | Standard CA validation; no fingerprint pinning (default) |
| `ask` | Show the fingerprint and ask on first contact; fail closed without a terminal |
| `accept-new` | Pin the first sighting silently (Trust-On-First-Use) |
| `strict` | Require an out-of-band pin; refuse an unknown or changed certificate |
| `none` | No verification (`--insecure`; loopback/tests only) |

A changed fingerprint is always refused as a possible man-in-the-middle. Pins
live in the known-hosts store and are managed like the bootnode store:

```bash
hsync trust server fingerprint server.example.com
hsync trust server add server.example.com sha256:FINGERPRINT
hsync trust server list
hsync trust server remove server.example.com
```

`hsync-server admin tls fingerprint` prints the server's own fingerprint for the
out-of-band step.

## Environment Variables — Server Mode

| Variable | Description | Default |
|---|---|---|
| `HSYNC_SERVER_URL` | Sync-server URL: bare `host`, `host:port`, `[IPv6]`, `[IPv6]:port`, or a full `https://…[/path]` URL (same as `--server-url`) | (required or pass `--server-url`) |
| `HSYNC_API_KEY` | Account token (`hs_<id>_<secret>`) | (required in server mode) |
| `HSYNC_SERVER_TRUST` | TLS trust policy (`chain`/`ask`/`accept-new`/`strict`/`none`) | `chain` |
| `HSYNC_SERVER_FINGERPRINT` | Certificate fingerprint(s) to pin (up to two) | — |
| `HSYNC_SERVER_TOFU` | Trust the certificate on first use | `false` |
| `HSYNC_SERVER_TRUST_FILE` | File storing the trusted server fingerprints | `$HSYNC_STATE_DIR/server_known_hosts.json`; the XDG/local-state path when the state dir is unset |
| `HSYNC_DELTA_MIN_FILE_SIZE` | Min file size for delta transfer in server mode | `262144` (256 KB) |

Bucket, local path, sync mode (`--once`), and sync direction are **CLI flags**, not
environment variables. Putting `HSYNC_BUCKET` in an env file has no effect.

## Environment files

The client does **not** load `./.env` by itself. Pass a file explicitly:

```bash
export HSYNC_API_KEY="hs_ACCOUNT_ID_ACCOUNT_SECRET"
hsync server --env-file ./hsync.env -b documents -p "$HOME/Documents"
```

With no `--server-url` on the command line the client reads the server URL from
`HSYNC_SERVER_URL` (set it in the env file, or `export` it in the shell). If
neither is provided, the client exits immediately with a usage error rather
than starting a connection attempt.

`--env-file` only applies `HSYNC_*` keys recognised by the client settings
model (for server mode: `HSYNC_SERVER_URL`, `HSYNC_API_KEY`, plus the shared
keys in [Configuration](configuration.md)). Alternatively, `export` the
variables in the shell (or `source` a file) before running `hsync`.

## See also

- [Configuration](configuration.md) — shared options (sync, compression, delta, ignore, logging)
- [Server Mode Guide](server-mode.md) — detailed walkthrough
- [Authentication — Client-Side](authentication.md) — auth flow diagrams
