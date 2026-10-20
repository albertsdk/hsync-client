# HSYNC Client — Troubleshooting

Task-focused fixes for problems you can observe in the client's logs or
behaviour. Symptom → cause → remedy, with copy-paste commands.

> The log labels referenced here (e.g. `[WATCHER ERROR]`) are catalogued in
> [Log Labels](log-labels.md).

## No peers are discovered

**Symptom.** The client starts, logs `Discovery: mDNS on the local network` (or
a bootnode banner), but the peer list stays empty and no sync happens.

**Cause & remedy.**

- Devices on **different networks** cannot see each other over mDNS → configure
  a bootstrap node on every device: [Sync across networks](p2p-bootnode.md).
- Same network but still nothing: mDNS is blocked (guest Wi-Fi, some container
  or VPN interfaces) or devices sit on different VLANs. Run the mDNS plan
  checklist in [Sync on the local network](p2p-lan.md).
- Peers listed but no sync → different `HSYNC_P2P_KEY`, bucket or `--group`.
- The log shows `Discovery: bootnode …` although mDNS was intended → a bootnode
  is configured; unset `HSYNC_BOOTNODE` (or drop the positional).

## The file watcher degrades to polling (inotify limits)

**Symptom.** The client log shows a parsable warning and the watcher switches
from the kernel inotify backend to a polling backend:

```console
[WATCHER FALLBACK] inotify watch limit reached (fs.inotify.max_user_watches): ...
Switching to PollingObserver for '/path/to/bucket' (higher CPU usage, up to
~1 s event latency).
```

**What happens.** On Linux the file watcher is built on the kernel's inotify API,
which has two per-user limits:

- `fs.inotify.max_user_instances` — how many independent watchers exist.
  Every hsync run holds one instance, and so does every other
  watcher-based application: editors (VS Code), browsers, desktop services
  (GNOME, gvfs, audio daemons)… The kernel default (often `128`) fills up
  easily on a desktop with several such applications, and the failure hits
  whichever watcher starts *after* the pool is exhausted.
- `fs.inotify.max_user_watches` — one watch per watched sub-directory.
  A very deep synced tree can exhaust it (the client also warns proactively
  when the tree approaches this limit).

When either limit is hit — at startup, or later when a new sub-directory is
created — `inotify_init`/`inotify_add_watch` fails with `EMFILE` (Errno 24,
instances) or `ENOSPC` (Errno 28, watches). Instead of blocking real-time sync,
the client **automatically degrades to a polling watcher** (`PollingObserver`),
which needs no inotify resources. The cost is higher CPU usage and up to ~1 s of
event latency; periodic resync (`RESYNC`) still covers any event the polling
watcher might miss.

**Automatic recovery.** While degraded, the client periodically probes whether
inotify is available again. As soon as the limits free up it restores the inotify
backend and logs:

```console
[WATCHER RESTORE] file watcher restored to inotify for '/path/to/bucket'.
```

**Diagnosis.** Check the current limits and how many instances are already
held by your user's processes:

```bash
cat /proc/sys/fs/inotify/max_user_instances
cat /proc/sys/fs/inotify/max_user_watches
find /proc/[0-9]*/fd -lname 'anon_inode:*inotify*' 2>/dev/null | wc -l
```

If the instance count is close to `max_user_instances`, the per-user pool is
saturated — this is a machine-level condition, not an hsync bug.

**Remedy — raise the limits (recommended).** Give your user enough headroom so
the watcher can stay on inotify:

```bash
sudo sysctl fs.inotify.max_user_instances=512
```

To make the change persistent across reboots:

```bash
echo fs.inotify.max_user_instances=512 \
  | sudo tee /etc/sysctl.d/99-hsync-inotify.conf
sudo sysctl --system
```

`max_user_watches` is raised the same way (e.g. `1048576` for very large
trees): replace the name in both commands.

**Remedy — free instances instead.** If you would rather not change kernel
settings, close other watcher-heavy applications (secondary editor windows,
idle browsers, unused desktop services); the client restores inotify by itself
on its next probe.

**Notes.**

- Inside containers the inotify limits are the **host kernel's**: raising
  them inside the container sysfs is not effective — tune the host (or run
  the client with host networking where the sync tree lives on the host).
- One hsync process holds one instance regardless of how many
  sub-directories it watches — running several hsync folders on the same
  machine costs one instance each.

## Too many open files (descriptor limit)

**Symptom.** The client log shows a descriptor-exhaustion warning:

```console
[FD EXHAUSTED] file-descriptor limit reached (RLIMIT_NOFILE / fs.file-max): ...
```

**Cause.** The process exhausted its open-file descriptors. `RLIMIT_NOFILE` is
the per-process limit and the one hsync checks proactively at startup; the
system-wide kernel pool `fs.file-max` is the harder outer bound. Usually
caused by many concurrent connections or transfers, not by the watcher itself.

**What happens.** The watcher still tries the polling fallback (it holds fewer
descriptors); if the descriptors are truly exhausted, the client logs
`[WATCHER ERROR]` and keeps retrying with a growing backoff while periodic
resync (`RESYNC`) keeps your data reconciled.

**Diagnosis.** Check the per-process limit and the client's current
open-descriptor count (the newest `hsync` process):

```bash
ulimit -n
ls /proc/$(pgrep -n hsync)/fd | wc -l
```

**Remedy.** Raise the soft limit for the client's shell (up to its hard limit
`ulimit -Hn`) or its service unit's `LimitNOFILE=`, e.g.:

```bash
ulimit -n 4096
```

If the command fails, the hard limit is lower — raise the hard limit (or use
`LimitNOFILE=` in the unit) instead. Alternatively, reduce concurrent
connections/transfers (see the bandwidth and connection
settings in [Configuration](configuration.md)).

## A folder keeps re-syncing or reports phantom conflicts

**Symptom.** The same files keep flipping between devices, deletions come back,
or the client reports conflicts on files nobody edited. The log may show a
SQLite error (`database disk image is malformed`, `file is not a database`) or a
cycle that never converges.

**Cause.** The per-folder state database (merge bases + deletion markers) is
stale, inconsistent or unreadable. It lives under the device directory, never
inside the synced folder:

```text
$XDG_STATE_HOME/hsync/device/syncstate/<uuid>.db
```

**Remedy.** Make the folder forget its sync history so the next cycle reconciles
it from scratch:

```bash
hsync state reset --folder ~/Photos
```

The command deletes only the merge bases and the deletion markers of that
folder. Synced files, `.hsyncignore`, the `.hsyncfolder` marker, the device
identity and the trusted fingerprints are never touched, and nothing is
transferred by the command itself. It refuses while another `hsync` process is
syncing the same folder — stop that process first.

What to expect on the next sync:

- every file is scanned and hashed again, so the first cycle is slower;
- files that are identical on both sides are not transferred;
- differences are resolved with the normal causal rules, and a conflict copy is
  preserved when both sides changed;
- deletions performed shortly before the reset can be undone (a file may come
  back) because the deletion markers were forgotten.

Then check what the next sync would transfer, without moving anything:

```bash
hsync state reset --folder ~/Photos --json
hsync server server.example.com -b photos -p ~/Photos --dry-run
```

If the state database is unreadable, move it aside instead of failing (the file
is kept for inspection):

```bash
hsync state reset --folder ~/Photos --quarantine
```

`--json` prints `folder`, `folder_id`, `db`, `found`, `versions_deleted`,
`tombstones_deleted` and `quarantined` for scripts and support tickets.
