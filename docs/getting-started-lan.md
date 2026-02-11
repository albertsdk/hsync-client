# Getting Started — LAN P2P Mode

Sync files directly between devices on your local network — no server needed.

> Back to [Getting Started](getting-started.md).

## Basic Usage

```bash
# Peers discover each other via mDNS; continuous mode is implicit
hsync -b mybucket -p /path/to/dir --lan-mode
```

See [LAN Mode](lan-mode.md) for the P2P architecture and master election.

## Try it end-to-end (local)

The fastest way to see LAN P2P sync in action is to run two clients on the same
machine (or two devices on the same network). No server, no API key — peers
discover each other via mDNS.

Open **two terminals** and run:

```bash
# 1. Create two empty folders to sync
mkdir -p /tmp/hsync-lan-a /tmp/hsync-lan-b

# 2. Put a file in the first folder
echo "hello hsync lan" > /tmp/hsync-lan-a/hello.txt

# 3. Terminal 1 — force this peer to be the master (it already holds the file,
#    so no deletion-risk warning is shown) and start LAN Mode. Give it its own
#    device identity: the two peers run on the same machine but must be two
#    distinct devices, or discovery would ignore them as "self".
HSYNC_DEVICE_DIR=/tmp/hsync-lan-a-device \
  hsync -b demo-lan -p /tmp/hsync-lan-a --lan-mode --force-master

# 4. Terminal 2 — start a second peer with its own device identity; it
#    discovers the master via mDNS and downloads hello.txt
HSYNC_DEVICE_DIR=/tmp/hsync-lan-b-device \
  hsync -b demo-lan -p /tmp/hsync-lan-b --lan-mode

# 5. Verify the file arrived
cat /tmp/hsync-lan-b/hello.txt   # → hello hsync lan
```

Continuous mode is **implicit** in LAN Mode: edit or create a file in either folder
and the change propagates to the other peer in real time. Press `Ctrl+C` in
both terminals to stop.

> **Why `--force-master` here?** The first peer already holds the file, so
> electing it as master avoids the initial
> deletion-risk warning on a non-empty directory. The master is only the
> transport hub — it holds no special authority over file contents. On a real
> LAN with multiple devices, omit `--force-master` and let master election
> happen automatically
> (see [LAN Mode → Master Election](lan-mode.md#master-election)).
>
> **Why the `HSYNC_DEVICE_DIR` lines?** Every installation owns one persistent
> device identity (a stable device ID) stored in `~/.local/state/hsync/device/`.
> With two peers on the *same* machine, both terminals would otherwise share one
> identity and ignore each other as "self" during mDNS discovery — so each
> terminal gets its own device directory here. On real devices this is not
> needed: omit the variable and the default is used. See
> [LAN Mode → Device Identity](lan-mode.md#device-identity).

## What to expect in LAN Mode

The first time you run `--lan-mode` on a non-empty directory, the client prints
a **deletion-risk warning** and asks for confirmation: files that exist only on
this peer (and not on the elected master) are deleted during the initial sync.
This is intentional — it lets deletions made on the master reach offline peers.

- Use `-y` to skip the prompt in scripts/services.
- Use `--sync-direction upload-only` if you never want local files deleted.
- Use `--force-master` on the device that already holds the data.

See [LAN Mode → Deletion Risk Warning](lan-mode.md#deletion-risk-warning) for
the full explanation.

## Authentication (optional)

For added security, set a shared `LAN_KEY` on all devices:

```bash
export HSYNC_LAN_KEY="your-shared-secret"
```

With a `LAN_KEY`:
- The master authenticates every slave connection
- The CID is cryptographically verified — used for echo suppression and log attribution
- Without `LAN_KEY`, a lightweight **hello handshake** lets the master log each
  slave's CID with an `(unauthenticated)` qualifier

See [Authentication — Client-Side](authentication.md) for diagrams of both flows.

## Environment Configuration

Set the shared secret in the shell (optional) and enable LAN on the CLI.
`HSYNC_LAN_MODE` is not read.

```bash
export HSYNC_LAN_KEY="shared-secret"   # optional, recommended
export HSYNC_LAN_GROUP="mygroup"       # optional, isolates peer groups
hsync -b documents -p ~/Documents --lan-mode
```

See [Configuration — LAN P2P Mode](configuration-lan.md) for all LAN-mode options.

## Next Steps

- [LAN Mode Guide](lan-mode.md) — detailed walkthrough
- [Configuration — LAN P2P Mode](configuration-lan.md) — all LAN-mode CLI options
- [Authentication — Client-Side](authentication.md) — hello handshake + auth diagrams
- [Examples — LAN P2P Mode](examples-lan.md) — practical recipes
