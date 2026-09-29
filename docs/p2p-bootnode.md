# Sync across networks (bootnode)

How to run `hsync p2p` when the devices are **not** on the same local network:
they discover each other through a `boot/1` bootstrap node instead of mDNS.

> Local network, no bootnode? Use the [mDNS plan](p2p-lan.md).
> The server-side reference (flags, environment variables, defaults):
> [Configuration — p2p Mode](configuration-p2p.md#p2p-mode-options).

## Before you start

- A reachable bootnode (its host or IP, and its UDP port — default `443`).
- The bootnode certificate fingerprint, to pin the TLS trust. On a terminal the
  client shows it on first contact and asks for confirmation (the default `ask`
  policy); non-interactive runs pin it explicitly with
  `--bootnode-fingerprint sha256:<hex>` or accept the first sighting with
  `--bootnode-tofu`. Read it without trusting with
  `hsync bootnode trust fingerprint <host>`. `--insecure` disables verification
  and is meant for loopback/tests only.
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

First contact on a terminal — the client prints the certificate fingerprint and
asks you to confirm it (like `ssh`); answer `yes` once and it is remembered:

```bash
HSYNC_P2P_KEY="shared-secret" hsync p2p boot.example.net \
  -b mybucket -p /srv/hsync
```

Non-interactive first contact — accept the first sighting explicitly (the
operator prints the fingerprint with `hsync-bootnode identity show`):

```bash
HSYNC_P2P_KEY="shared-secret" hsync p2p boot.example.net \
  -b mybucket -p /srv/hsync --bootnode-tofu
```

Pinned — the hardened form, once the fingerprint is known. Replace
`YOUR_BOOTNODE_FINGERPRINT` with the operator's `sha256:<hex>` value:

```bash
HSYNC_P2P_KEY="shared-secret" hsync p2p boot.example.net \
  -b mybucket -p /srv/hsync \
  --bootnode-fingerprint YOUR_BOOTNODE_FINGERPRINT
```

## Manage the known hosts

The accepted fingerprints live in `~/.local/state/hsync/bootnode_known_hosts.json`
and are managed like SSH's known hosts (`HSYNC_BOOTNODE_TRUST_FILE` overrides the
path):

```bash
hsync bootnode trust fingerprint boot.example.net   # read the node's fingerprint (no trust)
hsync bootnode trust add boot.example.net sha256:FINGERPRINT
hsync bootnode trust list                            # show pinned nodes
hsync bootnode trust show boot.example.net
hsync bootnode trust remove boot.example.net         # like ssh-keygen -R
```

`--bootnode-trust` selects the policy explicitly (`ask`, `accept-new`, `strict`,
`none`); `--bootnode-fingerprint` implies `strict`, `--bootnode-tofu` implies
`accept-new`. Without a terminal the `ask` default fails closed, so automation
must opt in explicitly.

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
HSYNC_P2P_KEY="shared-secret" hsync p2p boot.example.net \
  -b mybucket -p /srv/hsync --bootnode-tofu
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
| TLS handshake fails | Bootnode certificate not pinned. On a terminal the default `ask` prompt confirms it; with no terminal the client fails closed — pass `--bootnode-fingerprint` (or `--bootnode-tofu`, or `--insecure` only on loopback). |
| `Error: 298 … hostname '<host>' doesn't match DNSPattern(pattern=b'hsync-bootnode')` | The client is verifying the certificate *normally* (no pin was given). The node is self-signed and its SAN is the placeholder `hsync-bootnode` (it binds `0.0.0.0` without `--public-host`), so standard TLS rejects it. Pin the node — `--bootnode-tofu` or `--bootnode-fingerprint <fp>` — which is the supported trust model for a self-signed node; the operator can also start it with `--public-host <host>` to get a matching SAN. See [FAQ — p2p Mode](faq-p2p.md). |
| `fingerprint_changed` (bootnode), or `ERROR: … certificate fingerprint … does not match the accepted pin` | The bootnode certificate does not match the pin — possible man-in-the-middle, or the operator restarted/rotated the identity. Re-pin with the new fingerprint (`--bootnode-fingerprint`) or clear the stale entry; during a rotation the operator publishes the new fingerprint and you pass both (old + new) once. The client **exits with a non-zero code**: see [FAQ — p2p Mode](faq-p2p.md) and the node's [FAQ](https://github.com/albertsdk/hsync-bootnode/blob/main/docs/faq.md). |
| Peer listed but connection fails | The advertised TCP endpoint is not reachable (UPnP disabled/failed and no public port): see "Let the node be reachable". |
| Cross-network sync not needed | Go back to the [mDNS plan](p2p-lan.md) by unsetting `HSYNC_BOOTNODE`. |

More: [Troubleshooting](troubleshooting.md) and [FAQ — p2p Mode](faq-p2p.md).
On the operator side, the [private node](https://github.com/albertsdk/hsync-bootnode/blob/main/docs/usage.md#private-node-authenticated-bootstrap)
and the [privacy and anonymity model](https://github.com/albertsdk/hsync-bootnode/blob/main/docs/usage.md#privacy-and-anonymity-model)
explain what a closed, pseudonymous network requires.
The wire contract is the [boot/1 bootstrap protocol](https://github.com/albertsdk/hsync-common/blob/main/docs/bootnode-protocol.md).
