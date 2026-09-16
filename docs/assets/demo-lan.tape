# HSYNC — LAN P2P demo for the README (docs/assets/demo-lan.gif)
#
# Regenerate from the hsync-client repository root:
#   vhs docs/assets/demo-lan.tape
#
# The tape simulates two devices on one machine (stacked tmux panes):
#   top pane    = Device A (master, --force-master)
#   bottom pane = Device B (client, discovers A via mDNS, receives the file)
#
# hello.txt is NOT created up front. demo-lan.sh launches a background watcher
# that writes it into Device A's directory only after BOTH clients signal
# readiness (HSYNC_READY_FILE: master once its servers+watcher are up, slave
# after its initial sync), so the GIF shows the file being replicated live.
#
# Everything that is not real tool usage (tmux setup, control send-keys,
# teardown) is wrapped in Hide/Show blocks and never appears in the GIF.
# The tmux status bar is disabled.
# Both panes export the same throwaway HSYNC_LAN_KEY (set by demo-lan.sh):
# a keyless LAN is read-only, and the shared key is what authorizes writes.
#
# Both `hsync lan` commands are typed on camera — Device A (master) first, then
# Device B — and each simulated device owns its own installation state under
# /tmp (HSYNC_STATE_DIR, HSYNC_LAN_PIN_FILE and HSYNC_DEVICE_DIR, all set by
# demo-lan.sh), so nothing under the host's real ~/.local/state/hsync is ever
# read, written or deleted and the two peers never share a syncstate.db or a
# trust store.

Output docs/assets/demo-lan.gif

Set Shell bash
Set FontSize 11
Set Width 1200
Set Height 760
Set Margin 12
Set Padding 20
Set Framerate 15
Set TypingSpeed 150ms

# --- hidden setup: clean start, no leftovers, neutral demo environment ------
# demo-lan.sh returns immediately after creating the tmux session (two clean
# prompts) and spawning the readiness watcher. Device A has the focus, so the
# recording opens on an idle two-pane terminal and the master command below is
# typed on camera.
Hide
# Hidden typing runs instantly; the on-camera commands keep the Set TypingSpeed
# cadence below (150ms), which is restored before the first Show.
Set TypingSpeed 10ms
Type "bash docs/assets/demo-lan.sh" Enter
Sleep 1500ms
Type "tmux -S /tmp/hsync-demo-tmux.sock attach -t demo" Enter
Sleep 1200ms
Set TypingSpeed 150ms
Show
Sleep 400ms

# --- Device A (top pane): the master startup, typed on camera ----------------
# The environment is already exported in the pane by demo-lan.sh, so the typed
# command matches the documented quickstart. HSYNC_READY_FILE makes the master
# write its PID to /tmp/hsync-a.ready once its HTTP+WSS servers and filesystem
# watcher are up, which is what the watcher waits for before creating hello.txt.
Type "hsync lan -b mybucket -p /tmp/hsync-a --force-master" Enter
Sleep 6000ms

# --- Device B (bottom pane): joins, discovers A via mDNS ----------------------
# Ctrl+B then Down is handled by the tmux client and never reaches the running
# hsync process: it moves the focus from Device A back to Device B.
# HSYNC_READY_FILE makes Device B write its PID once its initial sync completes,
# which is what the watcher in demo-lan.sh waits for before creating hello.txt.
# Once BOTH are READY the watcher drops hello.txt into Device A's dir and the
# master pushes it to Device B live. The wait covers the slave's initial sync
# plus the master's file-stability delay (5s by default) before the upload.
Hide
Set TypingSpeed 10ms
Ctrl+B
Down
Sleep 300ms
Type "export HSYNC_DEVICE_DIR=/tmp/hsync-b-device HSYNC_READY_FILE=/tmp/hsync-b.ready" Enter
Set TypingSpeed 150ms
Ctrl+L
Sleep 300ms
Show
Type "hsync lan -b mybucket -p /tmp/hsync-b" Enter
Sleep 12000ms

# --- proof on Device B: stop the agent, show the received file ---------------
Ctrl+C
Sleep 800ms
Type "ls -l /tmp/hsync-b" Enter
Sleep 1200ms
Type "cat /tmp/hsync-b/hello.txt" Enter
Sleep 4000ms

# --- hidden teardown ---------------------------------------------------------
# Stop the watcher (best-effort: it exits on its own after writing hello.txt)
# and clean up the readiness files alongside the throwaway device dirs.
Hide
Type "tmux -S /tmp/hsync-demo-tmux.sock send-keys -t demo:0.0 C-c" Enter
Sleep 500ms
Type "tmux -S /tmp/hsync-demo-tmux.sock kill-session -t demo" Enter
Type "kill $(cat /tmp/hsync-demo-watcher.pid 2>/dev/null) 2>/dev/null; rm -f /tmp/hsync-demo-watcher.pid /tmp/hsync-a.ready /tmp/hsync-b.ready" Enter
Type "rm -rf /tmp/hsync-a-device /tmp/hsync-b-device" Enter
Show
