#!/bin/bash
# Prepare the tmux environment for the hsync LAN demo GIF.
#
# This script is a SETUP HELPER invoked by demo-lan.tape (the recording entry
# point: `vhs docs/assets/demo-lan.tape`). It creates the tmux session (two
# clean prompts: Device A in the top pane, Device B in the bottom pane) and
# spawns a DETACHED background watcher that writes the demo file once BOTH
# clients are READY, then returns immediately so the tape can attach to the
# session and drive the visible part of the demo — the tape types both
# `hsync lan` commands on camera. It must NOT block and must NOT invoke vhs
# (that would re-enter the tape and steal the attach keystrokes).
set -e

SOCKET="/tmp/hsync-demo-tmux.sock"
SESSION="demo"
READY_A="/tmp/hsync-a.ready"
READY_B="/tmp/hsync-b.ready"
WATCHER_PIDFILE="/tmp/hsync-demo-watcher.pid"

# Clean up any previous state
tmux -S "$SOCKET" kill-session -t "$SESSION" 2>/dev/null || true
rm -f "$SOCKET"
if [[ -f "$WATCHER_PIDFILE" ]]; then
  kill "$(cat "$WATCHER_PIDFILE")" 2>/dev/null || true
  rm -f "$WATCHER_PIDFILE"
fi

# Create directories for demo. Every path is under /tmp: one state dir and one
# throwaway HSYNC_DEVICE_DIR per simulated device, so the host's real
# installation state (~/.local/state/hsync: syncstate.db, device identity,
# pinned peers) is never read, written or deleted.
rm -rf /tmp/hsync-a /tmp/hsync-b /tmp/hsync-demo-state-a /tmp/hsync-demo-state-b
rm -rf /tmp/hsync-a-device /tmp/hsync-b-device
rm -f "$READY_A" "$READY_B"
mkdir -p /tmp/hsync-a /tmp/hsync-b /tmp/demo-bin

# Link hsync binary
SCRIPT_PATH="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
REPO_ROOT=$(realpath "$SCRIPT_PATH/../../")
ln -sf "$REPO_ROOT/.venv/bin/hsync" /tmp/demo-bin/hsync

# Create tmux session with two stacked panes. Both panes open on a clean prompt:
# the tape types each device's `hsync lan` command on camera, so the recording
# starts with the master startup visible instead of an already-scrolled log.
# Top pane: Device A (master). HSYNC_READY_FILE makes it write its PID to
# $READY_A once its HTTP+WSS servers and filesystem watcher are up (see
# hsync_client/lan_handler.py).
# HSYNC_LAN_KEY is exported in BOTH panes: a shared LAN key authorizes writes
# (a keyless LAN is read-only since ADR-0029), so the recording mirrors the
# documented quickstart without typing the key on screen.
# Each pane gets its own installation state — HSYNC_STATE_DIR plus
# HSYNC_LAN_PIN_FILE, because the pin store does not follow HSYNC_STATE_DIR (see
# hsync_client/pinning.py) — so the two simulated devices never share a
# syncstate.db or a trust store, and nothing lands in ~/.local/state/hsync.
tmux -S "$SOCKET" new-session -d -s "$SESSION" -x 200 -y 50 \
  "export PS1='user@host-a $ '; export PATH=/tmp/demo-bin:\$PATH; export HSYNC_READY_FILE=$READY_A; export HSYNC_LAN_KEY=demo-lan-key; export HSYNC_STATE_DIR=/tmp/hsync-demo-state-a; export HSYNC_LAN_PIN_FILE=/tmp/hsync-demo-state-a/lan_known_peers.json; export HSYNC_DEVICE_DIR=/tmp/hsync-a-device; bash --norc"

# Bottom pane: Device B (client) - clean prompt; the tape launches hsync later.
# HSYNC_READY_FILE=$READY_B is exported by the tape when Device B is launched.
tmux -S "$SOCKET" split-window -v -t "$SESSION" \
  "export PS1='user@host-b $ '; export PATH=/tmp/demo-bin:\$PATH; export HSYNC_LAN_KEY=demo-lan-key; export HSYNC_STATE_DIR=/tmp/hsync-demo-state-b; export HSYNC_LAN_PIN_FILE=/tmp/hsync-demo-state-b/lan_known_peers.json; bash --norc"

# Disable the status bar, pin the tmux prefix to the default (so the tape's
# Ctrl+B pane navigation is deterministic even on a host with a custom
# tmux.conf) and leave the focus on Device A, whose command is typed first.
tmux -S "$SOCKET" set-option -t "$SESSION" status off
tmux -S "$SOCKET" set-option -g prefix C-b
tmux -S "$SOCKET" select-pane -t "$SESSION:0.0"

# Background watcher: write hello.txt into Device A's dir ONLY after BOTH clients
# have signalled readiness (master: servers+watcher up; slave: initial sync done).
# This makes the GIF show live replication — the master's watchdog detects the new
# file and pushes it to the connected slave — instead of a pre-existing transfer.
(
  # Wall-clock budget: the master startup, both commands typed on camera and the
  # slave's initial sync all happen before BOTH readiness files exist. hello.txt
  # is written the moment they do; the deadline only bounds a broken recording.
  WATCHER_TIMEOUT=180
  DEADLINE=$((SECONDS + WATCHER_TIMEOUT))
  while (( SECONDS < DEADLINE )); do
    if [[ -s "$READY_A" && -s "$READY_B" ]]; then
      echo 'hello hsync' > /tmp/hsync-a/hello.txt
      exit 0
    fi
    sleep 0.5
  done
  exit 1
) &
WATCHER_PID=$!
disown 2>/dev/null || true
echo "$WATCHER_PID" > "$WATCHER_PIDFILE"

# Nothing else to do here: the recording is driven by the tape, which invokes
# this helper, attaches to the session created above, types both `hsync lan`
# commands on camera and moves the focus back to Device B for the second one.
# Regenerate the GIF from the repository root with:
#   vhs docs/assets/demo-lan.tape
