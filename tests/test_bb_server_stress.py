# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Stress tests — server-mode load/replication harness (black-box).

Drives the real client through its public CLI (in-process ``run_sync_once``)
against a real ``hsync-server`` subprocess, and verifies the replica
byte-for-byte. Verification hierarchy (plan §6): the **primary** verdict is
wire-level — REST listings (``/files`` metadata, ``/dirs`` structure) plus a
per-file download re-hash; the on-disk bucket sweep (``verify_tree``) is a
**secondary** fast cross-check only.

Phases per test: initial burst → verify → mutation wave(s) → re-verify; a
separate test sweeps ``--max-concurrent-transfers``. Every iteration gets a
fresh bucket / tree (plan §7 isolation) with derived seeds ``seed_i = seed + i``.

Design: ``docs/internal/stress-test-plan.md``. Requires ``HSYNC_BB_STRESS=1`` to
run; ``make ci`` sets it at a bounded scale, ``make test-stress`` runs it full
scale.
"""

import hashlib
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
import requests
import urllib3
import urllib3.exceptions

from tests import conftest
from tests.conftest import BB_CLIENT_CID

from . import stress_tree

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_ENABLED = os.environ.get("HSYNC_BB_STRESS") == "1"
_REASON = "Stress tests are opt-in; set HSYNC_BB_STRESS=1 to enable"

pytestmark = pytest.mark.skipif(not _ENABLED, reason=_REASON)

_K = stress_tree.stress_knobs("server")


def _wire_verify(
    live_server, bucket: str, manifest: stress_tree.Manifest, mtime_tolerance: float
) -> list[stress_tree.Mismatch]:
    """Primary server-mode verification over the public REST surface (plan §6).

    Structure + metadata parity from the ``/files`` and ``/dirs`` listings,
    then one download per file re-hashed on arrival (``Accept-Encoding:
    identity`` so the digest is computed over the stored bytes whatever the
    server's compression decision). Content is thus verified as any real
    client would observe it — no internal server state is consulted.
    """
    base, api_key = live_server
    headers = {"Authorization": f"Bearer {api_key}"}
    mismatches: list[stress_tree.Mismatch] = []

    r = requests.get(
        f"{base}/api/v1/buckets/{bucket}/files", headers=headers, timeout=300, verify=False
    )
    assert r.status_code == 200, r.text
    listed = {f["filepath"]: f for f in r.json()}
    files = {p: m for p, m in manifest.items() if m["type"] == "file"}
    for rel in sorted(files):
        m = files[rel]
        e = listed.get(rel)
        if e is None:
            mismatches.append(stress_tree.Mismatch(rel, "missing", "file", None))
            continue
        if e["size"] != m["size"]:
            mismatches.append(stress_tree.Mismatch(rel, "size", str(m["size"]), str(e["size"])))
        if e["sha256sum"] != m["sha256"]:
            mismatches.append(stress_tree.Mismatch(rel, "sha256", str(m["sha256"]), e["sha256sum"]))
        if int(e["permissions"], 8) & 0o777 != int(m["mode"]) & 0o777:
            mismatches.append(
                stress_tree.Mismatch(rel, "mode", oct(int(m["mode"]) & 0o777), e["permissions"])
            )
        if abs(float(e["mtime"]) - float(m["mtime"])) > mtime_tolerance:
            mismatches.append(stress_tree.Mismatch(rel, "mtime", str(m["mtime"]), str(e["mtime"])))
    for rel in sorted(listed):
        if rel not in files:
            # Single-writer scenario: nothing outside the manifest may exist,
            # conflicted copies included (plan §6.2 — deliberately not whitelisted).
            mismatches.append(stress_tree.Mismatch(rel, "extra", None, "listed"))

    r = requests.get(
        f"{base}/api/v1/buckets/{bucket}/dirs", headers=headers, timeout=120, verify=False
    )
    assert r.status_code == 200, r.text
    listed_dirs = {d["path"] for d in r.json()}
    dirs = {p for p, m in manifest.items() if m["type"] == "dir"}
    for rel in sorted(dirs - listed_dirs):
        mismatches.append(stress_tree.Mismatch(rel, "missing", "dir", None))
    for rel in sorted(listed_dirs - dirs):
        mismatches.append(stress_tree.Mismatch(rel, "extra", None, "listed"))

    # Per-file download re-hash (streamed; content-level black-box verdict).
    for rel in sorted(files):
        m = files[rel]
        with requests.get(
            f"{base}/api/v1/buckets/{bucket}/files/download",
            params={"filename": rel, "cid": BB_CLIENT_CID},
            headers={**headers, "Accept-Encoding": "identity"},
            timeout=300,
            verify=False,
            stream=True,
        ) as resp:
            if resp.status_code != 200:
                mismatches.append(
                    stress_tree.Mismatch(rel, "download", "200", str(resp.status_code))
                )
                continue
            h = hashlib.sha256()
            size = 0
            for chunk in resp.iter_content(chunk_size=stress_tree.CHUNK):
                h.update(chunk)
                size += len(chunk)
        if size != m["size"]:
            mismatches.append(stress_tree.Mismatch(rel, "size", str(m["size"]), str(size)))
        if h.hexdigest() != m["sha256"]:
            mismatches.append(stress_tree.Mismatch(rel, "content", str(m["sha256"]), h.hexdigest()))
    return mismatches


def _bucket_root(live_server, bucket: str) -> Path:
    """On-disk bucket root of the live server (secondary FS cross-check).

    The module-scoped ``live_server`` fixture registers its buckets dir in
    ``conftest.LIVE_SERVER_BUCKETS_DIRS`` keyed by base_url (unique per
    instance — it embeds the port).
    """
    base = live_server[0]
    buckets_dir = conftest.LIVE_SERVER_BUCKETS_DIRS.get(base)
    if buckets_dir is None:
        raise RuntimeError(f"no buckets dir registered for {base}")
    return Path(buckets_dir) / bucket


def _combined_verify(live_server, bucket, manifest, mtime_tolerance):
    """Wire-primary + FS-secondary verification; returns the merged mismatches."""
    mismatches = _wire_verify(live_server, bucket, manifest, mtime_tolerance)
    fs_mism, _fs_stats = stress_tree.verify_tree(
        _bucket_root(live_server, bucket), manifest, mode="server", mtime_tolerance=mtime_tolerance
    )
    return mismatches + fs_mism


def _report(record: dict[str, object]) -> None:
    stress_tree.collect(record)
    stress_tree.print_report(record)
    stress_tree.write_report(_K["report"], record)  # type: ignore[arg-type]


@dataclass
class _SubprocessResult:
    """Minimal Typer-``Result`` stand-in for the wall-clock subprocess driver."""

    exit_code: int
    output: str


def _subprocess_env(api_key: str, local_path: str) -> dict[str, str]:
    """Scrubbed subprocess environment for one server-mode client run.

    Mirrors ``p2p_helpers._p2p_env``: every inherited ``HSYNC_*`` is dropped (the
    developer's own configuration must never leak into the run) and a per-run
    device/state isolation root derived from *local_path* is wired in, so the
    subprocess never touches the real device identity, locks or state dirs. The
    forwarded isolation keys are taken from ``conftest._ISOLATION_MANAGED_HSYNC_
    KEYS`` (single source of truth) so the two cannot drift. ``HSYNC_API_KEY``
    carries the credential: it is never placed on the command line (same
    contract as the in-process ``run_sync_once``).
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("HSYNC_")}
    isolation = Path(local_path).parent / f"{Path(local_path).name}-stress-subproc"
    env["HSYNC_API_KEY"] = api_key
    for key in conftest._ISOLATION_MANAGED_HSYNC_KEYS:
        name = key.removeprefix("HSYNC_").removesuffix("_DIR").lower()
        env[key] = str(isolation / name)
    return env


def _subprocess_run(live_server, bucket: str, local_path: str, *extra: str) -> _SubprocessResult:
    """Run one wall-clock client sync as a real subprocess (plan §13.5).

    Enabled by ``HSYNC_BB_STRESS_SUBPROCESS=1``; ``run_sync_once`` stays the
    default so timings remain comparable. Unlike the in-process CliRunner this
    pays the real process boot and API-key env path. Output is
    captured to a log **file** (never an undrained pipe) and returned so the
    caller's ``result.exit_code`` / ``result.output`` assertions keep working.
    ``--no-autotune`` is forced and de-duplicated when the caller
    already passes it (the concurrency sweep does).
    """
    base_url, api_key = live_server
    args = [
        sys.executable,
        "-m",
        "hsync_client",
        "server",
        "-b",
        bucket,
        *conftest.bb_server_cli_args(base_url),
        "-p",
        local_path,
        "--once",
        "--yes",
        "--insecure",
        "--min-free-disk-space",
        "0",
        "--no-autotune",
        *[a for a in extra if a != "--no-autotune"],
    ]
    log_path = Path(local_path).parent / f"{Path(local_path).name}.stress-subprocess.log"
    timed_out = False
    with open(log_path, "wb") as log_fh:
        proc = subprocess.Popen(
            args,
            env=_subprocess_env(api_key, local_path),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
        )
        try:
            proc.wait(timeout=float(_K["timeout"]))
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            proc.wait(timeout=10)
    output = log_path.read_text(encoding="utf-8", errors="replace")
    if timed_out:
        return _SubprocessResult(exit_code=124, output=f"TIMEOUT after {_K['timeout']}s\n{output}")
    return _SubprocessResult(exit_code=proc.returncode, output=output)


@pytest.fixture
def sync_runner(run_sync_once, live_server):
    """Client runner used by the stress tests (plan §13.5).

    In-process ``run_sync_once`` by default; a wall-clock subprocess when
    ``HSYNC_BB_STRESS_SUBPROCESS=1``. Both expose ``(bucket, local_path, *extra)
    -> result`` with ``exit_code`` / ``output``, so the test bodies are identical
    under either mode.
    """
    if not _K["subprocess"]:
        return run_sync_once

    def _run(bucket: str, local_path: str, *extra: str) -> _SubprocessResult:
        return _subprocess_run(live_server, bucket, local_path, *extra)

    return _run


class TestServerModeStress:
    """Initial burst → verify → mutation wave(s) → re-verify, REST-primary."""

    @pytest.mark.parametrize("iteration", range(_K["iterations"]))
    def test_burst_verify_mutate_reverify(
        self,
        live_server,
        bb_bucket,
        local_dir,
        tmp_path: Path,
        sync_runner,
        iteration: int,
    ) -> None:
        seed = int(_K["seed"]) + iteration  # derived seed, plan §7
        src = tmp_path / "src"
        t0 = time.perf_counter()
        manifest = stress_tree.generate(
            src,
            seed=seed,
            n_files=int(_K["n_files"]),
            n_dirs=int(_K["n_dirs"]),
            max_depth=int(_K["max_depth"]),
            max_file_size=int(_K["max_file_size"]),
        )
        t_generate = time.perf_counter() - t0
        n_files, n_dirs, total_bytes = stress_tree.manifest_totals(manifest)

        t0 = time.perf_counter()
        result = sync_runner(bb_bucket, str(src))
        t_sync = time.perf_counter() - t0
        assert result.exit_code == 0, result.output

        t0 = time.perf_counter()
        mismatches = _combined_verify(
            live_server, bb_bucket, manifest, float(_K["mtime_tolerance"])
        )
        t_verify = time.perf_counter() - t0
        _report(
            stress_tree.make_record(
                mode="server",
                seed=seed,
                n_files=n_files,
                n_dirs=n_dirs,
                total_bytes=total_bytes,
                t_generate_s=t_generate,
                t_sync_s=t_sync,
                t_verify_s=t_verify,
                max_concurrent_transfers=0,  # autotune-managed (no explicit flag)
                iteration=iteration,
                phase="initial_burst",
                mismatches=mismatches,
            )
        )
        if mismatches:
            stress_tree.fail_with(mismatches)

        for wave in range(int(_K["waves"])):
            manifest, _primitives = stress_tree.apply_mutation(
                src,
                manifest,
                seed=seed,
                wave=wave,
                fraction=float(_K["mutation_fraction"]),
                max_file_size=int(_K["max_file_size"]),
            )
            n_files, n_dirs, total_bytes = stress_tree.manifest_totals(manifest)
            t0 = time.perf_counter()
            result = sync_runner(bb_bucket, str(src))
            t_sync = time.perf_counter() - t0
            assert result.exit_code == 0, result.output
            t0 = time.perf_counter()
            mismatches = _combined_verify(
                live_server, bb_bucket, manifest, float(_K["mtime_tolerance"])
            )
            t_verify = time.perf_counter() - t0
            _report(
                stress_tree.make_record(
                    mode="server",
                    seed=seed,
                    n_files=n_files,
                    n_dirs=n_dirs,
                    total_bytes=total_bytes,
                    t_generate_s=0.0,
                    t_sync_s=t_sync,
                    t_verify_s=t_verify,
                    max_concurrent_transfers=0,
                    iteration=iteration,
                    phase=f"mutation_wave{wave}",
                    mismatches=mismatches,
                )
            )
            if mismatches:
                stress_tree.fail_with(mismatches)

    @pytest.mark.parametrize("max_transfers", _K["concurrency"])
    @pytest.mark.parametrize("iteration", range(_K["iterations"]))
    def test_concurrency_sweep(
        self,
        live_server,
        bb_bucket,
        local_dir,
        tmp_path: Path,
        sync_runner,
        iteration: int,
        max_transfers: int,
    ) -> None:
        """Initial burst with an explicit ``--max-concurrent-transfers`` bound
        (plan §9): each N is its own test case with a fresh tree + bucket."""
        seed = int(_K["seed"]) + iteration
        src = tmp_path / "src"
        manifest = stress_tree.generate(
            src,
            seed=seed,
            n_files=int(_K["n_files"]),
            n_dirs=int(_K["n_dirs"]),
            max_depth=int(_K["max_depth"]),
            max_file_size=int(_K["max_file_size"]),
        )
        n_files, n_dirs, total_bytes = stress_tree.manifest_totals(manifest)

        t0 = time.perf_counter()
        result = sync_runner(
            bb_bucket,
            str(src),
            "--no-autotune",
            "--max-concurrent-transfers",
            str(max_transfers),
        )
        t_sync = time.perf_counter() - t0
        assert result.exit_code == 0, result.output

        t0 = time.perf_counter()
        mismatches = _combined_verify(
            live_server, bb_bucket, manifest, float(_K["mtime_tolerance"])
        )
        t_verify = time.perf_counter() - t0
        _report(
            stress_tree.make_record(
                mode="server",
                seed=seed,
                n_files=n_files,
                n_dirs=n_dirs,
                total_bytes=total_bytes,
                t_generate_s=0.0,
                t_sync_s=t_sync,
                t_verify_s=t_verify,
                max_concurrent_transfers=max_transfers,
                iteration=iteration,
                phase="concurrency_sweep",
                mismatches=mismatches,
            )
        )
        if mismatches:
            stress_tree.fail_with(mismatches)
