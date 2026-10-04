# Getting Started — Windows installer

The installer is **per-user** (no administrator rights): it installs `hsync.exe`
and appends its directory to your user `PATH`, so `hsync` is available from a new
terminal.

## Prerequisites

- Windows 10/11 (x64)

## Install

1. Download `hsync-client-VERSION-windows-amd64-setup.exe` from the release page
   (replace `VERSION` with the release you want).
2. Run it and follow the wizard.
3. Open a **new** terminal (so the updated `PATH` is loaded) and check:

   ```bash
   hsync --help
   ```

## Update and remove

Download and run the newer installer; it upgrades in place. To remove it, go to
**Settings → Apps → HSYNC Client**. The `PATH` entry is removed automatically.

## Prefer no installer?

The portable `hsync-client-VERSION-windows-amd64.zip` contains the same
`hsync.exe`; unzip it anywhere and run it directly (add the folder to `PATH`
yourself if you want the bare `hsync` command).

## Next

- Every CLI option and environment variable: [Configuration](configuration.md).
