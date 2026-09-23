# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Deterministic stress-tree generator, mutator and verifier (stress suites).

Shared, importable-only helper for ``tests/test_bb_server_stress.py`` and
``tests/test_bb_p2p_stress.py``. The design is specified in
``docs/internal/stress-test-plan.md`` (§5 generator, §6 verifier, §7 metrics,
§8 mutation, §10 knobs); this module implements it without deviating.

Rules honoured here:

* **No tests and no ``assert`` statements** — every check returns data
  (``Mismatch`` / ``VerificationStats``) and the callers assert on it.
* **Determinism** — a single layout PRNG drives structure; every file content
  stream is derived from a per-path PRNG (``f"{seed}:{relpath}"``), mutation
  waves from ``f"{seed}:wave{w}"``, so a run is exactly reproducible from its
  base seed without holding all content in memory.
* **Forbidden names** — the generator refuses by construction (``ValueError``)
  to emit names colliding with the always-active default ignore patterns or
  the engine-owned names (plan §13.1).

Consumers: ``hsync-client`` stress suites only (not the engine, not other
projects).
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

CHUNK: Final[int] = 1 << 20

# Permission pools (never setuid/setgid/sticky — stripped on receipt).
# 0o200 was dropped from the original pool: the engine deliberately
# skips owner-unreadable files on upload (transfer.py logs "Permission denied
# reading" and moves on), so a write-only file can never reach the replica and
# would break the strict byte-for-byte contract by design, not by defect.
# Every mode below keeps owner-read (and dirs keep owner-rwx).
FILE_MODES: Final[tuple[int, ...]] = (0o600, 0o644, 0o700, 0o755, 0o666, 0o640)
DIR_MODES: Final[tuple[int, ...]] = (0o700, 0o750, 0o755)

# Plan §13.2: empty dirs are not replicated in server mode; v1 always
# generates the leaf invariant (every directory has a file somewhere in its
# subtree), so no `.hsynckeep` sentinels are needed.
ENGINE_FILENAMES: Final[frozenset[str]] = frozenset(
    {".hsyncfolder", ".hsyncignore", ".hsyncstate", ".hsynctombstones", ".hsynckeep"}
)
# Plan §6.2: partial-transfer leftovers + server-bucket `.backup` artifacts.
WHITELIST_SUFFIXES: Final[tuple[str, ...]] = (
    ".part",
    ".delta.part",
    ".delta.part.lit",
    ".backup",
)
# Subset of ``WHITELIST_SUFFIXES`` that marks a transfer **in flight**: the
# engine streams to ``<name>.part`` and renames on success, so the bytes of a
# multi-megabyte file are invisible to the replica signal (count and size) until
# the rename. A quiet poll that ignores them would declare convergence while a
# large file is still arriving (plan §13.10). ``.backup`` is terminal state, not
# activity, so it is deliberately not part of this set.
INFLIGHT_SUFFIXES: Final[tuple[str, ...]] = (".part", ".delta.part", ".delta.part.lit")

Manifest = dict[str, dict[str, object]]

# ---------------------------------------------------------------------------
# Forbidden-name machinery (plan §13.1)
# ---------------------------------------------------------------------------

_FORBIDDEN_EXACT: Final[frozenset[str]] = frozenset(
    {
        ".ds_store",
        ".spotlight-v100",
        ".trashes",
        "thumbs.db",
        "thumbs.db:encryptable",
        "ehthumbs.db",
        "ehthumbs_vista.db",
        "desktop.ini",
        "4913",
        *ENGINE_FILENAMES,
    }
)


def _is_forbidden_name(name: str) -> bool:
    low = name.lower()
    if low in _FORBIDDEN_EXACT or name.startswith("."):
        return True
    if name.startswith(("._", "~$", ".~lock", ".goutputstream-")):
        return True
    if name.endswith(("~", ".tmp", ".temp", ".bak", ".crdownload", *WHITELIST_SUFFIXES)):
        return True
    if re.search(r"\.sw[a-z]?$", name):
        return True
    # hsync partial-rename pattern `*-????-??-??-??????` (date-time suffix).
    return re.search(r"-\d{4}-\d{2}-\d{2}-\d{6}$", name) is not None


def _check_safe_name(name: str) -> None:
    if _is_forbidden_name(name):
        raise ValueError(f"generated name collides with an ignored/engine name: {name!r}")


# ---------------------------------------------------------------------------
# Deterministic name / size / content primitives
# ---------------------------------------------------------------------------

_BASE_WORDS: Final[tuple[str, ...]] = (
    "doc",
    "relazione",
    "piano",
    "archivio",
    "dati",
    "foto",
    "note",
    "report",
    "media",
    "export",
    "bozza",
    "condivisi",
    "appunti",
    "liste",
    "foto-ñ",
    "dati-č",
    "note-é",
    "media-ü",
)
_MID: Final[tuple[str, ...]] = (" ", "-", ".", "_")
_SECOND: Final[tuple[str, ...]] = ("finali", "vecchi", "nuovi", "lavoro", "casa", "ufficio")
_NAME_ALPHABET: Final[str] = "abcdefghijkmnopqrstuvwxyz23456789"


def _gen_name(rng: random.Random) -> str:
    """Random safe name: alnum, Unicode, spaces, dots, dashes, long forms."""
    base = rng.choice(_BASE_WORDS)
    style = rng.random()
    if style < 0.35:
        name = f"{base}{rng.randint(0, 999)}"
    elif style < 0.55:
        name = f"{base}{rng.choice(_MID)}{rng.choice(_SECOND)}"
    elif style < 0.70:
        name = f"{base}{rng.choice(_MID)}v{rng.randint(1, 99)}"
    elif style < 0.85:
        name = f"{base} {rng.choice(_SECOND)}"
    else:
        pad = "".join(rng.choice(_NAME_ALPHABET) for _ in range(rng.randint(40, 100)))
        name = f"{base}-{pad}"[:128]
    _check_safe_name(name)
    return name


def _unique_name(rng: random.Random, used: set[str]) -> str:
    name = _gen_name(rng)
    n = 1
    while name in used:
        name = f"{_gen_name(rng)}-{n}"
        n += 1
    used.add(name)
    return name


def _draw_size(rng: random.Random, max_file_size: int) -> int:
    """Mixed size distribution (plan §5): 10% empty, 25% ≤1KiB, 35% ≤64KiB,
    25% ≤256KiB, 5% outliers up to *max_file_size*."""
    r = rng.random()
    if r < 0.10:
        return 0
    if r < 0.35:
        return rng.randint(1, 1024)
    if r < 0.70:
        return rng.randint(1025, 64 * 1024)
    if r < 0.95:
        return rng.randint(64 * 1024 + 1, 256 * 1024)
    return rng.randint(256 * 1024 + 1, max(256 * 1024 + 1, max_file_size))


def _draw_mtime(rng: random.Random) -> int:
    return rng.randint(1_700_000_000, max(1_700_000_001, int(time.time())))


def _content_rng(seed: int, relpath: str) -> random.Random:
    return random.Random(f"{seed}:{relpath}")


def _write_content(path: Path, crng: random.Random, size: int) -> str:
    """Write deterministic content of *size* bytes; return the sha256 hexdigest.

    Two content families (plan §5): incompressible random bytes (stresses
    SHA-256 / zstd no-op) and compressible repetitive patterns (zeros /
    repeating blocks — stresses the zstd transfer path).

    Callers must use :func:`_deposit_file` rather than calling this directly on
    a path inside the synced tree: the metadata (mode/mtime) has to be final
    *before* the file becomes visible to the watcher, which is only possible on
    a staged copy (plan §13.9).
    """
    family_roll = crng.random()
    h = hashlib.sha256()
    written = 0
    repeating: bytes = b""
    with open(path, "wb") as fh:
        while written < size:
            n = min(CHUNK, size - written)
            if family_roll < 0.33:  # zeros
                block = b"\x00" * n
            elif family_roll < 0.66:  # repeating block
                if len(repeating) < 4096:
                    repeating = crng.randbytes(4096)
                block = (repeating * (n // len(repeating) + 1))[:n]
            else:  # incompressible
                block = crng.randbytes(n)
            fh.write(block)
            h.update(block)
            written += n
    return h.hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Atomic deposit (plan §13.9)
# ---------------------------------------------------------------------------

DEPOSIT_DIR_SUFFIX: Final[str] = ".stress-deposit"


def _deposit_dir(root: Path) -> Path:
    """Return the staging directory for atomic deposits on *root*'s filesystem.

    It is a **sibling** of the synced root, never inside it (the watcher/peer
    under test must not observe the staging file) and on the same filesystem,
    which is what keeps ``os.replace`` an atomic rename rather than a copy.
    """
    staging = Path(str(root) + DEPOSIT_DIR_SUFFIX)
    staging.mkdir(parents=True, exist_ok=True)
    return staging


def _deposit_file(
    root: Path,
    relpath: str,
    crng: random.Random,
    *,
    size: int,
    mode: int,
    mtime: int,
) -> str:
    """Atomically materialize ``root/relpath`` with its final metadata.

    The bytes are written to a staging file (see :func:`_deposit_dir`), the
    final ``chmod``/``utime`` are applied *there*, and only then is the file
    renamed into the synced tree. The tree therefore emits a single event whose
    content and metadata are already final.

    Why (plan §13.9): the engine propagates the metadata it observes **at event
    time** and deliberately suppresses a later change that keeps the same
    content hash (so a plain ``touch`` is never broadcast as a transfer).
    Writing the bytes first and back-dating the mtime afterwards therefore
    races the watcher and can leave a replica holding the arrival time instead
    of the source mtime — a harness artifact, not an engine defect. Depositing
    atomically removes the race while staying a perfectly ordinary way for a
    real application to publish a file.
    """
    dest = root / relpath
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Deterministic, collision-free staging name (relpaths are unique).
    token = hashlib.sha1(relpath.encode("utf-8")).hexdigest()
    staging = _deposit_dir(root) / f"{len(relpath)}-{token}"
    sha = _write_content(staging, crng, size)
    os.chmod(staging, mode)
    os.utime(staging, (mtime, mtime))
    os.replace(staging, dest)
    return sha


def _is_inflight(name: str) -> bool:
    """Return whether *name* is an in-flight partial-transfer artifact.

    In-flight artifacts are excluded from the replica signal's count/bytes (they
    are not manifest entries) but are reported separately so the quiet poll can
    tell "converged" apart from "a big file is still streaming" (plan §13.10).
    """
    return name.endswith(INFLIGHT_SUFFIXES)


def _replica_state(root: Path) -> tuple[int, int, int]:
    """Cheap replica signal ``(file count, total bytes, in-flight artifacts)``.

    No hashing (plan §7). Engine artifacts whitelisted in verification
    (``.hsynckeep`` sentinels, ``.hsyncfolder`` marker, partial/backup suffixes)
    are excluded from the signal: in P2P mode they live *inside* the synced tree
    and would otherwise make the replica signal permanently overshoot the
    manifest totals, so convergence could never be detected. In-flight
    ``*.part*`` artifacts are counted separately instead of being dropped: they
    are the only on-disk evidence that a transfer is still running.
    """
    count = 0
    total = 0
    inflight = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if _is_inflight(name):
                inflight += 1
                continue
            if _is_whitelisted(name):
                continue
            try:
                total += (Path(dirpath) / name).stat().st_size
            except OSError:
                pass
            count += 1
    return count, total, inflight


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Mismatch:
    """One verification discrepancy (plan §6.8)."""

    path: str
    field: str
    expected: str | None
    actual: str | None


@dataclass
class VerificationStats:
    """Counters for one verification pass (plan §6.2)."""

    objects: int = 0
    bytes: int = 0
    whitelisted: int = 0
    checked: int = 0


def manifest_totals(manifest: Manifest) -> tuple[int, int, int]:
    """Return ``(n_files, n_dirs, total_bytes)`` of a manifest."""
    files = [m for m in manifest.values() if m["type"] == "file"]
    dirs = [m for m in manifest.values() if m["type"] == "dir"]
    return len(files), len(dirs), sum(int(m["size"]) for m in files)


def manifest_path(root: Path) -> Path:
    """Sidecar JSON path for *root* — a sibling, never inside the synced tree."""
    return Path(str(root) + ".stress.manifest.json")


# ---------------------------------------------------------------------------
# Generator (plan §5)
# ---------------------------------------------------------------------------


def generate(
    root: Path,
    *,
    seed: int,
    n_files: int,
    n_dirs: int,
    max_depth: int = 5,
    max_file_size: int = 8 << 20,
    allow_empty_dirs: bool = False,
) -> Manifest:
    """Generate a deterministic heterogeneous tree under *root* and return the
    manifest ``{relpath: {type, size, sha256, mode, mtime}}``.

    Layout: directories at depths ``0..max_depth - 1`` (path depth =
    ``len(parts) - 1``), files at depths ``0..max_depth`` including the tree
    root. Leaf invariant (v1, ``allow_empty_dirs=False``): every emitted
    directory contains at least one file somewhere in its subtree — this is
    what makes the server-mode strict dir assertion sound (plan §13.2).
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    manifest: Manifest = {}
    used: set[str] = set()

    dirs: list[dict[str, object]] = []  # {"relpath", "depth", "mode", "mtime"}
    for _ in range(n_dirs):
        parents = [d for d in dirs if int(d["depth"]) <= max_depth - 2]
        parent_rel: str | None = None
        if dirs and rng.random() >= 0.35:
            if parents and rng.random() < 0.65:
                parent_rel = str(rng.choice(parents)["relpath"])
        name = _unique_name(rng, used)
        relpath = name if parent_rel is None else f"{parent_rel}/{name}"
        depth = 0 if parent_rel is None else int(parent_rel.count("/")) + 1
        _check_safe_name(relpath)
        dirs.append(
            {
                "relpath": relpath,
                "depth": depth,
                "mode": rng.choice(DIR_MODES),
                "mtime": _draw_mtime(rng),
            }
        )

    files: list[dict[str, object]] = []
    for _ in range(n_files):
        # Root-level files allowed (plan §5); otherwise any emitted directory
        # (all have depth <= max_depth - 1, so file depth <= max_depth).
        parent_rel = None if not dirs or rng.random() < 0.10 else str(rng.choice(dirs)["relpath"])
        name = _unique_name(rng, used)
        relpath = name if parent_rel is None else f"{parent_rel}/{name}"
        _check_safe_name(relpath)
        files.append(
            {
                "relpath": relpath,
                "size": _draw_size(rng, max_file_size),
                "mode": rng.choice(FILE_MODES),
                "mtime": _draw_mtime(rng),
            }
        )

    dirs = _enforce_leaf_invariant(dirs, files, allow_empty_dirs)

    # Materialize: directory metadata is applied immediately after its mkdir
    # (before any child exists) and every file is deposited atomically, so each
    # watched event already carries its final metadata (plan §13.9).
    for d in dirs:
        p = root / str(d["relpath"])
        p.mkdir(parents=True, exist_ok=True)
        os.chmod(p, int(d["mode"]))
        os.utime(p, (int(d["mtime"]), int(d["mtime"])))
        manifest[str(d["relpath"])] = {
            "type": "dir",
            "size": 0,
            "sha256": None,
            "mode": int(d["mode"]),
            "mtime": int(d["mtime"]),
        }
    for f in files:
        relpath = str(f["relpath"])
        sha = _deposit_file(
            root,
            relpath,
            _content_rng(seed, relpath),
            size=int(f["size"]),
            mode=int(f["mode"]),
            mtime=int(f["mtime"]),
        )
        manifest[relpath] = {
            "type": "file",
            "size": int(f["size"]),
            "sha256": sha,
            "mode": int(f["mode"]),
            "mtime": int(f["mtime"]),
        }

    manifest_path(root).write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


def _enforce_leaf_invariant(
    dirs: list[dict[str, object]],
    files: list[dict[str, object]],
    allow_empty_dirs: bool,
) -> list[dict[str, object]]:
    """Drop directories whose subtree contains no file (plan §5 leaf invariant).

    Structural guarantee of the generator: if a kept directory's ancestors
    were dropped the invariant would be violated — ancestors always contain
    the kept directory's subtree files, so this cannot happen; the check
    below raises instead of asserting (this module carries no asserts).
    """
    if allow_empty_dirs:
        return dirs
    direct: dict[str, int] = {}
    for f in files:
        rel = str(f["relpath"])
        parent = rel.rsplit("/", 1)[0] if "/" in rel else None
        if parent is not None:
            direct[parent] = direct.get(parent, 0) + 1
    children: dict[str, list[str]] = {}
    for d in dirs:
        rel = str(d["relpath"])
        parent = rel.rsplit("/", 1)[0] if "/" in rel else None
        if parent is not None:
            children.setdefault(parent, []).append(rel)
    kept: set[str] = set()
    for d in sorted(dirs, key=lambda d: int(d["depth"]), reverse=True):
        rel = str(d["relpath"])
        if direct.get(rel, 0) > 0 or any(c in kept for c in children.get(rel, [])):
            kept.add(rel)
    dropped = {str(d["relpath"]) for d in dirs} - kept
    leaked = direct.keys() & dropped
    if leaked:
        raise ValueError(f"leaf invariant violated by {sorted(leaked)!r}")
    return [d for d in dirs if str(d["relpath"]) in kept]


# ---------------------------------------------------------------------------
# Mutation (plan §8)
# ---------------------------------------------------------------------------


def apply_mutation(
    root: Path,
    manifest: Manifest,
    *,
    seed: int,
    wave: int,
    fraction: float = 0.2,
    max_file_size: int = 8 << 20,
) -> tuple[Manifest, list[tuple[str, str]]]:
    """Apply one deterministic change wave on an existing tree.

    Returns the updated manifest and the primitives applied per path
    (``create | modify | delete | chmod``). Wave PRNG:
    ``random.Random(f"{seed}:wave{wave}")`` — independent of the generation
    stream, so the post-mutation manifest is exactly reproducible. ``chmod``
    targets files only (plan §8); deletes never remove the last file of a
    directory subtree, preserving the leaf invariant without pruning.
    """
    root = Path(root)
    wrng = random.Random(f"{seed}:wave{wave}")
    files = sorted(p for p, m in manifest.items() if m["type"] == "file")
    k = max(1, round(len(files) * fraction)) if files else 0
    n_mod = round(k * 0.4)
    n_create = round(k * 0.25)
    n_del = round(k * 0.25)
    n_chmod = max(0, k - n_mod - n_create - n_del)

    # Deletable files: deleting must not empty a directory subtree. Track the
    # remaining per-directory subtree counts so multiple deletions in the same
    # wave can never strip a directory bare (leaf invariant preserved).
    subtree: dict[str, int] = {}
    for f in files:
        head = f.rsplit("/", 1)[0] if "/" in f else None
        while head is not None:
            subtree[head] = subtree.get(head, 0) + 1
            head = head.rsplit("/", 1)[0] if "/" in head else None

    primitives: list[tuple[str, str]] = []
    pool = list(files)
    wrng.shuffle(pool)
    mod_set = set(pool[:n_mod])
    del_set: set[str] = set()
    remaining = dict(subtree)
    for f in pool[n_mod:]:
        if len(del_set) >= n_del:
            break
        if f in mod_set:
            continue
        chain: list[str] = []
        head = f.rsplit("/", 1)[0] if "/" in f else None
        while head is not None:
            chain.append(head)
            head = head.rsplit("/", 1)[0] if "/" in head else None
        if any(remaining.get(c, 0) - 1 < 1 for c in chain):
            continue  # deleting f would empty some directory subtree
        del_set.add(f)
        for c in chain:
            remaining[c] -= 1
    chmod_set = [f for f in pool if f not in mod_set and f not in del_set][:n_chmod]

    for f in sorted(mod_set):
        new_rng = random.Random(f"{seed}:wave{wave}:mod:{f}")
        size = _draw_size(new_rng, max_file_size)
        mtime = _draw_mtime(new_rng)
        mode = int(manifest[f]["mode"])
        # Atomic re-deposit (plan §13.9): one event, final content + metadata.
        # The new content may coincide with the previous one (e.g. an empty file
        # redrawn at size 0): that is a metadata-only change and the engine must
        # still propagate the new mtime (plan §13.11).
        sha = _deposit_file(root, f, new_rng, size=size, mode=mode, mtime=mtime)
        manifest[f] = {
            "type": "file",
            "size": size,
            "sha256": sha,
            "mode": mode,
            "mtime": mtime,
        }
        primitives.append((f, "modify"))

    for f in sorted(del_set):
        (root / f).unlink()
        del manifest[f]
        primitives.append((f, "delete"))

    for f in chmod_set:
        old = int(manifest[f]["mode"])
        new = wrng.choice([m for m in FILE_MODES if m != old])
        os.chmod(root / f, new)
        manifest[f]["mode"] = new
        primitives.append((f, "chmod"))

    dir_relpaths = sorted(p for p, m in manifest.items() if m["type"] == "dir")
    for i in range(n_create):
        parent: str | None = None
        if dir_relpaths and wrng.random() >= 0.15:
            parent = str(wrng.choice(dir_relpaths))
        # Seed the name PRNG from the wave stream so it stays deterministic
        # while independent of the generation stream. Uniqueness is relative to
        # *this* parent: a new basename must not collide with an existing file
        # OR directory entry under the same parent (checking a bare basename
        # against full relpaths misses the latter, and the atomic replace would
        # then fail with IsADirectoryError).
        name_rng = random.Random(f"{seed}:wave{wave}:new:{i}:{wrng.getrandbits(64)}")
        parent_prefix = parent or ""
        siblings = {
            rel.rsplit("/", 1)[-1] for rel in manifest if rel.rpartition("/")[0] == parent_prefix
        }
        name = _unique_name(name_rng, siblings)
        relpath = name if parent is None else f"{parent}/{name}"
        _check_safe_name(relpath)
        size = _draw_size(name_rng, max_file_size)
        mtime = _draw_mtime(name_rng)
        mode = wrng.choice(FILE_MODES)
        # Atomic deposit (plan §13.9) — same reason as the generation path.
        sha = _deposit_file(root, relpath, name_rng, size=size, mode=mode, mtime=mtime)
        manifest[relpath] = {
            "type": "file",
            "size": size,
            "sha256": sha,
            "mode": mode,
            "mtime": mtime,
        }
        primitives.append((relpath, "create"))

    manifest_path(root).write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest, primitives


# ---------------------------------------------------------------------------
# Verifier (plan §6)
# ---------------------------------------------------------------------------


def _is_whitelisted(name: str) -> bool:
    return name in ENGINE_FILENAMES or name.endswith(WHITELIST_SUFFIXES)


def verify_tree(
    root: Path,
    manifest: Manifest,
    *,
    mode: str,
    mtime_tolerance: float = 2.0,
) -> tuple[list[Mismatch], VerificationStats]:
    """Compare a replica root against a manifest.

    ``mode`` is ``"server"`` or ``"p2p"``: directory modes are asserted
    floor-only (``>= 0o700``) in server mode (plan §13.3) and strict equality
    with ``(source & 0o777) | 0o700`` in P2P mode; file modes, sizes and
    streaming SHA-256 digests are strict in both; mtimes are tolerance-checked.
    Never asserts; returns the sorted mismatch list plus stats. Whitelisted
    engine artifacts found on disk are *counted*, never silently dropped
    (plan §6.2).
    """
    root = Path(root)
    mismatches: list[Mismatch] = []
    stats = VerificationStats()
    seen: set[str] = set()

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(dirnames)
        rel_base = os.path.relpath(dirpath, root)
        for name in sorted(filenames) + dirnames:
            rel = name if rel_base == "." else f"{rel_base}/{name}"
            entry = manifest.get(rel)
            full = Path(dirpath) / name
            if entry is None:
                if _is_whitelisted(name):
                    stats.whitelisted += 1
                else:
                    mismatches.append(Mismatch(rel, "extra", None, "present"))
                continue
            seen.add(rel)
            stats.checked += 1
            if entry["type"] == "dir":
                if not full.is_dir():
                    mismatches.append(Mismatch(rel, "type", "dir", "file"))
                    continue
                st = full.stat()
                stats.objects += 1
                actual = st.st_mode & 0o777
                expected = int(entry["mode"]) & 0o777
                if mode == "server":
                    ok = (st.st_mode & 0o700) == 0o700
                    exp_repr = "floor 0o700"
                    act_repr = oct(st.st_mode & 0o777)
                else:
                    ok = actual == (expected | 0o700)
                    exp_repr = oct(expected | 0o700)
                    act_repr = oct(actual)
                if not ok:
                    mismatches.append(Mismatch(rel, "dir_mode", exp_repr, act_repr))
                continue
            if full.is_dir():
                mismatches.append(Mismatch(rel, "type", "file", "dir"))
                continue
            st = full.stat()
            stats.objects += 1
            stats.bytes += st.st_size
            if st.st_size != int(entry["size"]):
                mismatches.append(Mismatch(rel, "size", str(entry["size"]), str(st.st_size)))
            digest = _sha256_file(full)
            if digest != entry["sha256"]:
                mismatches.append(Mismatch(rel, "sha256", str(entry["sha256"]), digest))
            actual_mode = st.st_mode & 0o777
            if actual_mode != int(entry["mode"]) & 0o777:
                mismatches.append(
                    Mismatch(rel, "mode", oct(int(entry["mode"]) & 0o777), oct(actual_mode))
                )
            if abs(st.st_mtime - float(entry["mtime"])) > mtime_tolerance:
                mismatches.append(Mismatch(rel, "mtime", str(entry["mtime"]), f"{st.st_mtime:.0f}"))

    for rel, entry in sorted(manifest.items()):
        if rel not in seen:
            mismatches.append(Mismatch(rel, "missing", str(entry["type"]), None))

    mismatches.sort(key=lambda m: (m.path, m.field))
    return mismatches, stats


# ---------------------------------------------------------------------------
# P2P convergence measurement (plan §7)
# ---------------------------------------------------------------------------


def converge_quiet_poll(
    replica: Path,
    manifest: Manifest,
    *,
    mode: str,
    timeout: float,
    poll_interval: float = 0.5,
    mtime_tolerance: float = 2.0,
) -> tuple[list[Mismatch], VerificationStats, float, float]:
    """Measure P2P convergence: quiet-poll the replica (cheap count+bytes
    signal, no hashing) until unchanged for two consecutive polls **with no
    in-flight transfer artifact on disk**, then run the single full
    ``verify_tree`` (plan §7). On mismatch, resume polling until *timeout*.
    Returns ``(mismatches, stats, t_sync, t_verify)``; ``t_sync`` stops at quiet
    detection (resolution = the poll interval) and the full verify is accounted
    in ``t_verify``.

    Requiring ``inflight == 0`` matters for correctness of the *measurement*: a
    multi-megabyte outlier streams into ``<name>.part``, whose bytes are
    whitelisted out of the count/size signal, so a signal-only quiet test would
    fire mid-transfer and the full verify would see the file as missing — the
    timeout then reports a false divergence (plan §13.10).

    On timeout the returned list starts with a ``<replica>: timeout`` marker
    **followed by the mismatches of the last full verify**, so a real
    divergence is never hidden behind the generic timeout entry (that is what
    report readability and triage depend on).
    """
    replica = Path(replica)
    start = time.perf_counter()
    last: tuple[int, int] | None = None
    last_mismatches: list[Mismatch] = []
    last_stats = VerificationStats()
    while True:
        elapsed = time.perf_counter() - start
        if elapsed >= timeout:
            marker = Mismatch("<replica>", "timeout", "converged", f"{elapsed:.1f}s")
            return [marker, *last_mismatches], last_stats, elapsed, 0.0
        count, total, inflight = _replica_state(replica)
        sig = (count, total)
        if sig == last and inflight == 0:
            t0 = time.perf_counter()
            mismatches, stats = verify_tree(
                replica, manifest, mode=mode, mtime_tolerance=mtime_tolerance
            )
            t_verify = time.perf_counter() - t0
            if not mismatches:
                return mismatches, stats, time.perf_counter() - start, t_verify
            last_mismatches, last_stats = mismatches, stats
            last = None  # force another cheap poll before re-verifying
        else:
            last = sig
        time.sleep(poll_interval)


# ---------------------------------------------------------------------------
# Knobs and reporting (plan §7, §10)
# ---------------------------------------------------------------------------


def stress_knobs(mode: str) -> dict[str, object]:
    """``HSYNC_BB_STRESS_*`` environment knobs with the documented defaults
    (plan §10). *mode* is ``"server"`` or ``"p2p"`` (different default scale)."""
    env = os.environ
    return {
        "seed": int(env.get("HSYNC_BB_STRESS_SEED", "20260828")),
        "n_files": int(env.get("HSYNC_BB_STRESS_FILES", "2000" if mode == "server" else "1500")),
        "n_dirs": int(env.get("HSYNC_BB_STRESS_DIRS", "300" if mode == "server" else "200")),
        "max_depth": int(env.get("HSYNC_BB_STRESS_MAXDEPTH", "5")),
        "max_file_size": int(env.get("HSYNC_BB_STRESS_MAXFILESIZE", str(8 << 20))),
        "iterations": int(env.get("HSYNC_BB_STRESS_ITERATIONS", "3")),
        "waves": int(env.get("HSYNC_BB_STRESS_WAVES", "1")),
        "mutation_fraction": float(env.get("HSYNC_BB_STRESS_MUTATION_FRACTION", "0.2")),
        "concurrency": [
            int(x) for x in env.get("HSYNC_BB_STRESS_CONCURRENCY", "1,4,8").split(",") if x.strip()
        ],
        "timeout": float(env.get("HSYNC_BB_STRESS_TIMEOUT", "300")),
        "poll_interval": float(env.get("HSYNC_BB_STRESS_POLL_INTERVAL", "0.5")),
        "mtime_tolerance": float(env.get("HSYNC_BB_STRESS_MTIME_TOLERANCE", "2")),
        "report": env.get("HSYNC_BB_STRESS_REPORT") or None,
        "subprocess": env.get("HSYNC_BB_STRESS_SUBPROCESS") == "1",
    }


def make_record(
    *,
    mode: str,
    seed: int,
    n_files: int,
    n_dirs: int,
    total_bytes: int,
    t_generate_s: float,
    t_sync_s: float,
    t_verify_s: float,
    max_concurrent_transfers: int,
    iteration: int,
    phase: str,
    mismatches: list[Mismatch] | None = None,
) -> dict[str, object]:
    """Build a per-run report record (plan §7 schema) with derived metrics."""
    files_per_s = n_files / t_sync_s if t_sync_s > 0 else 0.0
    mib_per_s = (total_bytes / (1 << 20)) / t_sync_s if t_sync_s > 0 else 0.0
    return {
        "mode": mode,
        "seed": seed,
        "n_files": n_files,
        "n_dirs": n_dirs,
        "total_bytes": total_bytes,
        "t_generate_s": round(t_generate_s, 3),
        "t_sync_s": round(t_sync_s, 3),
        "t_verify_s": round(t_verify_s, 3),
        "files_per_s": round(files_per_s, 1),
        "mib_per_s": round(mib_per_s, 2),
        "max_concurrent_transfers": max_concurrent_transfers,
        "iteration": iteration,
        "phase": phase,
        "n_mismatches": len(mismatches or []),
        "mismatches": [f"{m.path}:{m.field}" for m in (mismatches or [])[:10]],
    }


def write_report(path: str | None, record: dict[str, object]) -> None:
    """Append one JSON line per run to the optional ``HSYNC_BB_STRESS_REPORT``
    file (JSONL: one record per run). No-op when *path* is unset."""
    if path is None:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def print_report(record: dict[str, object]) -> None:
    """Print one human-readable summary line for a run record."""
    print(
        "[STRESS] mode={mode} iter={iteration} seed={seed} "
        "files={n_files} dirs={n_dirs} bytes={total_bytes} "
        "t_gen={t_generate_s:.2f}s t_sync={t_sync_s:.2f}s t_verify={t_verify_s:.2f}s "
        "files/s={files_per_s} MiB/s={mib_per_s} "
        "conc={max_concurrent_transfers} phase={phase} mismatches={n_mismatches}".format(**record)
    )


# ---------------------------------------------------------------------------
# Cross-iteration aggregation (plan §7)
# ---------------------------------------------------------------------------

# Records appended by the stress suites' ``_report`` helper during a run. The
# terminal-summary hook in ``tests/conftest.py`` aggregates them after the
# session (test modules cannot host pytest hooks). Empty on non-stress runs.
RECORDS: list[dict[str, object]] = []

# Per-run record metrics aggregated across the parametrized iterations.
_AGG_METRICS: Final[tuple[str, ...]] = (
    "t_generate_s",
    "t_sync_s",
    "t_verify_s",
    "files_per_s",
    "mib_per_s",
)


def collect(record: dict[str, object]) -> None:
    """Append one per-run report record for end-of-session aggregation.

    Called by the stress suites right before :func:`print_report` /
    :func:`write_report`; the aggregated median/min/max table is emitted by
    ``tests/conftest.py::pytest_terminal_summary`` (plan §7).
    """
    RECORDS.append(record)


def aggregate(records: list[dict[str, object]]) -> list[dict[str, object]]:
    """Group per-run records and compute median/min/max per metric (plan §7).

    Groups by ``(mode, phase, max_concurrent_transfers)`` so the concurrency
    sweep and the burst/mutation phases never mix, then reports ``median``,
    ``min`` and ``max`` for every metric in :data:`_AGG_METRICS`. Pure (no I/O,
    no printing); callers render the rows with :func:`format_aggregate`.
    """
    groups: dict[tuple[str, str, int], list[dict[str, object]]] = {}
    for record in records:
        key = (
            str(record.get("mode")),
            str(record.get("phase")),
            int(record.get("max_concurrent_transfers", 0)),
        )
        groups.setdefault(key, []).append(record)
    rows: list[dict[str, object]] = []
    for (mode, phase, max_transfers), group in sorted(groups.items()):
        row: dict[str, object] = {
            "mode": mode,
            "phase": phase,
            "max_concurrent_transfers": max_transfers,
            "runs": len(group),
        }
        for metric in _AGG_METRICS:
            values = [float(r[metric]) for r in group if metric in r]
            if values:
                row[f"{metric}_median"] = round(statistics.median(values), 3)
                row[f"{metric}_min"] = round(min(values), 3)
                row[f"{metric}_max"] = round(max(values), 3)
        rows.append(row)
    return rows


def _metric_cell(row: dict[str, object], metric: str) -> str:
    """Format one ``median [min..max]`` aggregate cell."""
    return f"{row.get(f'{metric}_median')} [{row.get(f'{metric}_min')}..{row.get(f'{metric}_max')}]"


def format_aggregate(rows: list[dict[str, object]]) -> list[str]:
    """Render aggregate rows as human-readable lines (plan §7)."""
    lines: list[str] = []
    if rows:
        lines.append("[STRESS] aggregate (median [min..max]) per phase:")
    for row in rows:
        lines.append(
            f"  mode={row['mode']} phase={row['phase']} "
            f"conc={row['max_concurrent_transfers']} runs={row['runs']} "
            f"t_gen={_metric_cell(row, 't_generate_s')}s "
            f"t_sync={_metric_cell(row, 't_sync_s')}s "
            f"t_verify={_metric_cell(row, 't_verify_s')}s "
            f"files/s={_metric_cell(row, 'files_per_s')} "
            f"MiB/s={_metric_cell(row, 'mib_per_s')}"
        )
    return lines


def fail_with(mismatches: list[Mismatch], limit: int = 20) -> None:
    """Raise the caller's assertion error for a mismatch list (callers wrap
    this: ``if mism: stress_tree.fail_with(mism)`` inside ``pytest.raises``-
    free tests — it always raises ``AssertionError``)."""
    lines = [
        f"  {m.path}: {m.field} expected={m.expected!r} actual={m.actual!r}"
        for m in mismatches[:limit]
    ]
    more = "" if len(mismatches) <= limit else f"\n  ... and {len(mismatches) - limit} more"
    raise AssertionError(f"{len(mismatches)} verification mismatches:\n" + "\n".join(lines) + more)
