# Getting Started — Server Mode

Connect your HSYNC client to a central HSYNC server.

> Back to [Getting Started](getting-started.md).

## Basic Usage

```bash
# Single sync
hsync -s https://server.example.com:9753 -b mybucket -p /path/to/dir

# Continuous sync (watch mode)
hsync -s https://server.example.com:9753 -b mybucket -p /path/to/dir -w
```

> For local development with a self-signed certificate, add `--insecure` and
> use `https://` (the client refuses to send API keys over plain HTTP).

## Try it end-to-end (local)

The fastest way to see HSYNC in action is to run the server and a client on the
same machine. You need the [server](https://github.com/albertobasso/hsync-server)
running locally (Docker or Poetry) with `HSYNC_API_KEY` set.

```bash
# 1. Start the server (see the server docs for Docker/Poetry setup)
#    It listens on https://localhost:9753 with a self-signed cert.

# 2. Create two empty folders to sync
mkdir -p /tmp/hsync-a /tmp/hsync-b

# 3. Put a file in the first folder
echo "hello hsync" > /tmp/hsync-a/hello.txt

# 4. Sync folder A → server (one-shot, self-signed cert)
hsync -s https://localhost:9753 -b demo -p /tmp/hsync-a --insecure

# 5. Sync server → folder B (one-shot, self-signed cert)
hsync -s https://localhost:9753 -b demo -p /tmp/hsync-b --insecure

# 6. Verify the file arrived
cat /tmp/hsync-b/hello.txt   # → hello hsync
```

For continuous, real-time sync, add `-w` to both commands and edit files in
either folder — changes propagate within seconds.

## Environment Configuration

Set the required variables in your shell or export them in a `.env` file:

```bash
export HSYNC_SERVER="https://server.example.com:9753"
export HSYNC_API_KEY="your-api-key"
```

See [Configuration — Server Mode](configuration-server.md) for all server-mode options.

## Next Steps

- [Server Mode Guide](server-mode.md) — detailed walkthrough
- [Configuration — Server Mode](configuration-server.md) — all server-mode CLI options
- [Authentication — Client-Side](authentication.md) — auth flow diagrams
- [Examples — Server Mode](examples-server.md) — practical recipes
