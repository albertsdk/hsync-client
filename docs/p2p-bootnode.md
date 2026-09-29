# Sync across networks (bootnode)

How to run `hsync p2p` when the devices are **not** on the same local network:
they discover each other through a `boot/1` bootstrap node instead of mDNS.

> Local network, no bootnode? Use the [mDNS plan](p2p-lan.md).
> The server-side reference (flags, environment variables, defaults):
> [Configuration — p2p Mode](configuration-p2p.md#p2p-mode-options).

## Before you start

- A reachable bootnode (its host or IP, and its UDP port — default `443`).
- The bootnode certificate fingerprint, to pin the TLS trust:
  `--bootnode-fingerprint sha256:<hex>` (or trust the first sighting with
  `--bootnode-tofu`). `--insecure` disables verification and is meant for
  loopback/tests only.
- The **same bootnode** configured on every device of the group.
- A **group key** if the bootnode is private (`--private`) or the bucket is
  protected: the client authenticates with the token derived from
  `HSYNC_P2P_KEY` (`HMAC-SHA256(key, "boot/1" || bucket)[:32]`), so set it to the
  same value the operator wrote in the bootnode's `bucket=<key>` line. An open
  node with no protected buckets needs no key to discover peers.

The bootnode operator prints the fingerprint with `hsync-bootnode identity show`
and distributes it out of band (it is never sent on the wire). Without a pin the
client refuses an unpinned self-signed bootnode — pinning is what makes
cross-network discovery secure.

## Run the plan

First contact — trust the fingerprint on first use; the operator prints it with
`hsync-bootnode identity show`:

```bash
HSYNC_P2P_KEY="shared-secret" hsync p2p \
  -b mybucket -p /srv/hsync boot.example.net --bootnode-tofu
```

Pinned — the hardened form, once the fingerprint is known. Replace
`YOUR_BOOTNODE_FINGERPRINT` with the operator's `sha256:<hex>` value:

```bash
HSYNC_P2P_KEY="shared-secret" hsync p2p \
  -b mybucket -p /srv/hsync boot.example.net \
  --bootnode-fingerprint YOUR_BOOTNODE_FINGERPRINT
```

Service units pass the same inputs as environment variables
(`HSYNC_BOOTNODE`, `HSYNC_BOOTNODE_FINGERPRINT`, `HSYNC_BOOTNODE_TOFU`);
the full reference is
[Configuration — p2p Mode](configuration-p2p.md#p2p-mode-options).

`HSYNC_BOOTNODE` is a **bare hostname**: no scheme, no path, no `host:port`.
Set the port with `--port` (UDP, default `443`); passing `--port` without a
bootnode is refused rather than silently ignored.

## Let the node be reachable

To be dialable from outside, a device that should act as coordinator must
publish a TCP endpoint. The client maps its master port with **UPnP** when a
bootnode is in use (on by default; disable with `--no-upnp` or
`HSYNC_UPNP_ENABLED=false`). If no mapping can be created and no public TCP
endpoint is known, the device stays **slave-only** instead of advertising an
address that cannot be reached.

## Confirm which plan is active

```
Discovery: bootnode boot.example.net:443 (cross-network; UPnP on)
```

The master additionally logs `[READY]` only once its endpoint has been
published to the bootnode, so the readiness line means "reachable", not just
"started".

The **master election is the same one used on a local network**: each device
publishes its role through the node, so a device that is already serving keeps
the role, `--force-master` wins over an ordinary peer, `--never-master` devices
are never elected, and ties fall back to the earliest start. Nothing extra to
configure; the full priority order is in
[p2p mode → Master Election](p2p-mode.md#master-election).

## Verify registration

```bash
# The other device should list this peer after a query round.
HSYNC_P2P_KEY="shared-secret" hsync p2p \
  -b mybucket -p /srv/hsync boot.example.net \
  --bootnode-tofu
```

Each block is self-contained: the bootnode host, the pin (or
`--bootnode-tofu`) and the group key all belong to that command, so paste the
whole block.

Look for the discovery banner, then a peer with `reachable=true`; the advisory
`capacity` hint a peer advertises is a pre-sizing hint only — the authoritative
limit is negotiated on the authenticated session.

## Troubleshoot

| Symptom | Likely cause |
|---|---|
| `Discovery: mDNS …` although a bootnode was intended | `HSYNC_BOOTNODE`/positional not set, or an empty value. |
| Client exits with `--port requires a bootnode hostname` | `--port` passed without a host: add the hostname or `HSYNC_BOOTNODE`. |
| TLS handshake fails | Bootnode certificate not pinned: pass `--bootnode-fingerprint` (or `--bootnode-tofu`, or `--insecure` only on loopback). |
| `fingerprint_changed` (bootnode) in the log | The bootnode certificate no longer matches the pin — possible man-in-the-middle, or the operator rotated the identity. Re-pin with the new fingerprint; during a rotation the operator publishes the new fingerprint and you pass both (old + new) once. |
| Peer listed but connection fails | The advertised TCP endpoint is not reachable (UPnP disabled/failed and no public port): see "Let the node be reachable". |
| Cross-network sync not needed | Go back to the [mDNS plan](p2p-lan.md) by unsetting `HSYNC_BOOTNODE`. |

More: [Troubleshooting](troubleshooting.md) and [FAQ — p2p Mode](faq-p2p.md).
On the operator side, the [private node](https://github.com/albertsdk/hsync-bootnode/blob/main/docs/usage.md#private-node-authenticated-bootstrap)
and the [privacy and anonymity model](https://github.com/albertsdk/hsync-bootnode/blob/main/docs/usage.md#privacy-and-anonymity-model)
explain what a closed, pseudonymous network requires.
The wire contract is the [boot/1 bootstrap protocol](https://github.com/albertsdk/hsync-common/blob/main/docs/bootnode-protocol.md).
