"""Tests for the release installation bootstrap."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = PROJECT_ROOT / "install.sh"


def _write_fake_commands(tmp_path: Path) -> None:
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
) -> subprocess.CompletedProcess[str]:
    _write_fake_commands(tmp_path)
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}{os.pathsep}{env['PATH']}"
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
