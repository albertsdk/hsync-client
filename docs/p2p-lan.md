# Sync on the local network (mDNS)

How to run `hsync p2p` with **mDNS only**: peers find each other over mDNS on the
same local network, with no server and no rendezvous. mDNS is always active, so
this page covers running without a rendezvous configured.

> First sync with two devices? Start from
> [Getting Started — p2p Mode](getting-started-p2p.md); this page covers
> operating and verifying the local-network plan.
> Rendezvous through a rendezvous node instead: [Sync across networks](p2p-rendezvous.md).

## Before you start

- Both devices are on the **same local network** (mDNS does not cross routers).
- For a write-capable run both sides share the same key:
  `HSYNC_P2P_PASSPHRASE="a-long-shared-password"`.
- A read-only peer (`--sync-direction download-only`) needs no key.

## Run the plan

```bash
# Continuous p2p sync with mDNS rendezvous (no rendezvous configured).
HSYNC_P2P_PASSPHRASE="a-long-shared-password" hsync p2p -b mybucket -p /srv/hsync
```

Nothing else is required: with no `RENDEZVOUS` hostname and no `HSYNC_RENDEZVOUS_SERVER_ENDPOINTS`,
`hsync p2p` never contacts a rendezvous node.

## Confirm which plan is active

At startup the client logs the plan it selected:

```text
Rendezvous: mDNS on the local network (no rendezvous configured)
```

If you see `Rendezvous: mDNS on the local network + rendezvous HOST:PORT` instead, a
rendezvous is configured — unset `HSYNC_RENDEZVOUS_SERVER_ENDPOINTS` (or drop the positional) to go
back to mDNS-only.

## Keep groups apart

The bucket isolates the sync set; `--group` adds a label peers must share:

```bash
HSYNC_P2P_PASSPHRASE="a-long-shared-password" hsync p2p -b mybucket -p /srv/hsync --group home
```

See [Configuration — p2p Mode](configuration-p2p.md#p2p-mode-options) for the
full option and environment-variable reference.

## Control the election (optional)

```bash
# Always/never become the sync coordinator for this bucket.
HSYNC_P2P_PASSPHRASE="a-long-shared-password" hsync p2p -b mybucket -p /srv/hsync --prefer-master
HSYNC_P2P_PASSPHRASE="a-long-shared-password" hsync p2p -b mybucket -p /srv/hsync --never-master
```

The mDNS plan also exposes `--peer-rendezvous-timeout SECONDS` (default `0` = wait
indefinitely) for one-shot runs.

## Verify it works

- The peer list grows in the logs as mDNS records arrive.
- `--prefer-master` on one side and `--never-master` on the other gives a
  deterministic coordinator; the slave logs the master it elected.
- A file created on the coordinator appears on the other device.

## Troubleshoot

| Symptom | Likely cause |
|---|---|
| No peers discovered | Devices on different networks/VLANs, or mDNS blocked (some guest Wi-Fi and container networks) → use the [rendezvous plan](p2p-rendezvous.md) across networks. |
| Peers seen, no sync | Different `HSYNC_P2P_PASSPHRASE`/`HSYNC_P2P_KEY`, bucket or `--group`. |
| Writes refused | No shared secret on one side: set the same password or raw key, or use `--sync-direction download-only`. |

More: [Troubleshooting](troubleshooting.md) and [FAQ — p2p Mode](faq-p2p.md).
Protocol internals: [p2p Internals](internal/p2p-internals.md).
