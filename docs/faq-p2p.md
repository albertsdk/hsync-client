# FAQ — p2p Mode

> For Server Mode FAQ, see [FAQ — Server Mode](faq-server.md).
> For common questions (including resetting a folder's sync state), see
> [FAQ — Common](faq.md).

## General

**Q: What is HSYNC p2p Mode?**
p2p Mode lets devices sync files directly without a central server — over the
local network (mDNS) or, across networks, through an optional bootnode. One
device is elected **master** and serves files to all other devices (**slaves**).

**Q: Is the server required for HSYNC to work?**
No. p2p Mode works without a server. The server is only needed if you
want to sync devices across different networks. Writes are authorized by a
shared `HSYNC_P2P_KEY` — see [Security & Safety](#security--safety).

**Q: Can I use both Server Mode and p2p Mode at the same time?**
No. Each HSYNC instance operates in one mode at a time.

## Security & Safety

**Q: How does HSYNC keep my files safe in p2p mode?**
- All traffic uses HTTPS/WSS (TLS encrypted).
- You can set a shared `P2P_KEY` to authenticate peers.
- The master's certificate is pinned on first use (TOFU). If the fingerprint
  changes, the connection is refused.
- Without `P2P_KEY`, the CID is logged as `(unauthenticated)` — you can still
  see which device made a change, but there's no cryptographic guarantee.

**Q: How do I confirm a new master when strict pairing (`--require-pairing`) is enabled?**

Strict pairing disables trust-on-first-use for the elected master: the first
time a master is seen, the client refuses it (the log shows a
`[PAIRING NEEDED]` line) instead of silently trusting its certificate. To trust
it, you confirm the master's certificate fingerprint through a channel you
control ("out of band") and record that fingerprint as a pin. Two ways:

**Verify the fingerprint out of band, then pin it (most secure).**

1. On the master device, read its certificate fingerprint through a channel you
   trust (its own screen or console, not the network you are joining). The
   fingerprint is the SHA-256 of the master's `p2p_master_cert.pem`:

   ```bash
   openssl x509 -in ~/.local/state/hsync/device/p2p_master_cert.pem \
     -noout -fingerprint -sha256
   ```

   If you set `HSYNC_DEVICE_DIR`, use `"$HSYNC_DEVICE_DIR/p2p_master_cert.pem"`.

2. On the slave, start with `--require-pairing`. The master is refused; the log
   names the master's device ID (its CID) and address, and the desktop/mobile
   app raises a pairing notification that also shows the fingerprint the slave
   observed.

3. Compare the two fingerprints. They must match exactly (case, colons and
   spaces are ignored, but the hex digits must be identical).

4. If they match, record the fingerprint as a pin on the slave:

   ```bash
   hsync trust peer add AbCdEfGhIjKlMnOpQrStU sha256:FINGERPRINT --bucket photos
   ```

   `AbCdEfGhIjKlMnOpQrStU` is the master's device ID from the `[PAIRING NEEDED]`
   line, `photos` is your bucket, and `FINGERPRINT` is the verified
   64-character fingerprint. The pin is stored in the known-peers store
   (`$XDG_STATE_HOME/hsync/p2p_known_peers.json`, or
   `~/.local/state/hsync/p2p_known_peers.json` when `XDG_STATE_HOME` is unset;
   override with `HSYNC_P2P_PIN_FILE`) — you never edit the JSON by hand.

5. Start the sync again — the now-pinned master is accepted.

**Bootstrap on a network you already trust (simpler).**

Run the slave once *without* `--require-pairing`: it pins the master on first
use. Then restart it *with* `--require-pairing` — the already-pinned master is
accepted, and from then on only a changed fingerprint is refused.

**Q: The master's certificate fingerprint changed (`[FINGERPRINT CHANGED]`) — what do I do?**

The master re-generated its certificate (for example its `p2p_master_cert.pem`
was deleted) while keeping the same device ID. Its peers refuse it as a possible
man-in-the-middle and log the presented fingerprint, the known-peers store and
the exact recovery commands. If you can verify the new fingerprint out of band,
re-pin it:

```bash
hsync trust peer add MASTER_DEVICE_ID sha256:NEW_FINGERPRINT --bucket BUCKET_NAME
```

To forget the pin and be prompted again at the next connection (like
`ssh-keygen -R`):

```bash
hsync trust peer remove MASTER_DEVICE_ID --bucket BUCKET_NAME
```

You can list the current pins with `hsync trust peer list` (add `--json` for a
machine-readable form).

**Q: What happens if two devices modify the same file?**
No device is authoritative: HSYNC resolves concurrent edits with a causal
vector-clock model (ADR-0022). On a true divergence the later
`corrected_mtime` keeps the original path (ADR-0023); SHA-256 is used only
when that field is missing or tied. The losing version is preserved as a
`_conflicted_copy_` so nothing is lost. See
[Conflict Resolution](conflict-resolution.md).

## p2p Mode

**Q: Can I sync devices that are not on the same local network?**
Yes. mDNS only works on one local network, so point every device of the group
at the same **boot/1 bootstrap node** (positional hostname or
`HSYNC_BOOTNODE`). Election, transport and security are unchanged — only
discovery differs. See [Sync across networks](p2p-bootnode.md).

**Q: The client aborts with "Bootnode '…' trust failed: the certificate
fingerprint does not match the accepted pin" — what do I do?**
The bootnode authenticates with a self-signed certificate that you **pin**: with
`--bootnode-tofu` the first fingerprint seen for `host:port` is remembered, and
any later difference is refused as a possible man-in-the-middle. A self-hosted
node generates a fresh certificate at every start unless the operator runs it
with `--identity PATH`, so **a restart changes the fingerprint**.

The client prints the presented fingerprint, the known-hosts file that holds the
accepted pin and the commands that unblock the situation:
- If the change is expected, verify the new fingerprint out of band and re-pin it
  with `hsync trust bootnode add HOSTNAME sha256:NEW_FINGERPRINT` (or pass
  `--bootnode-fingerprint NEW_FINGERPRINT` on this run).
- To forget the pin and be prompted again at the next connection, run
  `hsync trust bootnode remove HOSTNAME` (like `ssh-keygen -R`).
- Ask the operator to persist the node identity (`--identity`) and to publish the
  stable fingerprint for clients to pin.

After a fatal trust failure the client **exits with a non-zero code** instead of
waiting for peers that can never be discovered. See
[Sync across networks](p2p-bootnode.md) and the
node's [FAQ](https://github.com/albertsdk/hsync-bootnode/blob/main/docs/faq.md).

**Q: How does master election work?**
A deterministic algorithm picks the master based on:
1. Already-serving incumbent keeps the role
2. `prefer_master` flag wins over ordinary peers
3. Bidirectional sync direction is preferred
4. Earliest startup time wins
5. Lowest CID lexicographically breaks ties

**Q: What happens if the master goes offline?**
The remaining slaves hold a new election. The slave that started earliest
becomes the new master. No data is lost — all slaves have a local copy.

Deletions made while no master is reachable are recorded locally (as causal
tombstones, see [Conflict Resolution](conflict-resolution.md)): when the
master comes back, the deletion propagates to it instead of the deleted file
being downloaded again.

**Q: Can I prefer a specific device as the master?**
Yes. Set `--prefer-master` on that device: it is preferred in the election over
peers that do not set it (a primary that is already serving keeps the role).

**Q: Can a device never become master?**
Yes. Set `--never-master`. That device always stays a slave.

**Q: How do I keep multiple sets of devices separate on the same LAN?**
Use the `--group` flag. Only devices with the same group name will see
each other. Devices with different groups (or no group) are isolated.

**Q: Do I need to set up port forwarding on my router?**
Not for local-network (mDNS) sync — discovery stays within the subnet and needs
no router configuration. Cross-network groups that use a bootnode must expose
the master's sync port: the client can map it with UPnP when you opt in
(`--upnp` / `HSYNC_UPNP_ENABLED=true`), otherwise forward it on the router.

**Q: Can I sync over Wi-Fi and wired Ethernet?**
Yes. As long as devices are on the same subnet, they will discover each other
regardless of whether they use Wi-Fi or Ethernet.

**Q: Does p2p mode work across VLANs or different subnets?**
Not with mDNS: that discovery is subnet-local. Point every device of the group
at the same `boot/1` bootnode to sync across subnets/VLANs (see
[Sync across networks](p2p-bootnode.md)); Server Mode is the alternative when
you prefer a central hub.

**Q: What ports does p2p mode use?**
A single TLS port, `--listen-port` (default `0` = auto, a free port is picked):
- it serves the REST API for file uploads/downloads **and** the real-time event
  stream (WSS) on the same port.

TLS is used for both HTTPS and WSS. There is no plaintext fallback.

**Q: Can I restrict which devices can write to the master?**
Yes. Set `HSYNC_P2P_KEY` on all devices. The master will refuse write
operations from slaves that don't present the correct key. Without a key,
read-only (download-only) access is always allowed.

**Q: What is the "hello" handshake?**
When no `P2P_KEY` is configured, the master sends `{"type": "hello"}` and the
slave responds with its CID. This lets the operator see which device is
connected in the logs, but there is no cryptographic authentication — the
log carries an explicit `(unauthenticated)` qualifier.

**Q: Can I use delta transfer in p2p mode?**
Yes. Delta transfer (rsync-style block sync) works in p2p mode when the
master supports it. The slave sends block signatures and the master replies
with only the changed blocks.

## Device Identity

**Q: What is the device ID and what is `HSYNC_DEVICE_DIR` for?**
Every installation owns one persistent **device identity** (the same model as
Syncthing's device IDs): a stable 21-character device ID plus the TLS keypair
used for the p2p transport. Both live in the device directory, which defaults
to `$XDG_STATE_HOME/hsync/device/` (or `~/.local/state/hsync/device/` when
`XDG_STATE_HOME` is unset) and can be overridden with
`HSYNC_DEVICE_DIR`. The device ID is the anchor for TOFU fingerprint pins, so
pins survive restarts, and a changed certificate for the same device ID is
detected and refused. See
[p2p Mode → Device Identity](p2p-mode.md#device-identity).

**Q: Do I need to set `HSYNC_DEVICE_DIR` on every device?**
No. On a normal setup — one hsync installation per device — the default
location is correct and you never set the variable. You only need it when:

- you run **multiple instances on the same host** that must count as distinct
  devices (e.g. two peers for the same bucket on one machine), or
- you want to keep hsync state outside the default OS state directory.

**Q: Why don't two instances on the same machine discover each other?**
Because they share one device identity: the device ID is used both as the
discovery CID and in the mDNS service name (`{bucket}-{device_id}`), so a
second instance with the same bucket is ignored as "self" during discovery
(or fails mDNS registration with a name clash). Give each instance its own
device directory:

```bash
HSYNC_P2P_KEY="shared-secret" HSYNC_DEVICE_DIR=~/.local/state/hsync/device-alt \
  hsync p2p -b photos -p ~/Other
```

Different buckets need no change — they share the identity but never clash.

**Q: What happens if I copy the device directory to another machine?**
That **clones the device identity**: two machines would present the same device
ID. hsync detects this at runtime and refuses to sync with the clone — the
other peer logs `[SECURITY] Duplicate device identity detected`, a slave whose
device ID is already in use is refused at the master (with a `[SECURITY]` error
of its own), and every detection point raises a **Duplicate Device Identity**
notification in the desktop app. Give each machine its own identity: delete the
copied device directory (hsync regenerates it on the next run) or point the
machine at a fresh location:

```bash
HSYNC_P2P_KEY="shared-secret" HSYNC_DEVICE_DIR=~/.local/state/hsync/device-this-machine \
  hsync p2p -b photos -p ~/Photos
```

> **Trusted-network note (no `HSYNC_P2P_KEY`):** on an unauthenticated network the device
> ID a peer declares is not verified, so a malicious peer could squat another
> peer's ID and get it refused while it holds the connection. This is a marginal
> availability risk in a mode where reads are open but writes are refused by
> default (a no-key p2p run confirms the posture and is read-only unless the
> dangerous `--allow-unauthenticated-writes` opt-in is enabled on both sides); the
> far more common case the check protects against is the *accidental* clone (a
> copied configuration on a home network).

## Files & Performance

**Q: Is there a file size limit in p2p mode?**
No specific limit, but very large files may take time to transfer over the
local network. Bandwidth limiting is available.

**Q: Does p2p mode use a lot of bandwidth?**
Only changed files are transferred. The event-driven architecture avoids
polling. Compression and delta transfer reduce bandwidth further.

**Q: Why does the log say the file watcher degraded to polling (`[WATCHER FALLBACK]`, inotify limits)?**
The machine's per-user kernel inotify limits are exhausted — a condition of
the host, not of HSYNC (editors, browsers and desktop services each hold
watcher instances). It can hit whichever device starts its watcher last.
Real-time sync continues on the polling fallback (higher CPU, ~1 s event
latency) and inotify is restored automatically once the limits free up
(`[WATCHER RESTORE]`). A `[WATCHER ERROR]` line instead means even the
polling fallback could not start. See
[The file watcher degrades to polling (inotify limits)](troubleshooting.md) for the
diagnosis commands and the remedies (raise the limits or free some up).

**Q: Why are very large files transferred uncompressed over the LAN?**
Compressible files below 256 MB are transferred with zstd compression, but
above that size the master serves them raw. The compressed branch has to
buffer the whole file in the master's memory to build the zstd frame, so it
is capped to protect the master; on a LAN the wire is usually the fast side
anyway, and the raw path additionally keeps download-resume (`Range`)
support, which a compressed response cannot offer. If you want large files
compressed on slow links, use client-server mode (its compression is fully
streamed with no size cap).

**Q: What are the `.hsynckeep` files that keep appearing in my synced folder?**
HSYNC creates a small, empty (0-byte) file named `.hsynckeep` in every folder
that is otherwise empty. It is a marker — like Git's `.gitkeep` — that tells
sync "this folder exists, keep it".

**Why are they created?** HSYNC synchronizes files, so a folder with nothing
in it has nothing to transfer and would be lost on your other devices. The
marker makes the folder visible to sync, so empty folders are preserved
everywhere.

**When are they created?** At the start of every sync, HSYNC scans your folder
and adds a `.hsynckeep` to any directory with no files of its own. A directory
that only contains other directories, or only hidden files, also counts as
empty. A marker is also added when the last file in a directory is deleted.

**Are they synced to other devices?** No. `.hsynckeep` files are local,
per-device markers: they are never uploaded or downloaded, and each device
creates and keeps its own.

**Can I delete them?** You can, but HSYNC will recreate them at the next sync.
They are harmless: zero bytes, hidden, and excluded from both sync and Git.

## Debian Package

**Q: How do I configure the systemd service for p2p mode?**
The packaged unit runs `hsync p2p` on `ExecStart` in continuous mode (the
client default). Edit
`/etc/hsync/hsync.conf` (not `hsync.env`):
```ini
HSYNC_BUCKET="mybucket"
HSYNC_PATH="/path/to/sync"
HSYNC_P2P_KEY="shared-secret"    # required for writes (a keyless p2p is read-only)
```
`HSYNC_P2P_MODE` is ignored; do not add it. Then `systemctl restart hsync`.
