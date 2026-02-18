#!/bin/bash
# Prepare the tmux environment for the hsync LAN demo GIF.
#
# This script is a SETUP HELPER invoked by demo-lan.tape (the recording entry
# point: `vhs docs/assets/demo-lan.tape`). It creates the tmux session (Device A
# master in the top pane, a clean prompt for Device B in the bottom pane) and
# spawns a DETACHED background watcher that writes the demo file once BOTH
# clients are READY, then returns immediately so the tape can attach to the
# session and drive the visible part of the demo. It must NOT block and must NOT
# invoke vhs (that would re-enter the tape and steal the attach keystrokes).
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

# Create directories for demo
rm -rf /tmp/hsync-a /tmp/hsync-b "$HOME/.local/state/hsync"
rm -rf /tmp/hsync-a-device /tmp/hsync-b-device
rm -f "$READY_A" "$READY_B"
mkdir -p /tmp/hsync-a /tmp/hsync-b /tmp/demo-bin

# Link hsync binary
SCRIPT_PATH="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
REPO_ROOT=$(realpath "$SCRIPT_PATH/../../")
ln -sf "$REPO_ROOT/.venv/bin/hsync" /tmp/demo-bin/hsync

# Create tmux session with two stacked panes
# Top pane: Device A (master) - starts hsync automatically.
# HSYNC_READY_FILE makes the master write its PID to $READY_A once its HTTP+WSS
# servers and filesystem watcher are up (see hsync_client/lan_handler.py).
tmux -S "$SOCKET" new-session -d -s "$SESSION" -x 200 -y 50 \
  "export PS1='user@host-a $ '; export PATH=/tmp/demo-bin:\$PATH; export HSYNC_READY_FILE=$READY_A; HSYNC_DEVICE_DIR=/tmp/hsync-a-device hsync lan -b mybucket -p /tmp/hsync-a --force-master"

# Bottom pane: Device B (client) - shows clean prompt, hsync started manually later.
# HSYNC_READY_FILE=$READY_B is exported by the tape when Device B is launched.
tmux -S "$SOCKET" split-window -v -t "$SESSION" \
  "export PS1='user@host-b $ '; export PATH=/tmp/demo-bin:\$PATH; bash --norc"

# Disable status bar
tmux -S "$SOCKET" set-option -t "$SESSION" status off

# Background watcher: write hello.txt into Device A's dir ONLY after BOTH clients
# have signalled readiness (master: servers+watcher up; slave: initial sync done).
# This makes the GIF show live replication — the master's watchdog detects the new
# file and pushes it to the connected slave — instead of a pre-existing transfer.
(
  WATCHER_TIMEOUT=90
  WAITED=0
  while [[ $WAITED -lt $WATCHER_TIMEOUT ]]; do
    if [[ -s "$READY_A" && -s "$READY_B" ]]; then
      echo 'hello hsync' > /tmp/hsync-a/hello.txt
      exit 0
    fi
    sleep 0.5
    WAITED=$((WAITED + 1))
  done
  exit 1
) &
WATCHER_PID=$!
disown 2>/dev/null || true
echo "$WATCHER_PID" > "$WATCHER_PIDFILE"

# Generate GIF
vhs "$SCRIPT_PATH/demo-lan.tape"
