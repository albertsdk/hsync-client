# Security Policy

## Supported Versions

Security fixes are applied to the latest released version on the `main` branch.

| Version | Supported |
|---------|-----------|
| 1.0.x   | ✅        |
| < 1.0   | ❌        |

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

Instead, use GitHub's private vulnerability reporting:

1. Open the **Security** tab of this repository.
2. Click **Report a vulnerability** to start a private advisory.

Please include the affected version, steps to reproduce, the impact, and any
suggested mitigation. Allow a reasonable time for a fix before any public
disclosure. We aim to acknowledge reports within a few days and to keep you
informed of remediation progress.

## Security Model

- **Server authentication** — the client authenticates with a Bearer API key
  (`HSYNC_API_KEY`) over HTTP(S)/WSS.
- **Transport security** — HTTPS/WSS supported. Use `--ca-cert` to trust a
  self-signed server certificate. `--insecure` disables verification and should
  be used only for testing.
- **p2p mode (P2P)** — HTTPS/WSS only, with a persistent self-signed identity and
  connection-level fingerprint pinning (TOFU); optional shared-secret
  authentication (`HSYNC_P2P_PASSPHRASE`, or the raw `HSYNC_P2P_KEY`) and
  group isolation (`--group`).
- **Bootnode discovery** — the `boot/1` QUIC/TLS channel authenticates the
  bootstrap node by certificate fingerprint: on a terminal the first contact is
  SSH-like (the client shows the fingerprint and asks to confirm it once, then
  remembers it). A run without a terminal must decide explicitly — pin it with
  `--bootnode-fingerprint` (or `HSYNC_BOOTNODE_FINGERPRINT`) or trust the first
  sighting with `--bootnode-tofu`; a changed fingerprint is always refused.
  `--insecure` skips verification and is for loopback/testing only.
- **Integrity** — transfers are verified with SHA-256 checksums.

## Hardening Recommendations

- Prefer HTTPS endpoints and avoid `--insecure` outside testing.
- Pin the bootnode fingerprint (`--bootnode-fingerprint`) on every device instead
  of accepting it at the prompt (`ask`) or with `--bootnode-tofu`.
- Set a shared secret (`HSYNC_P2P_PASSPHRASE`, or the raw `HSYNC_P2P_KEY`) when
  using p2p mode on shared networks.
- Keep your API key out of shell history and logs.
