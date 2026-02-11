# HSYNC — WD My Cloud OS5 Package

**HSYNC** keeps your NAS folders in sync — either with a central [HSYNC Server](https://github.com/albertobasso/hsync-server) (Server Mode) or directly with other devices on the same network (LAN Mode, no server required).

This is the **WD My Cloud OS5 package**. It installs the HSYNC client as a background service on your NAS.

> [!WARNING]
> HSYNC creates, overwrites, moves, and deletes files. Always keep verified,
> independent backups. The software is provided "as is", without warranty of
> any kind — see [License](#license) and [Disclaimer](#disclaimer) below.

## What HSYNC does

HSYNC synchronises files using only HTTP/HTTPS, so it works through
corporate firewalls, proxies, and cloud environments where other protocols
(SSH, rsync, Syncthing) are often blocked.

For a full feature list, operating-mode comparison, and detailed
documentation, visit the [HSYNC Client
README](https://github.com/albertobasso/hsync-client/blob/main/README.md).

## Installing on your WD My Cloud

1. Download the latest `.bin` package from the
   [Releases page](https://github.com/albertobasso/hsync-client/releases).
2. Open the WD My Cloud web dashboard, go to **Apps**, and upload the
   `.bin` file.
3. Once installed, the HSYNC service starts automatically. Open the app
   icon to access the **HSYNC Web UI** for configuration.

## Configuring HSYNC

Use the **Web UI** (accessible from the Apps dashboard) to set:

- **Server URL** — the address of your HSYNC server (Server Mode)
- **API Key** — authentication token matching the server
- **Bucket** — the sync bucket name
- **Local Path** — the folder on your NAS to keep in sync
- **Sync Direction** — bidirectional, upload-only, or download-only

Configuration is saved automatically. The service reads it and starts
synchronising.

For LAN Mode (peer-to-peer, no server), toggle **LAN Mode** in the Web
UI. Peers on the same network discover each other via mDNS.

## Using the CLI

The `hsync` binary is installed at `/usr/local/bin/hsync`. You can run it
manually via SSH for advanced options:

```bash
# One-shot server-mode sync
hsync --once -s https://server:9753 -b mybucket -p /path/to/dir

# Continuous mode (the default)
hsync -s https://server:9753 -b mybucket -p /path/to/dir

# LAN mode (peer-to-peer, no server)
hsync -b mybucket -p /path/to/dir --lan-mode
```

> The API key is **required** in Server Mode. Set it via the Web UI or
> `HSYNC_API_KEY` environment variable. The client refuses to send the
> key over plain HTTP — always use `https://`.

For the complete CLI reference, environment variables, and all options,
see the [HSYNC Documentation](https://github.com/albertobasso/hsync-client/tree/main/docs).

## Documentation

- [Getting Started](https://github.com/albertobasso/hsync-client/blob/main/docs/getting-started.md)
- [Server Mode](https://github.com/albertobasso/hsync-client/blob/main/docs/server-mode.md)
- [LAN Mode](https://github.com/albertobasso/hsync-client/blob/main/docs/lan-mode.md)
- [Configuration](https://github.com/albertobasso/hsync-client/blob/main/docs/configuration.md)
- [Examples](https://github.com/albertobasso/hsync-client/blob/main/docs/examples.md)

## License

[Mozilla Public License 2.0](LICENSE)

## Disclaimer

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, AND NONINFRINGEMENT. Although the author has implemented reasonable safety measures, IN NO EVENT SHALL THE AUTHOR OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES, OR OTHER LIABILITY — including, without limitation, any direct, indirect, incidental, special, exemplary, or consequential damages, loss of data, or business interruption — WHETHER IN AN ACTION OF CONTRACT, TORT, OR OTHERWISE, ARISING FROM, OUT OF, OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

This disclaimer supplements, and does not replace, Sections 6 ("Disclaimer of Warranty") and 7 ("Limitation of Liability") of the [Mozilla Public License 2.0](LICENSE), which govern this software.
