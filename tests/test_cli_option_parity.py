# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Contract test: ``hsync p2p`` and ``hsync server`` share one option surface.

Both commands drive the same engine, so a knob that applies to sync must exist
on both. Typer requires every option to be declared explicitly in each command
signature, so nothing at runtime can enforce that: this test does. Every option
must be classified below as common or mode-specific — adding or removing a flag
fails until it is classified, and a common flag whose type/default drifts fails
too.
"""

from __future__ import annotations

import pytest
import typer.core
import typer.main
from hsync_common import defaults
from typer.testing import CliRunner

from hsync_client import constants
from hsync_client.cli import (
    _SHARED_VALIDATION_KEYS,
    _validate_shared_options,
    app,
)

COMMON_OPTIONS = frozenset(
    {
        "allow_deletions",
        "bucket",
        "ca_cert",
        "color",
        "delta_min_file_size",
        "delta_min_file_size_p2p",
        "dryrun",
        "env_file",
        "events_json_fd",
        "filter_path",
        "http_timeout",
        "deletion_policy",
        "ignore_patterns",
        "insecure",
        "local_path",
        "logfile",
        "mass_delete_guard",
        "mass_delete_percent",
        "mass_delete_threshold",
        "max_bandwidth_download",
        "max_bandwidth_upload",
        "max_concurrent_transfers",
        "max_inbound_size",
        "max_wait_timeout",
        "min_free_disk_space",
        "no_autotune",
        "no_compression",
        "no_delta",
        "once",
        "quiet",
        "reconnect_delay",
        "resync",
        "stability_delay",
        "sync_direction",
        "verbose",
    }
)

P2P_ONLY_OPTIONS = frozenset(
    {
        "allow_unauthenticated_p2p",
        "allow_unauthenticated_writes",
        "discovery_server",
        "discovery_server_fingerprint",
        "discovery_server_trust",
        "discovery_server_tofu",
        "peer_discovery_timeout",
        "never_master",
        "no_peer_download",
        "p2p_group",
        "p2p_passphrase_fd",
        "p2p_key_fd",
        "p2p_listen_port",
        "prefer_master",
        "p2p_trust",
        "map_data_port",
    }
)

SERVER_ONLY_OPTIONS = frozenset(
    {
        "api_key_fd",
        "server_fingerprint",
        "server_url",
        "server_tofu",
        "server_trust",
    }
)


@pytest.fixture(scope="module")
def commands() -> dict[str, typer.core.TyperCommand]:
    """The Typer-built commands, so the assertions read the real CLI definition."""
    group = typer.main.get_command(app)
    return {name: group.commands[name] for name in ("p2p", "server")}


def _params(
    command: typer.core.TyperCommand,
) -> dict[str, typer.core.TyperOption | typer.core.TyperArgument]:
    return {param.name: param for param in command.params if getattr(param, "name", None)}


def _shape(param: typer.core.TyperOption | typer.core.TyperArgument) -> tuple[object, ...]:
    """Everything that must match for a *shared* flag (help may legitimately differ)."""
    return (
        param.param_type_name,
        str(param.type),
        repr(param.default),
        param.required,
        param.hidden,
        tuple(param.opts or param.secondary_opts),
    )


class TestSyncOptionParity:
    def test_every_option_is_classified(self, commands):
        assert set(_params(commands["p2p"])) == COMMON_OPTIONS | P2P_ONLY_OPTIONS
        assert set(_params(commands["server"])) == COMMON_OPTIONS | SERVER_ONLY_OPTIONS

    def test_classification_is_a_partition(self):
        assert COMMON_OPTIONS.isdisjoint(P2P_ONLY_OPTIONS)
        assert COMMON_OPTIONS.isdisjoint(SERVER_ONLY_OPTIONS)
        assert P2P_ONLY_OPTIONS.isdisjoint(SERVER_ONLY_OPTIONS)

    def test_shared_options_keep_the_same_definition(self, commands):
        p2p = _params(commands["p2p"])
        server = _params(commands["server"])
        mismatched = {
            name: (_shape(p2p[name]), _shape(server[name]))
            for name in sorted(COMMON_OPTIONS)
            if _shape(p2p[name]) != _shape(server[name])
        }
        assert mismatched == {}

    def test_shared_validation_keys_are_common_options(self):
        """The TypedDict the validator reads may only name real shared options.

        A key that no command declares would otherwise surface only as a runtime
        ``KeyError`` when that command runs; a typo is caught here instead.
        """
        assert set(_SHARED_VALIDATION_KEYS) <= COMMON_OPTIONS

    def test_validator_names_a_missing_option(self):
        """A frame missing a shared option fails fast, naming the option."""
        with pytest.raises(RuntimeError, match="'http_timeout'"):
            _validate_shared_options({})

    def test_help_lists_every_shared_flag(self, commands):
        """Parser-level guard: each shared flag is really rendered by both commands.

        The introspection assertions above read Typer's metadata; this one drives
        the actual parser/formatter through the public ``--help`` path, so a
        parameter that fails to materialise (or a renamed flag) is caught too.
        """
        runner = CliRunner()
        # A wide terminal keeps Rich from abbreviating long flag names
        # (``--delta-min-file-s…``), so the assertion can match them verbatim.
        wide = {"COLUMNS": "200"}
        p2p_help = runner.invoke(app, ["p2p", "--help"], color=False, env=wide)
        server_help = runner.invoke(app, ["server", "--help"], color=False, env=wide)
        assert p2p_help.exit_code == 0, p2p_help.output
        assert server_help.exit_code == 0, server_help.output
        for name in sorted(COMMON_OPTIONS):
            param = _params(commands["p2p"])[name]
            if param.hidden:
                # Internal file-descriptor plumbing (e.g. --events-json-fd) is
                # deliberately absent from the help output.
                continue
            flag = (param.opts or param.secondary_opts)[0]
            assert flag in p2p_help.output, f"{flag} missing from `p2p --help`"
            assert flag in server_help.output, f"{flag} missing from `server --help`"


_KNOB_BOUNDS = (
    "RECONNECT_DELAY_MIN",
    "RECONNECT_DELAY_MAX",
    "STABILITY_DELAY_MIN",
    "STABILITY_DELAY_MAX",
    "MAX_WAIT_TIMEOUT_MIN",
    "MAX_WAIT_TIMEOUT_MAX",
    "RESYNC_MIN",
    "RESYNC_MAX",
    "HTTP_TIMEOUT_MIN",
    "HTTP_TIMEOUT_MAX",
    "MAX_CONCURRENT_TRANSFERS_MIN",
    "MAX_CONCURRENT_TRANSFERS_MAX",
    "MIN_FREE_DISK_SPACE_MIN",
    "MIN_FREE_DISK_SPACE_MAX",
    "MAX_INBOUND_SIZE_MB_MIN",
    "MAX_INBOUND_SIZE_MB_MAX",
    "P2P_PEER_READ_CAP_MIN",
    "P2P_PEER_READ_CAP_MAX",
    "P2P_PEER_SOURCES_MAX_MIN",
    "P2P_PEER_SOURCES_MAX_MAX",
)


@pytest.mark.parametrize("name", _KNOB_BOUNDS)
def test_validation_bounds_are_the_shared_ones(name: str) -> None:
    """A bound copied locally would let a consumer emit a value the CLI rejects."""
    assert getattr(constants, name) == getattr(defaults, name)
