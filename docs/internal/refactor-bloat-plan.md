# HSYNC Client — Code-bloat cleanup: implementation plan

> **Status: Plan (Rev. 1, 2026-10-30). All sections implemented — §2, §5.1, §5.3, §5.2, §5.4 done.**
> The low-risk items (`7893eee`), §5.1 (`3633180`), §5.3 (`0690ebc`), §5.2
> (`e45bdb0`) and §5.4 (RunOptions `a9a81dd` + phase extraction below) are
> committed; all four oversized functions now meet their line/CC targets.
> This document records the analysis behind the completed work.

## 1. Context and goal

The client is 37.4k lines across 75 modules. A full scan for bloat
(dead code, duplication, over-engineering, verbosity) found:

- **Dead code / unused imports: clean.** `ruff check --select F` passes; an AST
  scan of the whole monorepo found 0 unreferenced top-level functions/classes,
  0 unreferenced module constants, 0 statements after a terminator, and 0
  `TODO/FIXME/HACK` markers. The 14 "unreferenced" functions are Typer commands
  registered by decorator. **Do not re-run this analysis looking for dead code;
  there is none.**
- **Verbosity: mostly handled by the linter.** `E,F,W,I,UP,B,C4,SIM` are enabled
  and pass, so modern-syntax and simplification opportunities are already gone.
- **Real remaining debt: four oversized functions** (plus one already fixed,
  `get_sync_actions`). This plan targets those.

## 2. Already implemented (committed as `7893eee` on 2026-10-30)

Committed in `hsync-client` before the larger decompositions; the four source
and wiki files (`git diff --stat` = +244/−175) are:

| Change | Location | Notes |
| ------ | -------- | ----- |
| `_emit_auth_error(action, op, filepath)` | `hsync_client/transfer.py` (§ `_emit_transfer_failed`) | Dedupes the log + `auth_error` event pair; used by download 401/403 and upload 401. Upload 403 stays log-only (unchanged behaviour). |
| `_load_env_file_or_exit(env_file)` | `hsync_client/cli.py` (before `_common_run_sync_kwargs`) | Replaces the 8-line `--env-file` block duplicated in `p2p` and `server`. |
| `_handle_upload_response` → `match` | `hsync_client/transfer.py` | 6-way status dispatch; behaviour identical. |
| `SyncActionName` dispatch → `match` | `hsync_client/sync.py` (the `for a in sync_actions` loop in `sync`) | 7-way dispatch; behaviour identical. |
| `get_sync_actions` split | `hsync_client/sync.py` | Now a 71-line / CC 5 orchestrator; passes extracted into `_actions_from_remote` (returns actions + remote path set) and `_actions_from_local`. |
| Wiki note | `llm-wiki/modules/sync-core.md` | Documents the two helper passes. |

Verification done: `ruff check hsync_client`, `ruff format --check`,
`mypy` on the three modules, and the full suite (**2557 passed, 226 skipped**).

## 3. Rejected / revised proposals

- **Shared Typer option signatures for `p2p` and `server`: do NOT attempt.**
  - Injecting parameters into `__signature__` does not create the function
    locals the bodies use (`local_path`, `once`, …), so Typer's call fails.
  - Moving them to the app callback would change the CLI surface
    (`hsync --opt p2p` instead of `hsync p2p --opt`).
  - The repo already encodes this: `tests/test_cli_option_parity.py` states
    *"Typer requires every option to be declared explicitly in each command
    signature"* and guards common-option parity. Keep the declarations; only the
    executable `env_file` block was deduped (§2).
- **`event_dispatch.py:363` `if/elif` → `match`: skipped.** Only 4 branches,
  intertwined with the preceding src/dst special-casing; low value, non-trivial
  risk. Revisit only if that function grows.
- **`utils/__init__.py` 156-line re-export facade: keep.** It is a documented
  backward-compatibility contract (`from hsync_client.utils import <name>`).

## 4. Measured hot spots (current line numbers)

| Function | Location | Lines | Cyclomatic | Notes |
| -------- | -------- | ----: | ---------: | ----- |
| `run_sync` | `hsync_client/main_logic.py` (**done, §5.4**) | 58 | 2 | `RunOptions` 1-param + phase helpers (`_init_runtime`, `_resolve_endpoints`, `_resolve_identity`, `_resolve_secrets`, `_resolve_autotune`, `_build_config`, `_finish_startup`, `_run_remote_mode`); was 666 / 101 / 57 params |
| `sync` | `hsync_client/sync.py` (**done, §5.3**) | 171 | 13 | orchestrator delegating 10 phase helpers (was 512 / 67) |
| `_download_from_source_attempt` | `hsync_client/transfer.py` (**done, §5.1**) | 80 | 11 | orchestrator over `_prepare_download_target` / `_open_download_stream` / `_stream_download_body` (was 336 / 55) |
| `_handle_upload` | `hsync_client/p2p_server.py` (**done, §5.2**) | 66 | 10 | orchestrator over `_admit_upload` / `_open_upload_destination` / `_close_upload_stream` / `_maybe_partial_upload_response` / `_finalize_upload` (was 401 / 66) |

Metrics were produced with a small AST script (CC counts `if/for/while/except`,
boolean operands, ternaries and `match` cases). Re-run it after each change to
confirm the target drops and nothing else regresses.

## 5. Deferred work

### 5.1 `transfer.py::_download_from_source_attempt` (implemented 2026-10-30: 336→80 lines, CC 55→11)

**Why it is bloat.** One linear pipeline mixes destination resolution, delta
and resume negotiation, the request build, response validation, body streaming,
digest verification and cleanup in a single function, with a nested cleanup
path.

**Current phase map (top-level statements).**

1. `t0`, destination + `.part` path, `dest_existed`/`dest_marker` snapshot.
2. Already-up-to-date shortcut (`_already_up_to_date`).
3. Delta attempt (`_try_delta_download`, `delta_only`).
4. Resume offset (`_resolve_resume_offset`, `force_restart`).
5. Disk/inbound-size guards (`_has_download_space`, `MAX_INBOUND_SIZE`).
6. Request build (`_build_download_request`).
7. Response validation (`_accept_download_response`).
8. Body stream: partial file, `_CountingReader`, sha256, zstd/inflate decode,
   progress, wire-vs-declared-size checks.
9. Finalize: atomic replace, mtime, permissions, deletion-marker snapshot,
   cleanup/sweep of the in-flight registry.

**Proposed extraction.** Keep `_download_from_source_attempt` as the orchestrator
(same name/signature) and extract:

- `_prepare_download_target(file) -> _DownloadTarget` where `_DownloadTarget` is
  a small dataclass (`full_save_path`, `partial_path`, `dest_existed`,
  `dest_marker`).
- `_try_delta_or_resume(file, target, *, allow_delta, delta_only, force_restart,
  inventory_observer, t0) -> ResumePlan | None` (returns "skip" vs an offset).
- `_open_download_stream(file, target, resume_offset) -> (response,
  content_length, counter)` — request build + `_accept_download_response` +
  status-to-exception mapping.
- `_stream_download_body(...) -> _StreamResult` (`written`, `sha256`,
  `wire_bytes`) — the decode loop only.
- `_commit_download(file, target, result, ...)` — atomic replace, mtime,
  permissions, marker snapshot, in-flight release.

**Pitfalls.** The cleanup ordering is load-bearing: the in-flight partial is
released last, and deletion-marker snapshots must be taken before/after the body
exactly as today. Preserve the `finally` semantics; do not move the
`register_inflight_partial`/release boundary. Keep the wire-vs-file size
distinction.

**Tests.** `tests/test_transfer_download.py`, `tests/test_transfer_progress.py`,
`tests/test_delta_transfer.py`, `tests/test_transfer_pools.py`,
`tests/test_transfer_upload_lifecycle.py`. Add a focused test for the new
`_stream_download_body` seams only if coverage of a branch is unclear — prefer
behaviour-level tests.

**Acceptance.** Same tests green; `_download_from_source_attempt` ≤ ~120 lines,
CC ≤ ~25; no behaviour change in the size-mismatch / retry / cleanup paths.

---

### 5.2 `p2p_server.py::_handle_upload` (implemented 2026-10-30: 401→66 lines, CC 66→10)

**Why it is bloat.** Multipart parsing, free-space admission, an inner
`_file_open` closure that validates form fields and resolves the destination
(conflict/marker/mtime rules), the streaming write, digest/length validation,
commit and the response all live in one method.

**Current phase map.**

1. `_parse_multipart_request` + content length/boundary.
2. `check_free_space` admission (413 on error).
3. `_StreamingMultipartParser` setup; state vars (`dest_path`, `tmp_path`,
   `raw_file`, `out_file`, `response_sent`, `dest_existed_at_start`,
   `dest_marker_snapshot`).
4. Inner `_file_open(form_fields, field_name, filename)` closure: form
   validation, destination resolution, conflict/tombstone rules, temp file.
5. `try:` stream fields → write chunks → validate size/sha → commit → respond;
   `except`/`finally` cleanup.

**Proposed extraction.**

- `_UploadTarget` dataclass for `(dest_path, tmp_path, dest_existed_at_start,
  dest_marker_snapshot)`.
- `_admit_upload(parser) -> int | None` (free-space check + error response).
- `_open_upload_destination(form_fields, filename, local_bucket) ->
  _UploadTarget` (lift `_file_open` to a method; it closes over request state,
  so pass what it needs explicitly).
- `_stream_upload_fields(parser, target) -> _UploadResult` (chunks, digest,
  size).
- `_commit_upload(target, result)` and `_respond_upload(...)`.

**Pitfalls.** The inner closure currently mutates the outer state
(`result["sent"]`, `dest_existed_at_start`, `dest_marker_snapshot`) and sends
error responses from inside; make those explicit (return values or a small
context object). Preserve 202 partial semantics, marker snapshots and the
`finally` cleanup order. This is the riskiest of the four — schedule it last.

**Tests.** `tests/test_p2p_server_resumable_upload.py`,
`tests/test_p2p_server_endpoints_advanced.py`,
`tests/test_p2p_server_endpoints_mutations.py`,
`tests/test_p2p_server_compression_cap.py`,
`tests/test_p2p_server_progress.py`.

**Acceptance.** Same tests green; `_handle_upload` ≤ ~150 lines, CC ≤ ~30.

---

### 5.3 `sync.py::sync` (implemented 2026-10-30: 512→171 lines, CC 67→13)

**Why it is bloat.** The body is a single long `try` under
`_cycle_bookkeeping_lock`; a reader cannot see the cycle boundaries. Complexity
is not in the number of top-level statements but in the nested phases.

**Current phase map (from the section comments).**

1. Storage guard (pre-emptive free-space check).
2. Offline-deletion markers + causal provenance (ADR-0003).
3. Snapshot remote/local listings; merge-base retention for excluded paths.
4. Tombstone merge + `get_sync_actions`.
5. Offline rename detection (local→remote and remote→local).
6. Remote-deletion handling + mass-delete guard.
7. Symlink structural replication (inline, sequential).
8. Execute actions: downloads/uploads/deletes under the worker pool/semaphore.
9. Finalize cycle (bookkeeping timestamps, stats, event emission).

**Proposed extraction.** Keep `sync` as the orchestrator and extract the phases
that already have comment banners:

- `_record_offline_deletions(...) -> DeletionMarkers`
- `_collect_listings(...) -> (remote_files, local_files)`
- `_detect_offline_renames(...)` (both directions)
- `_apply_remote_deletions(...)` (includes the mass-delete guard)
- `_replicate_symlinks(...)`
- `_execute_sync_actions(...)` (the pool/concurrency block)
- `_finalize_cycle(...)`

**Pitfalls.** `sync` sets and clears module globals
(`_sync_cycle_active`, `_last_cycle_ended_at`, `_sync_cycle_started_at`); keep
those in the orchestrator, not in helpers, or pass explicit state. Ordering of
the rename/delete/mass-delete phases is behaviourally significant. Dry-run
(`persist_state=False`) must stay pure.

**Tests.** `tests/test_sync_remote.py`, `tests/test_sync_local.py`,
`tests/test_sync_workers.py`, `tests/test_sync_permanent_failures.py`,
`tests/test_sync_keep_policy.py`, `tests/test_initial_sync_status.py`,
`tests/test_p2p_sync.py`.

**Acceptance.** Same tests green; `sync` ≤ ~180 lines, CC ≤ ~25; cycle
bookkeeping and event order unchanged.

---

### 5.4 `main_logic.py::run_sync` (implemented 2026-10-30: 666→58 lines, CC 101→2, 57 params → 1 `RunOptions`)

**Why it is bloat.** The 57-argument "funnel signature" is threaded from the CLI
through `_common_run_sync_kwargs` into `run_sync`, and the body is a linear
startup script (secrets, emitter, logging, endpoints, TLS trust, autotune,
instance lock, P2P rendezvous, run loop). Every new flag touches four places.

**Current phase map.**

1. Secrets from file descriptors; emitter init; `--env-file`.
2. Logging setup + PyInstaller/netrc workaround.
3. Local path / logfile validation.
4. Endpoint resolution (server URL vs P2P placeholder), CID/session event.
5. API key / P2P key / passphrase resolution + security gates
   (no cleartext, weak-key check).
6. Autotune resolution (timeouts/retries/concurrency provenance).
7. TLS trust (rendezvous + server pins/TOFU).
8. One-shot vs continuous derivation.
9. Ignore matcher + hot reload; instance lock; engine-running marker.
10. P2P rendezvous + master election, or server connectivity.
11. Run loop (one-shot / continuous) and shutdown.

**Proposed design (staged).**

1. Add `hsync_client/run_options.py` with a `@dataclass(slots=True)`:
   ```python
   @dataclass(slots=True)
   class RunOptions:
       secrets: SecretsOptions
       logging: LoggingOptions
       endpoints: EndpointOptions
       sync: SyncOptions
       p2p: P2POptions
       transfers: TransferOptions
       safety: SafetyOptions
   ```
   Group the current keyword arguments into those sub-dataclasses (fields keep
   their names so the mapping is mechanical).
2. Change `_common_run_sync_kwargs` (and `cli.py`) to build a `RunOptions`
   instead of a raw dict; change `run_sync(options: RunOptions)`.
3. Keep a temporary back-compat path for tests: either a
   `run_sync(**kwargs) -> run_sync(RunOptions.from_kwargs(**kwargs))` shim behind
   a private name, or update the four `tests/test_main_logic_*.py` call sites in
   the same change. Prefer updating the tests (there are only four files) so the
   shim does not become permanent.
4. Extract phases: `_init_runtime(options)`, `_resolve_secrets(options)`,
   `_resolve_endpoints(options)`, `_resolve_autotune(options)`,
   `_resolve_trust(options)`, `_acquire_run_lock(options)`,
   `_run_p2p(options, ...)` / `_run_server(options, ...)`.

**Pitfalls.** `run_sync` mutates the global `CONFIG` and installs signal
handlers; keep those in the orchestrator. The CLI contract (flags/env) must not
change. This is the highest-blast-radius change: do it last, behind a green
suite, and keep each step committable.

**Tests.** `tests/test_main_logic_sync.py`, `tests/test_main_logic_p2p.py`,
`tests/test_main_logic_preview.py`, `tests/test_main_logic_deletion_risk.py`,
`tests/test_cli.py`, `tests/test_cli_option_parity.py`, plus the full suite.

**Acceptance.** Same tests green; `run_sync` parameter count 1 (a `RunOptions`),
body ≤ ~120 lines; CLI and env behaviour byte-for-byte equivalent.

## 6. Execution order and per-step workflow

1. **Commit §2** (the already-done work) ✅ `7893eee`
2. `_download_from_source_attempt` (§5.1) ✅ `3633180` — best seams, strong tests.
3. `sync` (§5.3) ✅ `0690ebc` — phase extraction only.
4. `_handle_upload` (§5.2) ✅ `e45bdb0` — riskiest of the "medium" group.
5. `run_sync` + `RunOptions` (§5.4) ✅ `8d579de` — RunOptions + phase extraction complete; run_sync 666→58 lines, CC 101→2.

For each step:

1. Re-run the metrics script (§10) and record before/after.
2. Extract **one** helper at a time; move the code verbatim, changing only
   names/indirection.
3. Run the function's targeted tests after every extraction.
4. Run `ruff check`, `ruff format --check`, `mypy` on the touched modules.
5. Run the full suite before moving on.
6. Update `llm-wiki` / `docs/internal` if a documented symbol or flow changes.
7. Commit as its own focused change (`refactor(transfer): …`,
   `refactor(sync): …`, `refactor(p2p): …`, `refactor(startup): …`).

Do not bundle two of the four functions in one commit.

## 7. Cross-project / public-surface constraints

- The public surface is the `hsync` CLI (flags/env), the events-JSON protocol and
  the exit codes. `hsync-desktop` drives the client **as a subprocess** and
  `hsync-wd-os5` packages it; neither imports internal symbols. `hsync-server`
  imports `hsync_client` only in `tests/test_bb_client_interop.py`.
- Therefore internal refactors are safe as long as the CLI/flag/env contract and
  the event payloads do not change. Any change to a flag, its default, or an
  event payload must update `hsync-desktop` and the interop test in the same
  change.
- `tests/test_cli_option_parity.py` is the guard for the `p2p`/`server` option
  surface: keep it passing.

## 8. Documentation to update

- `llm-wiki/modules/sync-core.md` — already updated for the `get_sync_actions`
  split; extend it if the `sync()` phase helpers get documented.
- `llm-wiki/modules/p2p-mode.md` — `_handle_upload` is referenced there; update
  the symbol map if the handler is split.
- `llm-wiki/modules/remote-mode.md` / `cli-config.md` — only if the startup
  phases or the CLI mapping are renamed.
- This file (§2/§5) — tick items as they land.

## 9. Definition of done

- All four functions meet their line/CC acceptance targets.
- Full client suite green (currently 2557 passed / 226 skipped), plus `ruff`,
  `ruff format --check`, `mypy` clean.
- No public-surface change (CLI/env/events/exit codes); interop test green.
- Each refactor landed as its own focused commit with a before/after metric note.

## 10. Commands

```bash
cd hsync-client

# Lint / types
.venv/bin/ruff check hsync_client
.venv/bin/ruff format --check hsync_client
.venv/bin/mypy hsync_client/<module>.py

# Targeted tests (adjust per step)
.venv/bin/python -m pytest tests/test_transfer_download.py tests/test_delta_transfer.py -q
.venv/bin/python -m pytest tests/test_sync_remote.py tests/test_sync_local.py tests/test_sync_workers.py -q
.venv/bin/python -m pytest tests/test_p2p_server_resumable_upload.py tests/test_p2p_server_progress.py -q
.venv/bin/python -m pytest tests/test_main_logic_sync.py tests/test_main_logic_p2p.py tests/test_cli.py -q

# Full suite
.venv/bin/python -m pytest tests -q -p no:cacheprovider
```

Metrics script (paste into `python - <<'PY'`), reporting line length, cyclomatic
complexity, nesting depth and top-level statement count for the four targets —
reuse the one used to produce §4 (adjust the `targets` list).
