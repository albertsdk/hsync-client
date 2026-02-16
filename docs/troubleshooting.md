# HSYNC Client — Troubleshooting

Task-focused fixes for problems you can observe in the client's logs or
behaviour. Symptom → cause → remedy, with copy-paste commands.

> The log labels referenced here (e.g. `[WATCHER ERROR]`) are catalogued in
> [Log Labels](log-labels.md).

## The file watcher cannot start (inotify limits)

**Symptom.** The client log shows a parsable warning and the client cannot
enter real-time sync (it keeps repeating its initial sync instead of switching
to "watching"):

```
[WATCHER ERROR] cannot start the file watcher for '/path/to/bucket': inotify
instance limit reached (fs.inotify.max_user_instances): ...
```

On older client versions the same condition surfaced as a raw kernel error
inside a generic disconnect line:

```
WebSocket connection ended: OSError: [Errno 24] inotify instance limit reached
```

**Cause.** On Linux the file watcher is built on the kernel's inotify API,
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

When either limit is hit, `inotify_init`/`inotify_add_watch` fails with
`EMFILE` (Errno 24, instances) or `ENOSPC` (Errno 28, watches) and the kernel
refuses to create the watcher; retrying quickly cannot help, so the client
backs off for 60 s between attempts and keeps the error visible.

**Diagnosis.** Check the current limits and how many instances are already
held by your user's processes:

```bash
cat /proc/sys/fs/inotify/max_user_instances
cat /proc/sys/fs/inotify/max_user_watches
find /proc/[0-9]*/fd -lname 'anon_inode:*inotify*' 2>/dev/null | wc -l
```

If the instance count is close to `max_user_instances`, the per-user pool is
saturated — this is a machine-level condition, not an hsync bug.

**Remedy — raise the limits (recommended).** Give your user enough headroom:

```bash
sudo sysctl fs.inotify.max_user_instances=512
```

To make the change persistent across reboots:

```bash
echo fs.inotify.max_user_instances=512 | sudo tee /etc/sysctl.d/99-hsync-inotify.conf
sudo sysctl --system
```

`max_user_watches` is raised the same way (e.g. `1048576` for very large
trees): replace the name in both commands.

**Remedy — free instances instead.** If you would rather not change kernel
settings, close other watcher-heavy applications (secondary editor windows,
idle browsers, unused desktop services) until the count drops below the
limit; the client recovers by itself on its next attempt.

**Notes.**

- Inside containers the inotify limits are the **host kernel's**: raising
  them inside the container sysfs is not effective — tune the host (or run
  the client with host networking where the sync tree lives on the host).
- One hsync process holds one instance regardless of how many
  sub-directories it watches — running several hsync folders on the same
  machine costs one instance each.
- While the watcher cannot start, the client stays in its backoff loop; once
  another process frees an instance (or you raise the limit), it resumes
  normal real-time sync without a restart.
