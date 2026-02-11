# Scheduling & Automation (cron / systemd)

One-shot sync is HSYNC's primary interface for automation. A one-shot run
performs a single full sync cycle — compare, upload, download, delete — and
then exits, making the outcome scriptable through the process **exit code**.
This page covers that contract and provides ready-to-use cron and systemd
examples.

## One-shot mode at a glance

Continuous/daemon mode is the **default**; for automation pass the dedicated
`--once` flag to run a single cycle and exit:

```bash
# One-shot: run one cycle and exit (required for cron/systemd timers)
hsync -b docs -s https://server.example.com --once
```

Without `--once` the client keeps running and retries an unreachable server
forever, so schedulers must always pass `--once`.

In **LAN P2P Mode** the same `--once` flag runs one slave sync cycle and
exits:

```bash
hsync -b docs -p ~/Sync --lan-mode --once
```

> **Note:** the LAN master role is inherently continuous (it must stay
> available to serve peers). One-shot LAN runs apply to the slave side:
> keep the master running and start the slave with `--once`.

Flags that make a run non-interactive and safe for schedulers:

| Flag | Purpose |
| ---- | ------- |
| `-y` / `--yes` | Accept confirmation prompts automatically (sysAdmins keep warnings on stderr). |
| `--env-file FILE` | Load `HSYNC_*` secrets from a file instead of the crontab. |
| `-o FILE` / `--output` | Write the log to a file (or the systemd journal when omitted). |
| `-n` / `--dry-run` | Validate a new bucket before scheduling it for real. |

## Exit codes

The exit status of a one-shot run is part of the public contract; scripts and
schedulers must rely on it rather than parsing log output.

| Code | Meaning | Operator action |
| ---- | ------- | ---------------- |
| `0` | Sync completed; no anomalies (also a successful dry-run). | None. |
| `1` | Fatal error: invalid configuration, unreachable server, authentication failure, directory lock held by another process, or an unexpected exception. | Inspect the log; fix configuration. |
| `2` | The cycle completed but **some transfers failed**. Those files are retried on the next run. | Re-run; alert if it persists. |
| `3` | The **mass-deletion guard** tripped: some unconfirmed deletions were skipped this cycle. | Review before they can proceed. |
| `4` | **Causal conflicts** were resolved by preserving `*_conflicted_copy_*` files. | Review the preserved copies. |
| `5` | **LAN one-shot only**: no peer/master was found within the discovery timeout; nothing was synchronized. | Verify the master is reachable. |

When several conditions co-exist, priority is:
failed transfers (`2`) > deletion guard (`3`) > conflicts (`4`).

Check the code in a shell as usual:

```bash
hsync -b docs -s https://server.example.com --once -y
echo $?
```

or branch on it:

```bash
hsync -b docs -s https://server.example.com --once -y || echo "sync failed"
```

## Best practices for automation

1. **Use an env file for secrets, not the command line.** The crontab is
   world-readable to administrators and the API key would appear in
   `ps` output. Keep it in a mode-`0600` file:

   ```bash
   install -m 600 /dev/null /etc/hsync/hsync.env
   cat > /etc/hsync/hsync.env <<'EOF'
   HSYNC_SERVER="https://hsync.example.com:9753"
   HSYNC_API_KEY="change-me"
   EOF
   ```

   and reference it via `--env-file`.

   For systemd-adjacent integrations, the parent process can pass the key
   over a file descriptor with `--api-key-fd` so it never touches disk.

2. **Always pass `-y`** in schedulers: prompts abort in non-interactive
   contexts, which is correct, but a scheduled job should accept the
   documented safety warnings and log them.

3. **Prevent overlapping jobs.** Two `hsync` processes on the same local
   directory corrupt each other's state, so the client refuses to start
   when an instance lock is held (fatal exit code `1`). With systemd timers
   use `Persistent=true`; with cron set an interval larger than the worst
   expected cycle and treat repeated lock exits as a symptom of overlapping
   schedules.

4. **Let the exit code drive alerting, not log scraping** (see the table
   above). With cron, anything printed to stderr is mailed; with systemd,
   `OnFailure=` triggers a unit that can page someone.

## cron

Entry example — sync every hour, secrets in an env file, log to a dedicated
file:

```cron
# /etc/cron.d/hsync — hourly one-shot sync
0 * * * * hsyncuser hsync -b documents -p /data/documents \
    --env-file /etc/hsync/hsync.env --once -y -o /var/log/hsync/documents.log
```

Notes:

* Run as a dedicated user (`hsyncuser`) that owns the synced directory;
  that user must also be able to read `/etc/hsync/hsync.env`.
* The log path and its directory must exist and be writable by the user
  (create it in advance, e.g. with `install -d -o hsyncuser /var/log/hsync`).
* Any remaining stderr output (warnings from the mass-deletion guard or the
  disk-space check) is picked up by cron's built-in mail mechanism — useful
  as a basic alert channel. Redirect stderr to the log too (`2>&1`) if mail
  is not configured.
* To trial a new bucket before scheduling it, run a dry-run first:

  ```bash
  sudo -u hsyncuser hsync -b newbucket -p /data/newbucket \
      --env-file /etc/hsync/hsync.env --dry-run
  ```

### LAN one-shot via cron

Schedules a slave that syncs against the LAN master and exits (exit `5`
means "no peer found"). A finite `--discovery-timeout` bounds the wait for
the first peer (the default is to wait indefinitely):

```cron
0 2 * * * hsyncuser HSYNC_LAN_KEY="shared-secret" hsync -b shared \
    -p /data/shared --lan-mode --once --discovery-timeout 30 -y
```

## systemd timer

Where systemd is available, a **timer** is preferable to cron: journal
logging, failure notifications, persistent catch-up for missed runs, and
exact resource control. Two units make a scheduled sync:

`/etc/systemd/system/hsync-documents.service`:

```ini
[Unit]
Description=HSYNC one-shot sync of documents
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=hsyncuser
Group=hsyncuser
EnvironmentFile=/etc/hsync/hsync.env
ExecStart=/usr/local/bin/hsync -b documents -p /data/documents --once -y
# Extra hardening (the service only touches its data directories)
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/data/documents
PrivateTmp=true
NoNewPrivileges=true
```

`/etc/systemd/system/hsync-documents.timer`:

```ini
[Unit]
Description=Hourly HSYNC sync of documents

[Timer]
OnCalendar=hourly
# Run missed jobs at boot instead of dropping them
Persistent=true
# Prevent two overlapping runs of the same unit
Unit=hsync-documents.service

[Install]
WantedBy=timers.target
```

Enable it with:

```bash
systemctl daemon-reload
systemctl enable --now hsync-documents.timer
```

### Alerting on failures

Add `OnFailure=` to the service so systemd triggers a notification unit
whenever the one-shot exits non-zero (transfer errors, guard trips,
conflicts, lock held):

```ini
[Unit]
Description=HSYNC one-shot sync of documents
Wants=network-online.target
After=network-online.target
OnFailure=hsync-alert@%i.service
```

with a generic `hsync-alert@.service` that executes a notifier (email,
webhook, pushover, …) using the failed unit name and the journal tail:

```ini
[Unit]
Description=HSYNC failure alert for %i

[Service]
Type=oneshot
ExecStart=/usr/local/bin/hsync-alert.sh %i
```

### Trigger a run manually (same code path as the timer)

```bash
systemctl start hsync-documents.service   # one cycle now
systemctl status hsync-documents.service  # Success or exit code in journal
systemctl list-timers hsync-documents.timer
```

## See also

- [CLI Options](configuration.md) — full reference of every flag.
- [Sync Modes](sync-modes.md) — one-shot vs continuous watch.
- [LAN Mode](lan-mode.md) — peer discovery and one-shot `--once` runs.
- [Server Mode](server-mode.md) — server-mode setup.
