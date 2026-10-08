# Getting Started — Windows installer

The installer is **per-user** (no administrator rights): it installs `hsync.exe`
and appends its directory to your user `PATH`, so `hsync` is available from a new
terminal.

## Prerequisites

- <img src="assets/os/windows.svg" alt="Windows" width="16" height="16"> Windows 10/11 (x64)

## Install

Download the latest installer:

[Download hsync-client-windows-amd64-setup.exe](https://github.com/albertsdk/hsync-client/releases/latest/download/hsync-client-windows-amd64-setup.exe)

Run it and follow the wizard, then open a **new** terminal (so the updated
`PATH` is loaded) and verify:

```powershell
hsync --help
```

The link always points to the **latest stable** release; to pin a version, use
the [Releases page](https://github.com/albertsdk/hsync-client/releases).

## Update and remove

Download and run the newer installer; it upgrades in place. To remove it, go to
**Settings → Apps → HSYNC Client**. The `PATH` entry is removed automatically.

## Prefer no installer?

The fixed-name `hsync-client-windows-amd64.zip` (or a versioned copy) contains the
same `hsync.exe`; unzip it anywhere and run it directly (add the folder to `PATH`
yourself if you want the bare `hsync` command).

## Next

- Every CLI option and environment variable: [Configuration](configuration.md).
