# Conflict Resolution & Causal Vector-Clock Merge

HSYNC uses a **decentralized causal model** (ADR-0022): every node records, per
file, the content hash it last agreed on with the cluster (**base**) plus a
**vector clock** (vclock) summarising the causal history of that version.
Comparing the local clock, the remote clock and the common ancestor lets a peer
tell whether only one side moved on (dominance) or both did (true concurrency)
— without trusting wall-clock timestamps. This is immune to clock skew and to
the false conflicts that arise when files relay through intermediate nodes.

On a true divergence (concurrent clocks) a **deterministic, content-only
tie-break** picks the winner (highest SHA-256 lexicographically) and the losing
content is preserved as a `_conflicted_copy_` (extrema ratio). **No node is
authoritative**: the server and the elected LAN master are transport hubs only;
every node applies the same algorithm.

## Table of Contents

- [How it works](#how-it-works)
- [The version (`FileVersion`)](#the-version-fileversion)
- [The decision (`resolve_three_way`)](#the-decision-resolve_three_way)
- [The store (`SyncStateStore`)](#the-store-syncstatestore)
- [Lifecycle in a sync cycle](#lifecycle-in-a-sync-cycle)
- [Conflict handling](#conflict-handling)
- [Boundary cases](#boundary-cases)

---

## How it works

```
        ┌─────────────┐
        │  Common     │
        │  Ancestor   │  ← "base" (hash + vclock recorded after last sync)
        │  hash=A     │
        └──────┬──────┘
               │
       ┌───────┴───────┐
       ▼               ▼
┌─────────────┐  ┌─────────────┐
│  Local      │  │  Remote     │
│  hash=A     │  │  hash=B     │  ← remote clock dominates → download
│  vclock M:1 │  │  vclock M:2 │
└─────────────┘  └─────────────┘

       OR

┌─────────────┐  ┌─────────────┐
│  Local      │  │  Remote     │
│  hash=X     │  │  hash=Y     │  ← concurrent clocks → tie-break by hash
│  vclock M:1 │  │  vclock N:1 │
└─────────────┘  └─────────────┘
```

On every sync cycle, for each file that appears on both sides:

1. Content identity: if `local_hash == remote_hash` → `in_sync` (nothing to do).
2. If a **base** is recorded and `local_hash == base_hash` → the remote moved
   on → **download**.
3. If a **base** is recorded and `remote_hash == base_hash` → the local moved
   on → **upload**.
4. Otherwise compare the **vector clocks**:
   - local dominates → **upload**
   - remote dominates → **download**
   - concurrent (neither dominates) → **true conflict**: deterministic
     SHA-256 tie-break picks the winner, the loser is preserved.

The wall clock is never consulted. This algorithm is implemented in
`hsync_common.models.version.resolve_three_way()` and consumed by the client's
`get_sync_actions()` and the upload arbitration on both server and LAN master.

---

## The version (`FileVersion`)

Each recorded version entry contains five fields:

| Field | Type | Purpose |
|-------|------|---------|
| `base_hash` | `str` | SHA-256 of the file content at the last successful sync |
| `version_id` | `str` | Opaque, random 128-bit id identifying this version lineage |
| `origin_cid` | `str \| None` | Client ID that authored the version (`None` if unknown) |
| `ts` | `int` | Wall-clock timestamp when the base was recorded (informational only) |
| `vclock` | `dict[str, int]` | Causal history: node cid → logical counter |

The `vclock` is the causal history: each node increments its own counter once
per observed local edit (`tick_vclock`), producing monotonically increasing
clocks so two distinct versions can never alias the same clock (the
anti-aliasing invariant). Comparing two clocks componentwise (`compare_vclocks`)
yields `equal`, `dominates`, `dominated`, or `concurrent`.

On the wire (inside `FileDict["version"]`), the form is `VersionInfo`: the
same `vclock`/`version_id`/`origin_cid`/`ts` but **without** `base_hash` — the
content hash of the version is the `sha256sum` of the enclosing `FileDict`.

---

## The decision (`resolve_three_way`)

```python
def resolve_three_way(
    local_hash: str | None,
    local_vclock: dict[str, int] | None,
    remote_hash: str | None,
    remote_vclock: dict[str, int] | None,
    base_hash: str | None,
) -> SyncDecision:
```

| Condition | Decision | Action |
|-----------|----------|--------|
| `local == remote` | `in_sync` | Nothing to do |
| `local == base` | `download` | Pull remote version |
| `remote == base` | `upload` | Push local version |
| local clock dominates | `upload` | Push local version |
| remote clock dominates | `download` | Pull remote version |
| concurrent, local hash > remote hash | `conflict_upload` | Upload (receiver preserves its loser) |
| concurrent, remote hash ≥ local hash | `conflict_download` | Preserve local + download |

---

## The store (`SyncStateStore`)

The store is an in-memory `dict[relpath, FileVersion]` persisted to a
**`.hsyncstate`** side-car file at the bucket root. Properties:

- **Private to each peer** — never exchanged with the server or other peers
  (the wire form `VersionInfo` carries the clock inside the listing/event).
- **Atomic writes** — written to a `.tmp` file first, then renamed.
- **Garbage collected** — entries for files no longer in the bucket are pruned.
- **Thread-safe** — wrapped by a re-entrant lock in `hsync_client.sync_state`
  because transfers record their base from the parallel transfer thread pool.

The recorded clock after a sync is `merge(local_effective, remote_advertised)`
(`merge_and_record`): in dominance it equals the dominant clock, in conflict it
includes both histories so third nodes recognise either side as an ancestor
(the convergence invariant D11).

---

## Lifecycle in a sync cycle

```mermaid
sequenceDiagram
    participant C as Client
    participant SS as SyncStateStore
    participant S as Server/Master
    participant FS as Local FS

    C->>SS: ① load() from .hsyncstate
    C->>S: ② GET remote file list (with version/vclock)
    C->>FS: ③ Scan local files

    loop For each file pair
        C->>SS: ④ local_version_for_compare (pure, may tick)
        C->>C: ⑤ resolve_three_way(local, remote, base)
        alt upload
            C->>S: ⑥ POST file (+vclock/version_id)
            C->>SS: ⑦ record merged base
        else download
            C->>S: ⑥ GET file
            C->>SS: ⑦ record merged base (remote vclock)
        else conflict_download
            C->>FS: ⑥ Copy local to _conflicted_copy_
            C->>S: ⑦ Download remote version
            C->>SS: ⑦ record merged base
            C->>GUI: ⑦ Emit conflict_resolved event
        end
    end

    C->>SS: ⑧ retain(files still in bucket)
    C->>SS: ⑨ flush() → .hsyncstate
```

A dry-run/preview (`get_sync_actions(persist_state=False)`) never mutates the
store: `merge_and_record` is skipped so `.hsyncstate` stays byte-identical,
while the causal decisions are still computed accurately via the pure
`local_version_for_compare`.

---

## Conflict handling

When `resolve_three_way` returns `conflict_download` (the remote won the
deterministic hash tie-break):

1. The **local file is copied** to `<stem>_conflicted_copy_<ISO8601-UTC>_<cid>.<ext>`
   — preserving the user's local changes.
2. The **remote version is downloaded**.
3. The **base is updated** with a merged clock, so subsequent syncs don't
   re-trigger the conflict.
4. A **`conflict_resolved` UI event** is emitted so the Desktop GUI can notify.

When `resolve_three_way` returns `conflict_upload` (the local won the
tie-break), the local version is uploaded and the **receiver** preserves its
losing copy — no conflict copy is made on the client side.

### Naming convention

```
<stem>_conflicted_copy_<YYYY-MM-DDTHHMMSSZ>_<cid>.<ext>
```

- Timestamps are always **UTC**.
- `<cid>` is the client ID that authored the losing version.
- The same format is used whether the conflict is detected client-side
  (`_save_conflict_copy` in `sync.py`) or receiver-side (`arbitration.py`).

### Deterministic tie-break (no auto-merge)

HSYNC uses a **deterministic content-only tie-break with preservation**. When
both sides diverge concurrently, the higher SHA-256 hash wins and the loser is
preserved as a conflicted copy. This is the same "keep both" approach as
Dropbox/OneDrive.

**HSYNC does not perform automatic content merging.** Rationale:

- Most synced files are binary (images, archives, databases) where text-merge
  is meaningless.
- A three-way text merge can silently produce semantically broken files.
- Deterministic tie-break + conflicted copy is predictable: every node
  converges on the same winner without coordination, and the user can merge
  manually with their tool of choice.

---

## Boundary cases

| Scenario | Behavior |
|----------|----------|
| **File deleted locally, changed remotely** | Tombstone logic (ADR-0003, causal) handles the absent-file branch separately. |
| **File deleted remotely, changed locally** | Tombstone logic; local edit is uploaded if direction allows. |
| **New file on both sides with same content** | `local == remote` → `in_sync` |
| **New file on both sides, different content** | Both clocks empty → concurrent → deterministic hash tie-break. |
| **File replaced with empty content** | Hash `""` is treated as absent by `record_base()` — a no-op guard prevents corrupting the store. |
| **Upload superseded** | The server/master arbitrates: a causally-dominated upload is rejected (`409 superseded`) and the client downloads the current remote version on the next cycle. |
| **Upload loses tie-break** | The incoming bytes are preserved as a conflicted copy (`409 conflict_lost`); the stored winner is kept. |

## See also

- [Sync Flow](sync-flow.md) — client-side sync flow with conflict handling diagrams.
- [Architecture](internal/architecture.md) — high-level system overview.
- [Developer Guide](internal/developer.md) — performance optimizations and internals.
- [HSYNC Common → Version models](https://github.com/albertobasso/hsync-common/blob/main/docs/internal/architecture.md) — shared version store.
