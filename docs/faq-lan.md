# FAQ — LAN P2P Mode

> For Server Mode FAQ, see [FAQ — Server Mode](faq-server.md).

## General

**Q: What is HSYNC LAN P2P Mode?**
LAN P2P Mode lets devices sync files directly over the local network without
a central server. One device is elected **master** and serves files to all
other devices (**slaves**).

**Q: Is the server required for HSYNC to work?**
No. LAN P2P Mode works without a server. The server is only needed if you
want to sync devices across different networks.

**Q: Can I use both Server Mode and LAN Mode at the same time?**
No. Each HSYNC instance operates in one mode at a time.

## Security & Safety

**Q: How does HSYNC keep my files safe in LAN mode?**
- All traffic uses HTTPS/WSS (TLS encrypted).
- You can set a shared `LAN_KEY` to authenticate peers.
- The master's certificate is pinned on first use (TOFU). If the fingerprint
  changes, the connection is refused.
- Without `LAN_KEY`, the CID is logged as `(unauthenticated)` — you can still
  see which device made a change, but there's no cryptographic guarantee.

**Q: What happens if two devices modify the same file?**
No device is authoritative: HSYNC resolves concurrent edits with a causal
vector-clock model (ADR-0022). On a true divergence the deterministic,
content-only tie-break (highest SHA-256) picks the winner, and the losing
version is preserved as a `_conflicted_copy_` so nothing is lost. See
[Conflict Resolution](conflict-resolution.md).

## LAN Mode

**Q: How does master election work?**
A deterministic algorithm picks the master based on:
1. Already-serving incumbent keeps the role
2. `force_master` flag wins over ordinary peers
3. Bidirectional sync direction is preferred
4. Earliest startup time wins
5. Lowest CID lexicographically breaks ties

**Q: What happens if the master goes offline?**
The remaining slaves hold a new election. The slave that started earliest
becomes the new master. No data is lost — all slaves have a local copy.

**Q: Can I force a specific device to always be the master?**
Yes. Set `--force-master` on that device. It will win elections against
non-force-master peers.

**Q: Can a device never become master?**
Yes. Set `--never-master`. That device always stays a slave.

**Q: How do I keep multiple sets of devices separate on the same LAN?**
Use the `--lan-group` flag. Only devices with the same group name will see
each other. Devices with different groups (or no group) are isolated.

**Q: Do I need to set up port forwarding on my router?**
No. LAN mode uses mDNS for discovery and works within the local subnet only.
No router configuration is needed.

**Q: Can I sync over Wi-Fi and wired Ethernet?**
Yes. As long as devices are on the same subnet, they will discover each other
regardless of whether they use Wi-Fi or Ethernet.

**Q: Does LAN mode work across VLANs or different subnets?**
No. mDNS discovery is subnet-local. Use Server Mode to sync across different
networks.

**Q: What ports does LAN mode use?**
Two consecutive ports, starting from `--lan-port` (default 9753):
- HTTP port: serves the REST API for file uploads/downloads
- WebSocket port (HTTP port + 1): real-time event stream

Both use TLS (HTTPS/WSS). There is no plaintext fallback.

**Q: Can I restrict which devices can write to the master?**
Yes. Set `HSYNC_LAN_KEY` on all devices. The master will refuse write
operations from slaves that don't present the correct key. Without a key,
read-only (download-only) access is always allowed.

**Q: What is the "hello" handshake?**
When no `LAN_KEY` is configured, the master sends `{"type": "hello"}` and the
slave responds with its CID. This lets the operator see which device is
connected in the logs, but there is no cryptographic authentication — the
log carries an explicit `(unauthenticated)` qualifier.

**Q: Can I use delta transfer in LAN mode?**
Yes. Delta transfer (rsync-style block sync) works in LAN mode when the
master supports it. The slave sends block signatures and the master replies
with only the changed blocks.

## Files & Performance

**Q: Is there a file size limit in LAN mode?**
No specific limit, but very large files may take time to transfer over the
local network. Bandwidth limiting is available.

**Q: Does LAN mode use a lot of bandwidth?**
Only changed files are transferred. The event-driven architecture avoids
polling. Compression and delta transfer reduce bandwidth further.

## Debian Package

**Q: How do I configure the systemd service for LAN mode?**
Set the environment variables in `/etc/hsync/hsync.env`:
```
HSYNC_BUCKET="mybucket"
HSYNC_PATH="/path/to/sync"
HSYNC_LAN_MODE=true
HSYNC_LAN_GROUP="mygroup"        # optional
HSYNC_LAN_KEY="shared-secret"    # optional, recommended
```
Then restart with `systemctl restart hsync`.
