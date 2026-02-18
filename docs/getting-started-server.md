# Getting Started — Server Mode

Connect your HSYNC client to a central HSYNC server.

> Back to [Getting Started](getting-started.md).

## Basic Usage

```bash
# Single sync (one-shot)
hsync --once -s https://server.example.com:9753 -b mybucket -p /path/to/dir

# Continuous sync (the default: keeps retrying if the server is unreachable)
hsync server -s https://server.example.com:9753 -b mybucket -p /path/to/dir
```

> For local development with a self-signed certificate, add `--insecure` and
> use `https://` (the client refuses to send API keys over plain HTTP).

## Try it end-to-end (local)

The fastest way to see HSYNC in action is to run the server and a client on the
same machine. You need the [server](https://github.com/albertobasso/hsync-server)
running locally (Docker or Poetry). At first start the server bootstraps an
admin account and prints its full token once — export it as `HSYNC_API_KEY`.

```bash
# 1. Start the server (see the server docs for Docker/Poetry setup)
#    It listens on https://localhost:9753 with a self-signed cert.

# 2. Export the account token printed by the server at first start
export HSYNC_API_KEY="hs_HsyncAdminBootstrap01_<secret>"

# 3. Create the bucket on the server (once; the creator becomes its owner)
curl -k -X POST "https://localhost:9753/api/v1/buckets?bucket=demo" \
  -H "Authorization: Bearer $HSYNC_API_KEY"

# 4. Create two empty folders to sync
mkdir -p /tmp/hsync-a /tmp/hsync-b

# 5. Put a file in the first folder
echo "hello hsync" > /tmp/hsync-a/hello.txt

# 6. Sync folder A → server (one-shot, self-signed cert)
hsync --once -s https://localhost:9753 -b demo -p /tmp/hsync-a --insecure

# 7. Sync server → folder B (one-shot, self-signed cert)
hsync --once -s https://localhost:9753 -b demo -p /tmp/hsync-b --insecure

# 8. Verify the file arrived
cat /tmp/hsync-b/hello.txt   # → hello hsync
```

For continuous, real-time sync, omit `--once` (the default) and edit files in
either folder — changes propagate within seconds.

## Environment Configuration

Set the required variables in your shell or export them in a `.env` file:

```bash
export HSYNC_SERVER="https://server.example.com:9753"
export HSYNC_API_KEY="hs_<id>_<secret>"
```

`HSYNC_API_KEY` holds the **full account token** (`hs_<id>_<secret>`) issued by
the server — at bootstrap for the admin account, or with
`hsync-server admin client add --role client` for dedicated accounts (see the
server [Getting Started](https://github.com/albertobasso/hsync-server/blob/main/docs/getting-started.md)
guide). `HSYNC_ADMIN_KEY` is a server-side bootstrap secret and has no meaning
on the client.

See [Configuration — Server Mode](configuration-server.md) for all server-mode options.

## Next Steps

- [Server Mode Guide](server-mode.md) — detailed walkthrough
- [Configuration — Server Mode](configuration-server.md) — all server-mode CLI options
- [Authentication — Client-Side](authentication.md) — auth flow diagrams
- [Examples — Server Mode](examples-server.md) — practical recipes
