# HSYNC — LAN P2P demo for the README (docs/assets/demo-lan.gif)
#
# Regenerate from the hsync-client repository root:
#   vhs demo-lan.tape
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
# Temporary state lives under /tmp and throwaway HSYNC_DEVICE_DIRs, so the
# host's real device identity is never touched.

Output demo-lan.gif

Set Shell bash
Set FontSize 11
Set Width 1200
Set Height 760
Set Margin 12
Set Padding 20
Set Framerate 15
Set TypingSpeed 5ms

# --- hidden setup: clean start, no leftovers, neutral demo environment ------
# demo-lan.sh returns immediately after creating the tmux session and spawning
# the readiness watcher; this pause lets Device A (master) reach READY so the
# visible recording opens on an already-running coordinator.
Hide
Type "bash docs/assets/demo-lan.sh" Enter
Sleep 4000ms
Type "tmux -S /tmp/hsync-demo-tmux.sock attach -t demo" Enter
Sleep 1500ms
Show
Sleep 2000ms

# --- Device B (bottom pane): joins, discovers A via mDNS ----------------------
# HSYNC_READY_FILE makes Device B write its PID once its initial sync completes,
# which is what the watcher in demo-lan.sh waits for before creating hello.txt.
# Once BOTH are READY the watcher drops hello.txt into Device A's dir and the
# master pushes it to Device B live (visible in the logs within a few seconds).
Type "HSYNC_DEVICE_DIR=/tmp/hsync-b-device HSYNC_READY_FILE=/tmp/hsync-b.ready hsync -b mybucket -p /tmp/hsync-b --lan-mode" Enter
Sleep 12000ms

# --- proof on Device B: stop the agent, show the received file ---------------
Ctrl+C
Sleep 800ms
Type "ls -l /tmp/hsync-b" Enter
Sleep 1200ms
Type "cat /tmp/hsync-b/hello.txt" Enter
Sleep 7000ms

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
