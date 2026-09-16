# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Startup benchmark — process spawn cost of the ``hsync`` CLI (source + binary).

Measures wall-clock time-to-output for every fast invocation of ``hsync``
(``--version``, ``--capabilities-json``, ``--help``, ``--help-symbols``) plus the
two import-only cases (``hsync_client.cli`` and ``hsync_client.main_logic``).
Each timed sample spawns a fresh process, so the measured region covers
interpreter/PyInstaller bootloader startup, imports and argument parsing up to
the process exit.

Two forms are measured:

* **source** — the dev venv console script (``.venv/bin/hsync``) or
  ``python -m hsync_client``, isolating the Python import cost from the
  bootloader;
* **binary** — the packaged PyInstaller ``--onefile`` artifact
  (``dist/<os>-<arch>/hsync``), and optionally a ``--onedir`` artifact via
  ``--binary-dir`` (measurement only; no packaging decision here).

This script is standalone (like the L2 wire benchmarks): it spawns processes and
depends on local binary paths, so it is deliberately **not** part of
``run_all.py`` / ``bench_result.txt``. Compare two runs with ``compare.py``.

Usage:
    cd hsync-client && .venv/bin/python bench/bench_startup.py --count 30
    .venv/bin/python bench/bench_startup.py --count 30 --source --json-out /tmp/startup.json
    .venv/bin/python bench/bench_startup.py --count 30 --binary dist/linux-amd64/hsync
    .venv/bin/python bench/bench_startup.py --count 30 --binary-dir /usr/lib/hsync
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
from collections.abc import Callable
from functools import partial
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

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_VERSION_RE = re.compile(r"hsync \d+\.\d+\.\d+")

# Output checks. Each is a callable over the combined stdout+stderr of the
# spawned process; a failed check raises AssertionError so a silent fallback or
# a dropped branch cannot be reported as a fast success.
Check = Callable[[str], bool]


def _contains(marker: str) -> Check:
    return lambda output: marker in output


def _is_capabilities_json(output: str) -> bool:
    try:
        doc = json.loads(output)
    except (ValueError, TypeError):
        return False
    return isinstance(doc, dict) and "schema_version" in doc


def _platform_tag() -> str:
    """Platform tag matching hsync-client/compile.sh (e.g. ``linux-amd64``)."""
    system = platform.system().lower()
    if system.startswith("mingw") or system.startswith("cygwin") or system.startswith("msys"):
        system = "windows"
    machine = platform.machine().lower()
    arch_map = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
        "armv7l": "armv7",
        "armv7": "armv7",
        "i386": "x86",
        "i686": "x86",
    }
    arch = arch_map.get(machine, machine)
    return f"{system}-{arch}"


def _clean_env() -> dict[str, str]:
    """Environment for the spawned processes: no ``HSYNC_*``, deterministic color."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("HSYNC_")}
    env["NO_COLOR"] = "1"
    env["PYTHONWARNINGS"] = "ignore"
    return env


def _console_script(python: str) -> str | None:
    """Return the dev-venv ``hsync`` console script next to ``python``, or None."""
    candidate = Path(python).with_name("hsync")
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def _default_binary() -> str | None:
    """Return the packaged ``dist/<os>-<arch>/hsync`` if present."""
    candidate = _PROJECT_ROOT / "dist" / _platform_tag() / "hsync"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def _run(cmd: list[str], *, check: Check, timeout: int) -> None:
    """Run ``cmd`` and assert it exited 0 and passed the ``check``."""
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_clean_env(),
        cwd=str(_PROJECT_ROOT),
    )
    output = f"{proc.stdout}\n{proc.stderr}"
    assert proc.returncode == 0, f"{cmd} exited {proc.returncode}: {output[-400:]}"
    assert check(output), f"{cmd} output check failed: {output[-400:]}"


def _source_cmd(python: str) -> list[str]:
    """Console script when available, else ``python -m hsync_client``."""
    console = _console_script(python)
    if console is not None:
        return [console]
    return [python, "-m", "hsync_client"]


def _scenarios(
    python: str,
    source: bool,
    binary: str | None,
    binary_dir: str | None,
) -> list[tuple[str, list[str], Check, int]]:
    """Return ``(name, cmd, check, timeout)`` tuples."""
    scenarios: list[tuple[str, list[str], Check, int]] = []
    if source:
        source_cmd = _source_cmd(python)
        scenarios += [
            (
                "source_import_cli",
                [python, "-c", "import hsync_client.cli"],
                lambda _out: True,
                60,
            ),
            (
                "source_import_main_logic",
                [python, "-c", "import hsync_client.main_logic"],
                lambda _out: True,
                60,
            ),
            (
                "source_version",
                [*source_cmd, "--version"],
                lambda o: bool(_VERSION_RE.search(o)),
                30,
            ),
            (
                "source_capabilities",
                [*source_cmd, "--capabilities-json"],
                _is_capabilities_json,
                30,
            ),
            ("source_help_symbols", [*source_cmd, "--help-symbols"], _contains("Log legend"), 30),
            ("source_help", [*source_cmd, "--help"], _contains("Usage"), 30),
        ]
    # The version/capabilities probes are the exact two subprocess calls the
    # Desktop GUI performs at startup (engine.get_binary_version /
    # get_binary_capabilities), so they are the representative binary scenarios.
    if binary is not None:
        scenarios += [
            ("binary_version", [binary, "--version"], lambda o: bool(_VERSION_RE.search(o)), 60),
            ("binary_capabilities", [binary, "--capabilities-json"], _is_capabilities_json, 60),
        ]
    if binary_dir is not None:
        bin_path = str(Path(binary_dir) / "hsync")
        scenarios += [
            ("onedir_version", [bin_path, "--version"], lambda o: bool(_VERSION_RE.search(o)), 60),
            ("onedir_capabilities", [bin_path, "--capabilities-json"], _is_capabilities_json, 60),
        ]
    return scenarios


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=30)
    parser.add_argument("--source", action="store_true", default=True, help=argparse.SUPPRESS)
    parser.add_argument(
        "--no-source", dest="source", action="store_false", help="Skip the source (venv) scenarios."
    )
    parser.add_argument(
        "--binary",
        default=None,
        help="Packaged --onefile binary (default: dist/<os>-<arch>/hsync).",
    )
    parser.add_argument(
        "--no-binary", action="store_true", help="Skip the packaged binary scenarios."
    )
    parser.add_argument(
        "--binary-dir",
        default=None,
        help="Packaged --onedir directory containing 'hsync' (measurement only).",
    )
    parser.add_argument(
        "--python", default=sys.executable, help="Python interpreter for source/import scenarios."
    )
    add_json_out(parser)
    args = parser.parse_args()

    binary = args.binary
    if binary is None and not args.no_binary:
        binary = _default_binary()

    scenarios = _scenarios(
        python=args.python,
        source=args.source,
        binary=binary,
        binary_dir=args.binary_dir,
    )

    print(
        f"hsync-client startup benchmark — count={args.count}",
        f"python {sys.version.split()[0]} — {platform.system().lower()}",
    )
    print("comparative figures (this machine, warm page cache), not SLAs\n")

    results: list[BenchResult] = []
    for name, cmd, check, timeout in scenarios:
        results.append(
            measure(
                name, partial(_run, cmd, check=check, timeout=timeout), count=args.count, warmup=1
            )
        )

    print_table(results)
    maybe_write_json(args.json_out, results)


if __name__ == "__main__":
    main()
