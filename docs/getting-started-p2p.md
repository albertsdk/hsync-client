# Getting Started — p2p Mode

Sync files directly between devices — on the same local network (mDNS, always
active) and, across networks, through a bootnode — with no server needed.

> Back to [Getting Started](getting-started.md).

## Basic Usage

```bash
# Peers discover each other via mDNS; continuous mode is implicit.
# Write-capable runs need a shared key (see Authentication below).
HSYNC_P2P_PASSPHRASE="a-long-shared-password" hsync p2p -b mybucket -p /path/to/dir
```

See [p2p Mode](p2p-mode.md) for the P2P architecture and master election.

## Try it end-to-end (local)

The fastest way to see p2p sync in action is to run two clients on the same
machine (or two devices on the same network). No server and no API key — peers
discover each other via mDNS and authorize writes with a shared p2p key (a
keyless p2p is read-only by default).

Open **two terminals** and run:

```bash
# 1. Create two empty folders to sync
mkdir -p /tmp/hsync-p2p-a /tmp/hsync-p2p-b

# 2. Put a file in the first folder
echo "hello hsync p2p" > /tmp/hsync-p2p-a/hello.txt

# 3. Terminal 1 — prefer this peer as the master (it already holds the file,
#    so no deletion-risk warning is shown) and start p2p Mode. Give it its own
#    device identity: the two peers run on the same machine but must be two
#    distinct devices, or discovery would ignore them as "self".
#    Both terminals share the same p2p key: it authorizes writes, and a
#    write-capable run without a key is refused with [AUTH REQUIRED].
HSYNC_P2P_PASSPHRASE="demo-shared-password" \
HSYNC_DEVICE_DIR=/tmp/hsync-p2p-a-device \
  hsync p2p -b demo-p2p -p /tmp/hsync-p2p-a --prefer-master

# 4. Terminal 2 — start a second peer with its own device identity; it
#    discovers the master via mDNS and downloads hello.txt
HSYNC_P2P_PASSPHRASE="demo-shared-password" \
HSYNC_DEVICE_DIR=/tmp/hsync-p2p-b-device \
  hsync p2p -b demo-p2p -p /tmp/hsync-p2p-b

# 5. Verify the file arrived
cat /tmp/hsync-p2p-b/hello.txt   # → hello hsync p2p
```

Continuous mode is **implicit** in p2p Mode: edit or create a file in either folder
and the change propagates to the other peer in real time. Press `Ctrl+C` in
both terminals to stop.

> **Why `--prefer-master` here?** The first peer already holds the file, so
> electing it as master avoids the initial
> deletion-risk warning on a non-empty directory. The master is only the
> transport hub — it holds no special authority over file contents. On a real
> LAN with multiple devices, omit `--prefer-master` and let master election
> happen automatically
> (see [p2p Mode → Master Election](p2p-mode.md#master-election)).
>
> **Why the `HSYNC_DEVICE_DIR` lines?** Every installation owns one persistent
> device identity (a stable device ID) stored in `$XDG_STATE_HOME/hsync/device/`
> (or `~/.local/state/hsync/device/` when `XDG_STATE_HOME` is unset).
> With two peers on the *same* machine, both terminals would otherwise share one
> identity and ignore each other as "self" during mDNS discovery — so each
> terminal gets its own device directory here. On real devices this is not
> needed: omit the variable and the default is used. See
> [p2p Mode → Device Identity](p2p-mode.md#device-identity).

## What to expect in p2p Mode

The first time you run `hsync p2p` on a non-empty directory, the client prints
a **deletion-risk warning** and asks for confirmation: files that exist only on
this peer (and not on the elected master) are deleted during the initial sync.
This is intentional — it lets deletions made on the master reach offline peers.

- Use `--allow-deletions` to accept the deletion-risk confirmation in scripts/services.
- Use `--sync-direction upload-only` if you never want local files deleted.
- Use `--prefer-master` on the device that already holds the data.

See [p2p Mode → Deletion Risk Warning](p2p-mode.md#deletion-risk-warning) for
the full explanation.

## Authentication (shared password)

The shared secret is what authorizes **writes**: set the same
`HSYNC_P2P_PASSPHRASE` (at least 12 characters; the client derives the group key)
or the same raw `HSYNC_P2P_KEY` (at least 32 characters) on every device that may
modify the shared folder.

```bash
export HSYNC_P2P_PASSPHRASE="your-shared-password"
```

With a shared secret:
- The master authenticates every slave connection and accepts mutations
- The CID is cryptographically verified — used for echo suppression and log attribution

Without a shared secret the group is **read-only**: discovery, listings, downloads
and read-only deltas keep working (a lightweight **hello handshake** lets the
master log each slave's CID with an `(unauthenticated)` qualifier), but any
write-capable sync is refused with `[AUTH REQUIRED]`. The only exceptions are
`--sync-direction download-only` and the deliberate, dangerous
`--allow-unauthenticated-writes` opt-in. A keyless run also asks for confirmation
at start-up (or requires `--allow-unauthenticated-p2p`) and fails closed when no
terminal is available.

See [Authentication — Client-Side](authentication.md) for diagrams of both flows.

## Environment Configuration

Set the shared secret in the shell (required for writes) and enable p2p mode on
the CLI. `HSYNC_P2P_MODE` is not read.

```bash
export HSYNC_P2P_PASSPHRASE="a-long-shared-password"   # required for writes (read-only without it)
hsync p2p -b documents -p ~/Documents --group mygroup  # --group is optional
```

See [Configuration — p2p Mode](configuration-p2p.md) for all p2p-mode options.

## Crossing networks

Devices on different networks cannot see each other over mDNS, so point every
device of the group at the same bootstrap node. mDNS stays active on the same
client, so same-network peers are still found locally:
[Sync across networks (bootnode)](p2p-bootnode.md).

## Next Steps

- [Sync on the local network](p2p-lan.md) — operate and verify the mDNS plan
- [Sync across networks](p2p-bootnode.md) — discover peers through a bootnode
- [p2p Mode Guide](p2p-mode.md) — detailed walkthrough
- [Configuration — p2p Mode](configuration-p2p.md) — all p2p-mode CLI options
- [Authentication — Client-Side](authentication.md) — hello handshake + auth diagrams
- [Examples — p2p Mode](examples-p2p.md) — practical recipes
