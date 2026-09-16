# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""L1 — LAN TOFU pin-store micro-benchmark (disk-backed, temp file).

Measures the per-connection trust checks in
:class:`~hsync_client.pinning.PinStore`, the durable Trust-On-First-Use store the
LAN slave consults before every master connection:

* ``get`` (hit) / (miss) — read + advisory flock + JSON load, on a present and
  an absent ``(cid, bucket)`` pin;
* ``check_and_record`` (matched) — the steady-state trust classification: the
  stored pin matches the observed fingerprint, so no write happens;
* ``check_and_record`` (changed) — a mismatch (possible MITM): compared and
  refused with no write;
* ``check_and_record`` (first use) — a never-seen ``(cid, bucket)``: the
  durable TOFU persist plus verification read-back;
* ``set`` — the atomic owner-only (0o600) repin: temp write + fsync + replace,
  the durable cost paid on first use / rotate.

One ``TemporaryDirectory`` per run keeps the pin file out of the OS state dir;
the store is pre-seeded so each measured op hits the intended branch.

Usage:
    cd hsync-client && .venv/bin/python bench/bench_pinning.py [--count N] [--json-out FILE]
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

# Allow ``from common import ...`` when run from the bench/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402  (import after sys.path tweak)
    BenchResult,
    add_json_out,
    maybe_write_json,
    measure,
    print_table,
)
from hsync_common.protocol.pairing import PinDecision  # noqa: E402

from hsync_client.pinning import PinStore  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=2000)
    add_json_out(parser)
    args = parser.parse_args()
    count = args.count

    with tempfile.TemporaryDirectory(prefix="hsync_pin_") as tmp:
        store = PinStore(Path(tmp) / "pins.json")
        # The durable first-use path grows its own file: mixing its 200 fresh pins
        # into ``store`` would inflate every later read/write (larger JSON), so the
        # two scenarios stay isolated per file (one scenario, one fixture).
        first_use_store = PinStore(Path(tmp) / "first_use.json")
        store.set("cid-1", "bucket", "aa:bb:cc")
        store.set("cid-2", "bucket", "11:22:33")
        assert store.get("cid-1", "bucket") == "aabbcc"
        assert store.check_and_record("cid-1", "bucket", "AABBCC") is PinDecision.matched
        assert store.check_and_record("cid-2", "bucket", "aa:bb:cc") is PinDecision.changed
        assert store.get("never-seen", "bucket") is None

        def op_get() -> None:
            assert store.get("cid-1", "bucket") == "aabbcc"

        def op_get_miss() -> None:
            assert store.get("never-seen", "bucket") is None

        def op_check_and_record() -> None:
            assert store.check_and_record("cid-1", "bucket", "aabbcc") is PinDecision.matched

        def op_check_changed() -> None:
            assert store.check_and_record("cid-2", "bucket", "aa:bb:cc") is PinDecision.changed

        first_use_state = {"i": 0}

        def op_check_first_use() -> None:
            i = first_use_state["i"]
            first_use_state["i"] += 1
            assert (
                first_use_store.check_and_record(f"first-{i}", "bucket", "aa:bb:cc")
                is PinDecision.trusted
            )

        def op_set() -> None:
            store.set("cid-1", "bucket", "aa:bb:cc")
            assert store.get("cid-1", "bucket") == "aabbcc"

        results: list[BenchResult] = [
            measure("PinStore.get(hit)", op_get, count=count),
            measure("PinStore.get(miss)", op_get_miss, count=count),
            measure("PinStore.check_and_record(match)", op_check_and_record, count=count),
            measure("PinStore.check_and_record(changed)", op_check_changed, count=count),
            measure("PinStore.check_and_record(first_use)", op_check_first_use, count=200),
            measure("PinStore.set(repin)", op_set, count=count),
        ]

        print(f"pinning micro-benchmark (ops={count}, temp pin file)")
        print_table(results)
        maybe_write_json(args.json_out, results)


if __name__ == "__main__":
    main()
