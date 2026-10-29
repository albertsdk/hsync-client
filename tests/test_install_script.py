"""Tests for the release installation bootstrap."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = PROJECT_ROOT / "install.sh"


def _restricted_bin(tmp_path: Path, names: tuple[str, ...]) -> Path:
    """A PATH dir holding only the given tools, so a real tool on the runner
    cannot leak into the "tool missing" tests."""
    bin_dir = tmp_path / "restricted-bin"
    bin_dir.mkdir(exist_ok=True)
    for name in names:
        real = shutil.which(name)
        if real is not None:
            (bin_dir / name).symlink_to(real)
    return bin_dir


def _write_fake_commands(tmp_path: Path, *, with_curl: bool = True) -> None:
    if not with_curl:
        return
    # Fake curl only answers the latest-release resolution; HSYNC_FAKE_CURL_FAIL
    # simulates a network error, HSYNC_FAKE_LATEST_TAG overrides the tag.
    curl = tmp_path / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        '[[ -z "${HSYNC_FAKE_CURL_FAIL:-}" ]] || exit 22\n'
        "printf 'https://github.com/albertsdk/hsync-client/releases/tag/%s\\n' "
        '"${HSYNC_FAKE_LATEST_TAG:-v9.9.9}"\n'
    )
    curl.chmod(0o755)


def _run_installer(
    tmp_path: Path,
    *args: str,
    extra_env: dict[str, str] | None = None,
    with_curl: bool = True,
    path_tools: tuple[str, ...] | None = None,
) -> subprocess.CompletedProcess[str]:
    _write_fake_commands(tmp_path, with_curl=with_curl)
    env = os.environ.copy()
    if path_tools is None:
        env["PATH"] = f"{tmp_path}{os.pathsep}{env['PATH']}"
    else:
        env["PATH"] = f"{tmp_path}{os.pathsep}{_restricted_bin(tmp_path, path_tools)}"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [
            str(INSTALL_SCRIPT),
            "--install-dir",
            str(tmp_path / "installation"),
            "--symlink-dir",
            str(tmp_path / "bin"),
            *args,
        ],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_help_has_no_runtime_dependencies(tmp_path: Path):
    result = subprocess.run(
        [str(INSTALL_SCRIPT), "--help"],
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Install the HSYNC client binary" in result.stdout


def test_dry_run_does_not_create_installation(tmp_path: Path):
    result = _run_installer(tmp_path, "--dry-run")

    assert result.returncode == 0, result.stderr
    assert "Dry run" in result.stdout
    assert not (tmp_path / "installation").exists()


def test_invalid_version_is_rejected_before_any_download(tmp_path: Path):
    result = _run_installer(tmp_path, "--version", "not-a-version", "--dry-run")

    assert result.returncode == 1
    assert "invalid version 'not-a-version'" in result.stderr
    assert not (tmp_path / "installation").exists()


def test_latest_resolves_to_the_stable_release_tag(tmp_path: Path):
    result = _run_installer(tmp_path, "--version", "latest", "--dry-run")

    assert result.returncode == 0, result.stderr
    assert "Version:    v9.9.9" in result.stdout
    assert "hsync-client-v9.9.9-" in result.stdout
    assert not (tmp_path / "installation").exists()


def test_latest_resolution_failure_writes_nothing(tmp_path: Path):
    result = _run_installer(
        tmp_path,
        "--version",
        "latest",
        "--dry-run",
        extra_env={"HSYNC_FAKE_CURL_FAIL": "1"},
    )

    assert result.returncode == 1
    assert "could not resolve the latest release" in result.stderr
    assert not (tmp_path / "installation").exists()


def test_latest_rejects_an_unexpected_tag_format(tmp_path: Path):
    result = _run_installer(
        tmp_path,
        "--version",
        "latest",
        "--dry-run",
        extra_env={"HSYNC_FAKE_LATEST_TAG": "not-a-version"},
    )

    assert result.returncode == 1
    assert "unexpected latest release tag" in result.stderr
    assert not (tmp_path / "installation").exists()


# --- Prerequisite guidance ---------------------------------------------------
#
# The sha256 tooling branch in sha256_verify is not covered here: reaching it
# needs successful download/extraction steps that the fake curl cannot emulate,
# so it is only syntax-checked (``bash -n``).


def test_missing_curl_prints_hint(tmp_path: Path):
    result = _run_installer(tmp_path, with_curl=False, path_tools=("bash", "sh", "uname"))

    assert result.returncode == 1
    assert "required command not found: curl" in result.stderr
    assert 'downloads the release archive with "curl"' in result.stderr
    assert "Then re-run:" in result.stderr
    assert "releases/latest/download/install.sh" in result.stderr


def test_missing_tar_prints_hint(tmp_path: Path):
    result = _run_installer(
        tmp_path,
        path_tools=("bash", "sh", "uname", "mktemp", "install", "ln", "mkdir"),
    )

    assert result.returncode == 1
    assert "required command not found: tar" in result.stderr
    assert 'extracts the release archive with "tar"' in result.stderr
    assert "Then re-run:" in result.stderr
