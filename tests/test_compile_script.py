"""Guard for the container build commands in ``../compile.sh``.

The ``build`` group (PyInstaller) must be installed inside the cross-build
containers. The client currently asks for it by exclusion —
``poetry install --no-root --without dev`` — which keeps it (``--without`` only
drops the listed groups), so this module is a **regression guard**, not a fix.
It exists because the switch to an ``--only`` list is a silent trap under
Poetry 2.x (2.3.2, pinned in ``packaging/deb/Dockerfile.builder``): ``--only``
prunes every group not named in its list and ignores a co-present ``--with``, so
``poetry install --only main --with build`` exits 0 while installing no
PyInstaller. That exact form silently broke ``rpi``/``deb-all`` in
hsync-bootnode, hsync-server and hsync-desktop; the equivalent guard lives there
too. A white-box guard on the script text is used because exercising the real
cross-build needs Docker, qemu and network access.

Consumers: hsync-client maintainers (regression guard).
"""

from __future__ import annotations

import re
from pathlib import Path

COMPILE_SH = Path(__file__).resolve().parents[1] / "compile.sh"

# The historical regression: Poetry 2.x ignores `--with` next to `--only`.
_BROKEN = "poetry install --only main --with build"
_FIXED = "poetry install --only main,build"


def _script() -> str:
    return COMPILE_SH.read_text(encoding="utf-8")


def _poetry_install_lines() -> list[str]:
    return [line.strip() for line in _script().splitlines() if "poetry install" in line]


def _build_group_requested(line: str) -> bool:
    """True when ``line`` installs the ``build`` group under Poetry 2.x rules."""
    only = re.search(r"--only\s+([^\s;]+)", line)
    if only:
        # Inside --only the group list is exhaustive: --with cannot re-add it.
        return "build" in only.group(1).split(",")
    return "--without build" not in line and "--without=build" not in line


def test_guard_rejects_the_pruned_build_group() -> None:
    """The guard must fail on the pre-fix form and pass on the fixed one."""
    assert not _build_group_requested(_BROKEN)
    assert _build_group_requested(_FIXED)
    # The `;`-terminated form used inside the container retry loop.
    assert not _build_group_requested("if poetry install --only main --with build; then :; fi")
    assert _build_group_requested("if poetry install --only main,build; then :; fi")


def test_every_container_build_installs_the_build_group() -> None:
    lines = _poetry_install_lines()
    assert lines, "no 'poetry install' invocation found in compile.sh"
    for line in lines:
        assert _build_group_requested(line), f"build group not installed: {line}"


def test_container_workdir_is_the_poetry_project_root() -> None:
    """The relative artifact paths need the project to sit at the repo root.

    hsync-bootnode hit the opposite case: its Poetry project lives in ``python/``,
    so the container workdir was one level below the repository root and the
    relative ``build/deb-all/...`` distpath landed in ``python/build/``. If this
    project ever moves into a subdirectory, the container workdir must be
    remounted and the artifact paths made absolute.
    """
    assert "-w /repo/hsync-client" in _script()
    assert (COMPILE_SH.parent / "pyproject.toml").is_file()
