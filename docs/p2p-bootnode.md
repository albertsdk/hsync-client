# Sync across networks (bootnode)

How to run `hsync p2p` when the devices are **not** on the same local network:
they discover each other through a `boot/1` bootstrap node. mDNS stays active on
the same client, so a device next to you is still found directly on the local
network without a round trip through the node.

> Local network, no bootnode? Use the [mDNS plan](p2p-lan.md).
> The server-side reference (flags, environment variables, defaults):
> [Configuration — p2p Mode](configuration-p2p.md#p2p-mode-options).

## Before you start

- A reachable bootnode (its host or IP, and its UDP port — default `443`).
- On a terminal, no further preparation: the client shows the node's SHA-256
  fingerprint on first contact and asks you to confirm it once (the default
  `ask` policy, like `ssh`). A run without a terminal has no prompt, so it must
  decide the trust explicitly: pin `--bootnode-fingerprint sha256:<hex>`, or
  accept the first sighting with `--bootnode-tofu`. Read a fingerprint without
  trusting it with `hsync trust bootnode fingerprint <host>`. `--insecure`
  disables verification and is meant for loopback/tests only.
- The **same bootnode** configured on every device of the group.
- A **group key** if the bootnode is private (`--private`) or the bucket is
  protected: the client derives the token with
  `HMAC-SHA256(key, "boot/1" || bucket)[:32]`. Supply the secret as
  `HSYNC_P2P_PASSPHRASE` (at least 12 characters; derived with scrypt) or as
  `HSYNC_P2P_KEY` (a full key of at least 32 characters). The bootnode always
  holds the **raw derived key** in its `bucket=<key>` line: for a passphrase
  group, obtain it once with `hsync derive-group-key` and paste the 64-hex
  output. An open node with no protected buckets needs no key to discover peers.

The bootnode operator prints the fingerprint with `hsync-bootnode identity show`
and distributes it out of band (it is never sent on the wire), for the devices
that pin it; on a terminal the `ask` prompt confirms the node instead. Either
way the fingerprint is what authenticates the self-signed node, so pin it for an
unattended or security-sensitive setup.

## Run the plan

First contact on a terminal — the client prints the certificate fingerprint and
asks you to confirm it (like `ssh`); answer `yes` once and it is remembered (the
client reports where the pin was saved):

```bash
HSYNC_P2P_PASSPHRASE="a-long-shared-password" hsync p2p boot.example.net \
  -b mybucket -p /srv/hsync
```

> For a protected bucket, the bootnode's `bucket=` line must contain the same
> **derived** key: run `hsync derive-group-key` once, store its 64-hex output
> where only the service can read it, and use it on the node.

Non-interactive first contact — accept the first sighting explicitly (the
operator prints the fingerprint with `hsync-bootnode identity show`):

```bash
HSYNC_P2P_PASSPHRASE="a-long-shared-password" hsync p2p boot.example.net \
  -b mybucket -p /srv/hsync --bootnode-tofu
```

Pinned — the hardened form, once the fingerprint is known. Replace
`YOUR_BOOTNODE_FINGERPRINT` with the operator's `sha256:<hex>` value:

```bash
HSYNC_P2P_PASSPHRASE="a-long-shared-password" hsync p2p boot.example.net \
  -b mybucket -p /srv/hsync \
  --bootnode-fingerprint YOUR_BOOTNODE_FINGERPRINT
```

## Manage the known hosts

The accepted fingerprints live in `$XDG_STATE_HOME/hsync/bootnode_known_hosts.json`
(or `~/.local/state/hsync/bootnode_known_hosts.json` when `XDG_STATE_HOME`
is unset) and are managed like SSH's known hosts
(`HSYNC_BOOTNODE_TRUST_FILE` overrides the path):

```bash
hsync trust bootnode fingerprint boot.example.net   # read the node's fingerprint (no trust)
hsync trust bootnode add boot.example.net sha256:FINGERPRINT
hsync trust bootnode list        # show pinned nodes
hsync trust bootnode list --json # machine-readable
hsync trust bootnode show boot.example.net
hsync trust bootnode remove boot.example.net   # like ssh-keygen -R
```

Every pinned fingerprint — confirmed at the prompt or accepted on first use — is
announced with the file that stores it, for example:

```text
Permanently added 'boot.example.net:443' (SHA256) to the list of known hosts (~/.local/state/hsync/bootnode_known_hosts.json).
```

`--bootnode-trust` selects the policy explicitly (`ask`, `accept-new`, `strict`,
`none`); `--bootnode-fingerprint` implies `strict`, `--bootnode-tofu` implies
`accept-new`. Without a terminal the `ask` default fails closed for the bootnode
source: the node is not used (and local-network discovery keeps working), so
automation must opt in explicitly.

Service units pass the same inputs as environment variables
(`HSYNC_BOOTNODE`, `HSYNC_BOOTNODE_FINGERPRINT`, `HSYNC_BOOTNODE_TOFU`,
`HSYNC_BOOTNODE_TRUST`); the full reference is
[Configuration — p2p Mode](configuration-p2p.md#p2p-mode-options).

`HSYNC_BOOTNODE` accepts the same endpoint forms as the `BOOTNODE` positional:
`boot.example.net`, `boot.example.net:8443`, `[2001:db8::1]` or
`[2001:db8::1]:8443` — no scheme, no path. A port in the value selects the QUIC
port (UDP, default `443`); an explicit `--port` overrides it, while `--port`
without any bootnode is refused rather than silently ignored.

## Let the node be reachable

To be dialable from outside, a device that should act as coordinator must
publish a TCP endpoint. The client can map its master port with **UPnP** when a
bootnode is in use (opt-in: enable with `--upnp` or `HSYNC_UPNP_ENABLED=true`).
Without a mapping it publishes the address the node observed (the NAT-mapped
public IP) plus its LAN address, which is enough whenever that public address
forwards anything the group can use — a home router with a forwarded port, an
APN that hands out a public IPv4.

The client publishes its **full candidate list** to the node (the UPnP/public
endpoint *and* the LAN address); the node relays the first four of them, and a
peer tries them host/LAN-first. Two
devices behind the same NAT therefore connect over the local path even when the
router's public IP has no hairpin (NAT loopback) route — the public endpoint is
simply the last candidate. If no advertised candidate is reachable, the client
retries discovery instead of failing the run.

## Behind carrier-grade NAT (mobile, hotspot, guest network)

When no candidate is dialable at all — the usual case for a phone hotspot or a
router with a data SIM, where the carrier's NAT owns the only public address and
forwards nothing — the bootnode itself carries the data plane. Nothing has to be
configured on the client: after every direct candidate failed, the master opens a
relay listener with the node and a slave asks the node for a tunnel to it. Look
for these two lines:

```text
[RELAY] listening on the bootnode — peers without a reachable inbound port can sync through it
[RELAY] master <cid> has no reachable inbound port; relaying the data plane through the bootnode
```

The node forwards the peers' TLS **verbatim**: it never terminates it, so it sees
no file name and no content, and the master's certificate pin is still what
authenticates it. What the node does see is which devices are paired and how much
they exchanged. The relay is opt-in on the **node** side (`hsync-bootnode run
--relay`): with a node that has not enabled it the clients keep the direct-only
behaviour and the slave keeps retrying discovery.

Two consequences worth knowing:

- A relayed group still syncs correctly, but every byte takes two legs (peer →
  node → peer) and shares the node's uplink, so a large first sync is slower than
  it would be between two directly reachable devices.
- Only the **master** needs to be reachable through the relay. Peer-assisted
  download keeps using direct seeder endpoints; a seeder that cannot be reached
  directly is skipped and the block is served by the master.

## Confirm which plan is active

```text
Discovery: mDNS on the local network + bootnode boot.example.net:443 (cross-network; UPnP on)
```

The master additionally logs `[READY]` only once its endpoint has been
published to the bootnode (bounded wait), so the readiness line means
"reachable", not just "started". A bootnode the client stops using (untrusted
identity) no longer gates that line.

The **master election is the same one used on a local network**: each device
publishes its role through the node, so a device that is already serving keeps
the role, `--prefer-master` wins over an ordinary peer, `--never-master` devices
are never elected, and ties fall back to the earliest start. Nothing extra to
configure; the full priority order is in
[p2p mode → Master Election](p2p-mode.md#master-election).

## Verify registration

```bash
# The other device should list this peer after a query round.
HSYNC_P2P_PASSPHRASE="a-long-shared-password" hsync p2p boot.example.net \
  -b mybucket -p /srv/hsync
```

Each block is self-contained: the bootnode host and the group key belong to that
command, so paste the whole block. On a terminal the `ask` prompt already
remembers the node; add `--bootnode-tofu` (or a pin) only for a run without one.

Look for the discovery banner, then a peer with `reachable=true`; the advisory
`capacity` hint a peer advertises is a pre-sizing hint only — the authoritative
limit is negotiated on the authenticated session.

## Troubleshoot

| Symptom | Likely cause |
|---|---|
| `Discovery: mDNS …` although a bootnode was intended | `HSYNC_BOOTNODE`/positional not set, or an empty value. |
| Client exits with `--port requires a bootnode hostname` | `--port` passed without a host: add the hostname or `HSYNC_BOOTNODE`. |
| `Bootnode … unreachable` warning, devices still sync locally | The node cannot be reached: the client retries it every 30 s and syncs over mDNS meanwhile. The bootnode only adds cross-network discovery; local sync is unaffected. |
| The bootnode identity cannot be confirmed and cross-network peers stop appearing | The node is not trusted yet (policy `ask` with no terminal, `strict` without a pin, or a changed fingerprint). The client logs the recovery block, publishes a `pairing_needed`/`fingerprint_changed` event (`scope: "bootnode"`) and **keeps syncing on the local network**: it stops using the bootnode, never contacts it again, and never writes to it. Confirm the fingerprint out of band and pin it — `--bootnode-fingerprint`, `hsync trust bootnode add …` (the desktop pins from its trust dialog), or `--bootnode-tofu`/`--insecure` (loopback only) — then restart the run to regain cross-network discovery. |
| `fingerprint_changed` (bootnode) with `ERROR: Bootnode '…' trust failed: the certificate fingerprint does not match the accepted pin` | The bootnode certificate does not match the pin — possible man-in-the-middle, or the operator restarted/rotated the identity. The client prints the presented fingerprint, the known-hosts file and the exact commands that unblock the situation, then drops the bootnode source (the local network keeps syncing). Re-pin the new fingerprint after verifying it out of band (`hsync trust bootnode add …`) or forget the pin with `hsync trust bootnode remove …` (like `ssh-keygen -R`) to be prompted again; during a planned rotation pass both fingerprints (old + new) once. See [FAQ — p2p Mode](faq-p2p.md) and the node's [FAQ](https://github.com/albertsdk/hsync-bootnode/blob/main/docs/faq.md). |
| Peer listed but connection fails | The advertised TCP endpoint is not reachable (UPnP disabled/failed and no public port): see "Let the node be reachable". The client retries the other candidates, then discovery. |
| Two peers on the same network (or same host) never connect | mDNS already runs alongside the bootnode, so same-network peers are normally found directly; this symptom is for peers behind the **same NAT on different networks**, where some routers block NAT loopback (hairpinning) and the public endpoint is unreachable from inside. The client prefers the host/LAN candidate and falls back to the public one. |
| Two peers on different networks never connect, and no `[RELAY]` line appears | The node has not enabled the data relay, so only directly reachable peers can connect: start it with `--relay` (or `HSYNC_BOOTNODE_RELAY=1`), or give at least the elected master a routable public endpoint (UPnP/public port). |
| Cross-network sync not needed | Go back to the [mDNS plan](p2p-lan.md) by unsetting `HSYNC_BOOTNODE`. |

More: [Troubleshooting](troubleshooting.md) and [FAQ — p2p Mode](faq-p2p.md).
On the operator side, the [private node](https://github.com/albertsdk/hsync-bootnode/blob/main/docs/usage.md#private-node-authenticated-bootstrap)
and the [privacy and anonymity model](https://github.com/albertsdk/hsync-bootnode/blob/main/docs/usage.md#privacy-and-anonymity-model)
explain what a closed, pseudonymous network requires.
The wire contract is the [boot/1 bootstrap protocol](https://github.com/albertsdk/hsync-common/blob/main/docs/bootnode-protocol.md).
