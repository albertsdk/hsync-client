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
- **LAN mode (P2P)** — HTTPS/WSS only, with a persistent self-signed identity and
  connection-level fingerprint pinning (TOFU); optional shared-secret
  authentication (`HSYNC_LAN_KEY`) and group isolation (`--group`).
- **Integrity** — transfers are verified with SHA-256 checksums.

## Hardening Recommendations

- Prefer HTTPS endpoints and avoid `--insecure` outside testing.
- Set `HSYNC_LAN_KEY` when using LAN mode on shared networks.
- Keep your API key out of shell history and logs.
