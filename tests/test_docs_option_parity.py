# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Contract test: the CLI, the user docs and the man page list the same options.

The man page and ``docs/`` are the only offline references for the CLI, and the
packaging rules require both to track every flag. Nothing at runtime catches the
two drift directions: a new flag with no man entry is invisible to ``man hsync``,
and a removed flag left in the page sends users to a command that exits 2.
"""

from __future__ import annotations

import re
from pathlib import Path

import click
import typer.main

from hsync_client.cli import app

_CLIENT_ROOT = Path(__file__).resolve().parents[1]
_MAN_PAGE = _CLIENT_ROOT / "packaging" / "deb" / "usr" / "share" / "man" / "man1" / "hsync.1"

# click registers these on every command; they are not documented as options.
_BUILTIN_FLAGS = frozenset({"--help", "--version"})


def _cli_parameters(*, include_hidden: bool = False) -> list[set[str]]:
    """Long flags of each parameter, one entry per option (``--x/--no-x`` pair)."""
    parameters: list[set[str]] = []
    visited: set[int] = set()

    def walk(command: click.Command) -> None:
        if id(command) in visited:
            return
        visited.add(id(command))
        for param in command.params:
            if getattr(param, "hidden", False) and not include_hidden:
                continue
            long_flags = {
                opt
                for opt in (*getattr(param, "opts", ()), *getattr(param, "secondary_opts", ()))
                if opt.startswith("--")
            }
            if long_flags:
                parameters.append(long_flags)
        for sub in getattr(command, "commands", {}).values():
            walk(sub)

    walk(typer.main.get_command(app))
    return parameters


def _cli_flags(*, include_hidden: bool = False) -> set[str]:
    return {flag for group in _cli_parameters(include_hidden=include_hidden) for flag in group}


def _man_text() -> str:
    return _MAN_PAGE.read_text(encoding="utf-8").replace("\\-", "-")


def _mentioned(flag: str, text: str) -> bool:
    """True when *flag* appears as a whole option (not as another flag's prefix)."""
    return re.search(rf"{re.escape(flag)}(?![a-z0-9-])", text) is not None


def _man_entries() -> set[str]:
    """Long options that own a definition block (a ``.TP`` entry) in the page.

    Inline ``.BR \\-\\-flag`` mentions inside prose (e.g. the bootnode's own
    ``--ports``) are intentionally not entries: they document another binary.
    """
    lines = _MAN_PAGE.read_text(encoding="utf-8").splitlines()
    entries: set[str] = set()
    for index, line in enumerate(lines):
        if line.strip() != ".TP":
            continue
        for candidate in lines[index + 1 :]:
            if not candidate.strip():
                continue
            if candidate.startswith((".BR", ".B")):
                match = re.search(r"--[a-z0-9][a-z0-9-]*", candidate.replace("\\-", "-"))
                if match:
                    entries.add(match.group())
            break
    return entries


def _user_docs_text() -> str:
    return "\n".join(
        page.read_text(encoding="utf-8") for page in sorted((_CLIENT_ROOT / "docs").glob("*.md"))
    )


def test_every_option_is_mentioned_in_the_man_page() -> None:
    man = _man_text()
    missing = sorted(
        next(iter(group)) if len(group) == 1 else " / ".join(sorted(group))
        for group in _cli_parameters()
        if not any(_mentioned(flag, man) for flag in group)
    )
    assert not missing, f"options missing from hsync.1: {missing}"


def test_man_page_has_no_removed_option() -> None:
    stale = sorted(_man_entries() - _cli_flags(include_hidden=True) - _BUILTIN_FLAGS)
    assert not stale, f"hsync.1 documents options the CLI does not accept: {stale}"


def test_every_option_is_documented_in_the_user_docs() -> None:
    docs = _user_docs_text()
    missing = sorted(flag for flag in _cli_flags() if not _mentioned(flag, docs))
    assert not missing, f"options missing from docs/: {missing}"
