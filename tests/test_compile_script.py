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


def test_rpm_version_mapping_is_registered() -> None:
    """The RPM targets must carry the SemVer -> RPM Version: mapping.

    RPM >= 4.10 uses ``~`` for "sorts before everything", so the mapping is the
    same as the Debian one (first ``-`` becomes ``~``). ``_semver_to_rpm`` must
    therefore exist and delegate to ``_semver_to_debian`` rather than re-encode
    the rule.
    """
    script = _script()
    assert "_semver_to_rpm()" in script
    assert '_semver_to_debian "$1"' in script


def test_rpm_arch_mapping_is_64_bit_only() -> None:
    """RHEL/Rocky is 64-bit only, so only x86_64 and aarch64 are mapped."""
    script = _script()
    assert 'x86_64|amd64)   echo "x86_64"' in script
    assert 'aarch64|arm64)  echo "aarch64"' in script


def test_rpm_targets_are_registered_in_dispatch() -> None:
    """``rpm`` and ``rpm-all`` must be wired into the main() dispatch and help."""
    script = _script()
    assert "rpm)" in script
    assert "rpm-all)" in script
    assert "Supported targets:" in script
    assert "rpm-all" in script
