# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Command-line interface for HSYNC Client.

This module provides the Typer-based CLI for the HSYNC command-line interface.
The parent callback carries the global options (``--version``,
``--capabilities-json``, ``--help-symbols``); the mutually exclusive
``p2p``/``server`` sync subcommands sit under it, alongside two maintenance
namespaces: ``trust`` (certificate pins) and ``state`` (local sync state).
"""

import json
import signal
import sys
import threading
from os import getcwd
from typing import Annotated

import typer
import typer.main
from hsync_common.endpoint import (
    format_authority,
)
from hsync_common.protocol.pairing import (
    parse_fingerprint,
)
from hsync_common.protocol.tls_trust import TrustPolicy, resolve_self_signed_policy
from hsync_common.runtime import ColorMode

from hsync_client.cli_options import (
    AllowDeletionsOpt,
    AllowUnauthenticatedP2POpt,
    AllowUnauthenticatedWritesOpt,
    ApiKeyFdOpt,
    BucketOpt,
    CaCertOpt,
    ColorOpt,
    DeletionPolicyOpt,
    DeltaMinFileSizeOpt,
    DeltaMinFileSizeP2POpt,
    DryRunOpt,
    EnvFileOpt,
    EventsJsonFdOpt,
    GroupOpt,
    HostArg,
    HttpTimeoutOpt,
    IgnorePatternOpt,
    InsecureOpt,
    IntervalOpt,
    LimitDownloadOpt,
    LimitUploadOpt,
    ListenPortOpt,
    LogFileOpt,
    MassDeleteGuardOpt,
    MassDeletePercentOpt,
    MassDeleteThresholdOpt,
    MaxConcurrentTransfersOpt,
    MaxInboundSizeOpt,
    MaxWaitTimeoutOpt,
    MinFreeDiskSpaceOpt,
    NeverMasterOpt,
    NoAutotuneOpt,
    NoCompressionOpt,
    NoDeltaOpt,
    NoMdnsOpt,
    NoPeerDownloadOpt,
    OnceOpt,
    P2PKeyFdOpt,
    P2PPassphraseFdOpt,
    P2PTrustOpt,
    PathOpt,
    PreferMasterOpt,
    QuietOpt,
    RendezvousArg,
    RendezvousFingerprintOpt,
    RendezvousTimeoutOpt,
    RendezvousTofuOpt,
    RendezvousTransportOpt,
    RendezvousTrustOpt,
    RetryDelayOpt,
    ServerFingerprintOpt,
    ServerTofuOpt,
    ServerTrustOpt,
    ServerUrlOpt,
    StabilityDelayOpt,
    SubdirOpt,
    SyncDirectionOpt,
    UpnpOpt,
    VerboseOpt,
    _build_rendezvous_server_pins,
    _build_rendezvous_servers,
    _build_run_options,
    _build_server_url,
    _load_env_file_or_exit,
    _parse_host_port,
    _validate_shared_options,
)
from hsync_client.constants import (
    HSYNC_VERSION,
    MASS_DELETE_GUARD_DEFAULT,
    MAX_INBOUND_SIZE_MB_DEFAULT,
    MAX_WAIT_TIMEOUT_DEFAULT,
    RENDEZVOUS_DEFAULT_PORT,
    SERVER_DEFAULT_PORT,
    SYNC_DIRECTION_DEFAULT,
)
from hsync_client.pinning import PinStore
from hsync_client.rendezvous_trust import RendezvousTrustStore
from hsync_client.server_trust import ServerTrustStore, fetch_server_fingerprint

# =============================================================================
# Typer Application
# =============================================================================

app = typer.Typer(
    name="hsync",
    help="HSYNC - HTTP-based file synchronization client",
    add_completion=False,
    rich_markup_mode="rich",  # Enable rich formatting for grouped options
)

# Default local path is resolved once at import time (B008-safe singleton) so
# every subcommand shares the same default without calling getcwd() per parse.
_DEFAULT_LOCAL_PATH = getcwd()


# =============================================================================
# Eager global callbacks (parent level)
# =============================================================================


def version_callback(value: bool) -> None:
    """Print version and exit."""
    if value:
        typer.echo(f"hsync {HSYNC_VERSION}")
        raise typer.Exit()


def legend_callback(value: bool) -> None:
    """Print the output-symbols legend and exit."""
    if value:
        from hsync_client.log_labels import legend_lines

        for line in legend_lines():
            typer.echo(line)
        raise typer.Exit()


def collect_subcommand_options(typer_app: typer.Typer) -> dict[str, list[str]]:
    """Map each registered subcommand to its supported long option flags.

    Introspects the click group built from ``typer_app`` so the advertised CLI
    surface can never drift from the real interface: every ``--flag`` an option
    declares (primary and secondary) is reported for its own subcommand.
    """
    options: dict[str, list[str]] = {}
    for name, command in typer.main.get_group(typer_app).commands.items():
        flags: set[str] = set()
        for param in command.params:
            if param.param_type_name != "option":
                continue
            for opt in (*param.opts, *param.secondary_opts):
                if opt.startswith("--"):
                    flags.add(opt)
        options[name] = sorted(flags)
    return dict(sorted(options.items()))


def capabilities_json_callback(_ctx: typer.Context, value: bool) -> None:
    """Print the binary's capabilities document as JSON and exit."""
    if not value:
        return
    from hsync_common.protocol.capabilities import build_capabilities

    # The parent context's command carries only the parent-level options: the
    # advertised surface is the per-subcommand one, so introspect each
    # registered subcommand's click command instead of ctx.command.params.
    options_by_subcommand = collect_subcommand_options(app)
    doc = build_capabilities(
        app_version=HSYNC_VERSION,
        subcommands=sorted(options_by_subcommand),
        cli_options_by_subcommand=options_by_subcommand,
    )
    typer.echo(json.dumps(doc, indent=2, sort_keys=True))
    raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool | None,
        typer.Option(
            "-V",
            "--version",
            help="Show version and exit.",
            is_eager=True,
            callback=version_callback,
        ),
    ] = None,
    capabilities_json: Annotated[
        bool | None,
        typer.Option(
            "--capabilities-json",
            help="Print the binary's capabilities document as JSON and exit.",
            is_eager=True,
            callback=capabilities_json_callback,
        ),
    ] = None,
    help_symbols: Annotated[
        bool,
        typer.Option(
            "--help-symbols",
            help="Show output symbols legend.",
            is_eager=True,
            callback=legend_callback,
        ),
    ] = False,
) -> None:
    """
    HSYNC - HTTP-based file synchronization client.

    Synchronize files between a local directory and a remote bucket. A bucket
    is a shared directory hosted on a central HSYNC server or on another peer
    in P2P Mode. In [bold]Server Mode[/bold], clients connect to a centralized server.
    In [bold]P2P Mode[/bold] (P2P), clients discover each other via mDNS and one
    automatically becomes the master—no central server required.

    [bold]Subcommands:[/bold]

        [cyan]server[/cyan]   Synchronize with a central HSYNC server (REST + WebSocket)
        [cyan]p2p[/cyan]      Synchronize peer-to-peer over the local network

    [bold]Global Options:[/bold]

        [cyan]-V, --version[/cyan]        Show version and exit
        [cyan]--capabilities-json[/cyan] Print the binary's capabilities document and exit
        [cyan]--help-symbols[/cyan]      Show the output-symbols legend

    [bold]Examples:[/bold]

        [dim]# Basic sync with a remote server (https:// and port 9753 are implied)[/dim]
        $ hsync server server.example.com -b photos

        [dim]# Sync a specific local directory[/dim]
        $ hsync server server.example.com -b documents -p ~/Documents

        [dim]# One-shot sync (single cycle, then exit)[/dim]
        $ hsync server server.example.com -b projects --once

        [dim]# Dry run to see what would be synced[/dim]
        $ hsync server server.example.com -b backup -n

        [dim]# P2P mode (no central server, peer-to-peer)[/dim]
        $ hsync p2p -b shared

        [dim]# P2P mode with authentication (user password)[/dim]
        $ HSYNC_P2P_PASSPHRASE=YOUR_SHARED_PASSWORD_AT_LEAST_12_CHARACTERS hsync p2p -b shared

    [bold]Environment Variables:[/bold]

        HSYNC_SERVER_URL          Full server URL (alternative to the HOSTNAME argument)
        HSYNC_API_KEY         API key for authentication
        HSYNC_P2P_PASSPHRASE  Shared password (>=12 chars, derived to the group key)
        HSYNC_P2P_KEY         Raw shared key (>=32 chars), used as-is

    [bold]Project:[/bold]

        https://github.com/albertsdk/hsync-client

    [bold]Authors:[/bold]

        Alberto Basso
    """


@app.command()
def p2p(
    bucket: BucketOpt,
    rendezvous_server: RendezvousArg = None,
    http_timeout: HttpTimeoutOpt = None,
    p2p_listen_port: ListenPortOpt = 0,
    peer_rendezvous_timeout: RendezvousTimeoutOpt = None,
    p2p_group: GroupOpt = None,
    prefer_master: PreferMasterOpt = False,
    never_master: NeverMasterOpt = False,
    p2p_trust: P2PTrustOpt = None,
    allow_unauthenticated_writes: AllowUnauthenticatedWritesOpt = False,
    allow_unauthenticated_p2p: AllowUnauthenticatedP2POpt = False,
    p2p_key_fd: P2PKeyFdOpt = None,
    p2p_passphrase_fd: P2PPassphraseFdOpt = None,
    map_data_port: UpnpOpt = False,
    no_mdns: NoMdnsOpt = False,
    insecure: InsecureOpt = False,
    ca_cert: CaCertOpt = None,
    rendezvous_server_fingerprint: RendezvousFingerprintOpt = None,
    rendezvous_server_tofu: RendezvousTofuOpt = False,
    rendezvous_server_trust: RendezvousTrustOpt = None,
    rendezvous_transport: RendezvousTransportOpt = None,
    events_json_fd: EventsJsonFdOpt = None,
    local_path: PathOpt = _DEFAULT_LOCAL_PATH,
    once: OnceOpt = False,
    resync: IntervalOpt = None,
    reconnect_delay: RetryDelayOpt = None,
    stability_delay: StabilityDelayOpt = None,
    max_wait_timeout: MaxWaitTimeoutOpt = MAX_WAIT_TIMEOUT_DEFAULT,
    filter_path: SubdirOpt = None,
    dryrun: DryRunOpt = False,
    deletion_policy: DeletionPolicyOpt = None,
    sync_direction: SyncDirectionOpt = SYNC_DIRECTION_DEFAULT,
    max_bandwidth_upload: LimitUploadOpt = None,
    max_bandwidth_download: LimitDownloadOpt = None,
    no_compression: NoCompressionOpt = False,
    no_delta: NoDeltaOpt = False,
    no_peer_download: NoPeerDownloadOpt = False,
    delta_min_file_size: DeltaMinFileSizeOpt = None,
    delta_min_file_size_p2p: DeltaMinFileSizeP2POpt = None,
    max_concurrent_transfers: MaxConcurrentTransfersOpt = None,
    no_autotune: NoAutotuneOpt = False,
    mass_delete_guard: MassDeleteGuardOpt = MASS_DELETE_GUARD_DEFAULT,
    mass_delete_threshold: MassDeleteThresholdOpt = None,
    mass_delete_percent: MassDeletePercentOpt = None,
    min_free_disk_space: MinFreeDiskSpaceOpt = None,
    max_inbound_size: MaxInboundSizeOpt = MAX_INBOUND_SIZE_MB_DEFAULT,
    ignore_patterns: IgnorePatternOpt = None,
    verbose: VerboseOpt = False,
    quiet: QuietOpt = False,
    logfile: LogFileOpt = None,
    color: ColorOpt = ColorMode.AUTO,
    allow_deletions: AllowDeletionsOpt = False,
    env_file: EnvFileOpt = None,
) -> None:
    """
    Synchronize a bucket over P2P peer-to-peer (P2P).

    Peers on the same local network discover each other via mDNS and one is
    elected master (an HTTPS/WSS transport hub). No central server is required.

    [bold]Examples:[/bold]

        [dim]# Basic P2P sync[/dim]
        $ hsync p2p -b shared

        [dim]# Prefer this peer as the master[/dim]
        $ hsync p2p -b shared --prefer-master

        [dim]# Isolate a sync group and authenticate it (shared password, 12+ chars)[/dim]
        $ HSYNC_P2P_PASSPHRASE=a-long-shared-password hsync p2p -b shared --group home

        [dim]# Advanced: a full raw group key instead of a password (32+ chars)[/dim]
        $ HSYNC_P2P_KEY=YOUR_SHARED_KEY_AT_LEAST_32_CHARACTERS hsync p2p -b shared --group home

        [dim]# Discover peers across networks through a discovery server[/dim]
        $ hsync p2p rdv.example.net -b shared

        [dim]# Rendezvous extra ports when your egress firewall blocks UDP 443[/dim]
        $ HSYNC_RENDEZVOUS_SERVER_EXTRA_PORTS=53 hsync p2p rdv.example.net -b shared

    [bold]Environment Variables:[/bold]

        HSYNC_P2P_PASSPHRASE  Shared password (>=12 chars, derived to the group key)
        HSYNC_P2P_KEY         Raw shared key (>=32 chars), used as-is
        HSYNC_P2P_TRUST       P2P master trust policy: ask | accept-new | strict
    """
    (
        parsed_bw_upload,
        parsed_bw_download,
        parsed_min_free_disk_space,
    ) = _validate_shared_options(locals())

    # P2P-only validation
    if peer_rendezvous_timeout is not None and peer_rendezvous_timeout < 0:
        raise typer.BadParameter(
            "Peer rendezvous timeout must be 0 or greater (0 = wait indefinitely)."
        )

    # --prefer-master is incompatible with directional sync (upload/download-only)
    if prefer_master and sync_direction != "bidirectional":
        raise typer.BadParameter(
            f"--prefer-master is incompatible with --sync-direction={sync_direction}. "
            f"The P2P master always operates in bidirectional mode."
        )

    # --prefer-master and --never-master are mutually exclusive (contradictory)
    if prefer_master and never_master:
        raise typer.BadParameter(
            "--prefer-master and --never-master are mutually exclusive: a client "
            "cannot both prefer the master role and be forbidden from it."
        )

    # Resolve the rendezvous-server endpoints from the CLI option, then the env
    # (loaded here so an --env-file value is visible before run_sync loads it
    # again later). One grammar on both surfaces.
    _load_env_file_or_exit(env_file)

    rendezvous_endpoints = _build_rendezvous_servers(rendezvous_server, use_env=True)
    if map_data_port:
        effective_map_data_port = True
    else:
        from hsync_client.config import get_map_data_port

        effective_map_data_port = get_map_data_port()

    # --no-mdns removes local-network discovery, so at least one discovery server
    # must remain to find peers: the flag is a per-command request, so a missing
    # server is a usage error. The HSYNC_MDNS env is a *persistent* setting, so a
    # misconfigured value there falls back to mDNS at the plan level
    # (``build_rendezvous_plan``) instead of breaking a daemon that starts on boot.
    from hsync_client.config import get_mdns_enabled

    effective_mdns = (not no_mdns) and get_mdns_enabled()
    if no_mdns and not rendezvous_endpoints:
        raise typer.BadParameter(
            "--no-mdns requires at least one --rendezvous-server endpoint: without "
            "a discovery server there would be no way to find peers."
        )

    # Rendezvous-server TLS trust (ADR-0034): --ca-cert is a server-mode knob, not
    # a rendezvous-channel knob, so refuse it rather than silently ignore it.
    if ca_cert:
        raise typer.BadParameter(
            "--ca-cert is not used for the rendezvous-server channel; pin the server "
            "with --rendezvous-server-fingerprint (or use --rendezvous-server-tofu) instead."
        )

    # Pins are per endpoint (``ENDPOINT=FINGERPRINT[|FINGERPRINT]``, repeatable;
    # a bare fingerprint is the unambiguous single-endpoint shorthand). The map
    # is bound to the configured endpoints here, so a pin for a server the client
    # never dials is a usage error rather than a silently ignored value.
    pins = _build_rendezvous_server_pins(rendezvous_server_fingerprint, rendezvous_endpoints)

    if insecure and (pins or rendezvous_server_tofu):
        raise typer.BadParameter(
            "--insecure is mutually exclusive with "
            "--rendezvous-server-fingerprint/--rendezvous-server-tofu."
        )

    if (pins or rendezvous_server_tofu) and not rendezvous_endpoints:
        raise typer.BadParameter(
            "--rendezvous-server-fingerprint/--rendezvous-server-tofu require at least "
            "one --rendezvous-server endpoint."
        )

    if rendezvous_transport is not None:
        from hsync_common.constants import RDV_TRANSPORTS

        if rendezvous_transport not in RDV_TRANSPORTS:
            allowed = ", ".join(RDV_TRANSPORTS)
            raise typer.BadParameter(
                f"invalid --rendezvous-transport value: {rendezvous_transport!r}; "
                f"use one of: {allowed}"
            )

    if p2p_trust is not None:
        try:
            resolve_self_signed_policy(p2p_trust)
        except ValueError as exc:
            raise typer.BadParameter(f"invalid --p2p-trust value: {exc}") from exc

    from hsync_client.main_logic import run_sync

    run_sync(
        _build_run_options(
            locals(),
            server_url=None,
            api_key_fd=None,
            p2p_key_fd=p2p_key_fd,
            p2p_passphrase_fd=p2p_passphrase_fd,
            p2p_mode=True,
            p2p_listen_port=p2p_listen_port,
            rendezvous_server_endpoints=rendezvous_endpoints,
            map_data_port=effective_map_data_port,
            rendezvous_server_pins=pins,
            rendezvous_server_tofu=bool(rendezvous_server_tofu),
            rendezvous_server_trust=rendezvous_server_trust,
            rendezvous_transport=rendezvous_transport,
            peer_rendezvous_timeout=peer_rendezvous_timeout,
            p2p_group=p2p_group,
            prefer_master=prefer_master,
            never_master=never_master,
            p2p_trust=p2p_trust,
            p2p_peer_download_enabled=not no_peer_download,
            mdns_enabled=effective_mdns,
            p2p_allow_unauthenticated_writes=allow_unauthenticated_writes,
            p2p_allow_unauthenticated=allow_unauthenticated_p2p,
        )
    )


@app.command("derive-group-key")
def derive_group_key_command(
    passphrase_fd: Annotated[
        int | None,
        typer.Option(
            "--passphrase-fd",
            help="Read the shared password from the given file descriptor.",
            metavar="FD",
            hidden=True,
        ),
    ] = None,
) -> None:
    """Print the P2P group key (64 hex) derived from a shared password.

    Use the output to populate a rendezvous-server key file. Treat it as a secret: it is
    the raw group key the discovery server derives bucket tokens from.
    """
    import os

    from hsync_common.protocol.rdv_tokens import (
        WeakGroupPassphrase,
        derive_group_key,
        validate_group_passphrase,
    )

    if passphrase_fd is not None:
        with os.fdopen(passphrase_fd, "r") as handle:
            passphrase = handle.read()
    else:
        passphrase = typer.prompt("Shared password", hide_input=True)

    try:
        validate_group_passphrase(passphrase)
    except WeakGroupPassphrase as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None

    typer.echo(derive_group_key(passphrase))
    typer.echo(
        "The value above is the group key secret: store it where only the service can read it.",
        err=True,
    )


# =============================================================================
# trust management (P2P peers, discovery servers, sync server) — one ``hsync trust``
# namespace
# =============================================================================
# ``hsync trust …`` mirrors OpenSSH's known-hosts workflow so an operator can
# inspect, pre-pin and forget fingerprints without editing the JSON stores by
# hand. One namespace covers the three certificate-pinning transports: the P2P
# master/seeders (``known_peers``), the discovery servers and the sync server
# (``known_hosts``). ``hsync server``/``hsync p2p`` stay actions, so the trust
# surface lives here rather than under them (ADR-0036).
_trust_root = typer.Typer(
    help="Inspect and manage certificate trust for the P2P peers, discovery servers and server.",
    no_args_is_help=True,
)
_trust_rendezvous_app = typer.Typer(
    help="Manage the rendezvous-server known-hosts store.",
    no_args_is_help=True,
)
_trust_peer_app = typer.Typer(
    help="Manage the P2P known-peers store.",
    no_args_is_help=True,
)
_trust_server_app = typer.Typer(
    help="Manage the sync-server known-hosts store.",
    no_args_is_help=True,
)
app.add_typer(_trust_root, name="trust")
_trust_root.add_typer(_trust_rendezvous_app, name="rendezvous-server")
_trust_root.add_typer(_trust_peer_app, name="peer")
_trust_root.add_typer(_trust_server_app, name="server")


_TrustStore = RendezvousTrustStore | ServerTrustStore


def _trust_list(store: _TrustStore, none_message: str, as_json: bool) -> None:
    """Print a known-hosts store, as text or JSON."""
    entries = store.entries()
    if as_json:
        typer.echo(json.dumps(entries, indent=2, sort_keys=True))
        return
    if not entries:
        typer.echo(none_message)
        return
    for entry in entries:
        typer.echo(
            f"{format_authority(entry['host'], entry['port'])} {entry['fingerprint']} "
            f"(source={entry['source']}, first_seen={entry['first_seen']})"
        )


def _trust_show(store: _TrustStore, host: str, port: int) -> None:
    """Print one pinned fingerprint or exit non-zero when absent."""
    fingerprint = store.get(host, port)
    if fingerprint is None:
        typer.echo(f"{format_authority(host, port)} is not in the known-hosts store.", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"{format_authority(host, port)} {fingerprint}")


def _trust_add(store: _TrustStore, host: str, port: int, fingerprint: str) -> None:
    """Validate and pin a fingerprint."""
    normalized = parse_fingerprint(fingerprint)
    if normalized is None:
        typer.echo("Not a valid sha256 fingerprint (expected 64 hex digits).", err=True)
        raise typer.Exit(code=1)
    store.set(host, port, normalized, source="cli")
    typer.echo(f"Pinned {format_authority(host, port)} {normalized} in {store.path}.")


def _trust_remove(store: _TrustStore, host: str, port: int) -> None:
    """Forget a pinned authority, reporting whether it was present."""
    if store.remove(host, port):
        typer.echo(f"Removed {format_authority(host, port)} from the known-hosts store.")
    else:
        typer.echo(f"{format_authority(host, port)} was not in the known-hosts store.")


def _trust_host_port(hostname: str | None) -> tuple[str, int]:
    """Resolve the ``HOST[:PORT]`` argument of a trust subcommand.

    The port is part of the endpoint (no ``--port`` flag): an entry without one
    uses the channel default so a pin keyed ``host`` and one keyed ``host:443``
    name the same node.
    """
    if hostname is None:
        raise typer.BadParameter("a rendezvous-server host[:port] is required")
    endpoint = _parse_host_port(hostname)
    return endpoint.host, endpoint.port if endpoint.port is not None else RENDEZVOUS_DEFAULT_PORT


@_trust_rendezvous_app.command("list")
def rendezvous_trust_list(
    as_json: bool = typer.Option(False, "--json", help="Emit entries as JSON."),
) -> None:
    """List the pinned rendezvous-server fingerprints (like ``known_hosts``)."""
    _trust_list(RendezvousTrustStore(), "No discovery servers are trusted yet.", as_json)


@_trust_rendezvous_app.command("show")
def rendezvous_trust_show(hostname: HostArg) -> None:
    """Show the pinned fingerprint for one discovery server."""
    host, resolved_port = _trust_host_port(hostname)
    _trust_show(RendezvousTrustStore(), host, resolved_port)


@_trust_rendezvous_app.command("add")
def rendezvous_trust_add(
    hostname: HostArg,
    fingerprint: str = typer.Argument(..., metavar="FINGERPRINT"),
) -> None:
    """Pin a rendezvous-server fingerprint (verify it out of band first)."""
    host, resolved_port = _trust_host_port(hostname)
    _trust_add(RendezvousTrustStore(), host, resolved_port, fingerprint)


@_trust_rendezvous_app.command("remove")
def rendezvous_trust_remove(hostname: HostArg) -> None:
    """Forget a pinned discovery server (like ``ssh-keygen -R``)."""
    host, resolved_port = _trust_host_port(hostname)
    _trust_remove(RendezvousTrustStore(), host, resolved_port)


@_trust_rendezvous_app.command("fingerprint")
def rendezvous_trust_fingerprint(hostname: HostArg) -> None:
    """Print a discovery server's certificate fingerprint without trusting it."""
    from hsync_client.config import get_rendezvous_transport
    from hsync_client.rdv_client import RendezvousClient, RendezvousError

    host, resolved_port = _trust_host_port(hostname)
    client = RendezvousClient(
        host, resolved_port, policy=TrustPolicy.none, transport=get_rendezvous_transport()
    )
    try:
        observed = client.fetch_fingerprint()
    except RendezvousError as exc:
        typer.echo(f"Could not read the fingerprint: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(observed)


# =============================================================================
# peer trust (known_peers) management
# =============================================================================

# ``hsync trust peer …`` manages the P2P known-peers store
# (``p2p_known_peers.json``), keyed ``cid|bucket``.


@_trust_peer_app.command("list")
def peer_trust_list(
    as_json: bool = typer.Option(False, "--json", help="Emit entries as JSON."),
) -> None:
    """List the pinned P2P peer fingerprints (like ``known_hosts``)."""
    entries = PinStore().entries()
    if as_json:
        typer.echo(json.dumps(entries, indent=2, sort_keys=True))
        return
    if not entries:
        typer.echo("No peers are trusted yet.")
        return
    for entry in entries:
        typer.echo(f"{entry['cid']}|{entry['bucket']} {entry['fingerprint']}")


@_trust_peer_app.command("add")
def peer_trust_add(
    bucket: BucketOpt,
    cid: str = typer.Argument(..., metavar="CID"),
    fingerprint: str = typer.Argument(..., metavar="FINGERPRINT"),
) -> None:
    """Pin a P2P peer fingerprint (verify it out of band first)."""
    normalized = parse_fingerprint(fingerprint)
    if normalized is None:
        typer.echo("Not a valid sha256 fingerprint (expected 64 hex digits).", err=True)
        raise typer.Exit(code=1)
    store = PinStore()
    store.set(cid, bucket, normalized)
    typer.echo(f"Pinned {cid}|{bucket} {normalized} in {store.path}.")


@_trust_peer_app.command("remove")
def peer_trust_remove(
    bucket: BucketOpt,
    cid: str = typer.Argument(..., metavar="CID"),
) -> None:
    """Forget a pinned P2P peer (like ``ssh-keygen -R``)."""
    if PinStore().remove(cid, bucket):
        typer.echo(f"Removed {cid}|{bucket} from the known-peers store.")
    else:
        typer.echo(f"{cid}|{bucket} was not in the known-peers store.")


# =============================================================================
# server trust (known_hosts) management
# =============================================================================


def _server_trust_host_port(hostname: str | None) -> tuple[str, int]:
    """Resolve the ``HOST[:PORT]`` argument of a server trust subcommand."""
    if hostname is None:
        raise typer.BadParameter("a server host[:port] is required")
    hp = _parse_host_port(hostname)
    return hp.host, SERVER_DEFAULT_PORT if hp.port is None else hp.port


@_trust_server_app.command("list")
def server_trust_list(
    as_json: bool = typer.Option(False, "--json", help="Emit entries as JSON."),
) -> None:
    """List the pinned sync-server fingerprints (like ``known_hosts``)."""
    _trust_list(ServerTrustStore(), "No servers are trusted yet.", as_json)


@_trust_server_app.command("show")
def server_trust_show(hostname: HostArg) -> None:
    """Show the pinned fingerprint for one server."""
    host, resolved_port = _server_trust_host_port(hostname)
    _trust_show(ServerTrustStore(), host, resolved_port)


@_trust_server_app.command("add")
def server_trust_add(
    hostname: HostArg,
    fingerprint: str = typer.Argument(..., metavar="FINGERPRINT"),
) -> None:
    """Pin a sync-server fingerprint (verify it out of band first)."""
    host, resolved_port = _server_trust_host_port(hostname)
    _trust_add(ServerTrustStore(), host, resolved_port, fingerprint)


@_trust_server_app.command("remove")
def server_trust_remove(hostname: HostArg) -> None:
    """Forget a pinned sync server (like ``ssh-keygen -R``)."""
    host, resolved_port = _server_trust_host_port(hostname)
    _trust_remove(ServerTrustStore(), host, resolved_port)


@_trust_server_app.command("fingerprint")
def server_trust_fingerprint(hostname: HostArg) -> None:
    """Print a server's certificate fingerprint without trusting it."""
    host, resolved_port = _server_trust_host_port(hostname)
    observed = fetch_server_fingerprint(host, resolved_port)
    if observed is None:
        typer.echo(
            f"Could not read a certificate fingerprint from {format_authority(host, resolved_port)}.",
            err=True,
        )
        raise typer.Exit(code=1)
    typer.echo(observed)


@app.command()
def server(
    bucket: BucketOpt,
    server_url: ServerUrlOpt = None,
    insecure: InsecureOpt = False,
    ca_cert: CaCertOpt = None,
    server_fingerprint: ServerFingerprintOpt = None,
    server_tofu: ServerTofuOpt = False,
    server_trust: ServerTrustOpt = None,
    http_timeout: HttpTimeoutOpt = None,
    api_key_fd: ApiKeyFdOpt = None,
    events_json_fd: EventsJsonFdOpt = None,
    local_path: PathOpt = _DEFAULT_LOCAL_PATH,
    once: OnceOpt = False,
    resync: IntervalOpt = None,
    reconnect_delay: RetryDelayOpt = None,
    stability_delay: StabilityDelayOpt = None,
    max_wait_timeout: MaxWaitTimeoutOpt = MAX_WAIT_TIMEOUT_DEFAULT,
    filter_path: SubdirOpt = None,
    dryrun: DryRunOpt = False,
    deletion_policy: DeletionPolicyOpt = None,
    sync_direction: SyncDirectionOpt = SYNC_DIRECTION_DEFAULT,
    max_bandwidth_upload: LimitUploadOpt = None,
    max_bandwidth_download: LimitDownloadOpt = None,
    no_compression: NoCompressionOpt = False,
    no_delta: NoDeltaOpt = False,
    delta_min_file_size: DeltaMinFileSizeOpt = None,
    delta_min_file_size_p2p: DeltaMinFileSizeP2POpt = None,
    max_concurrent_transfers: MaxConcurrentTransfersOpt = None,
    no_autotune: NoAutotuneOpt = False,
    mass_delete_guard: MassDeleteGuardOpt = MASS_DELETE_GUARD_DEFAULT,
    mass_delete_threshold: MassDeleteThresholdOpt = None,
    mass_delete_percent: MassDeletePercentOpt = None,
    min_free_disk_space: MinFreeDiskSpaceOpt = None,
    max_inbound_size: MaxInboundSizeOpt = MAX_INBOUND_SIZE_MB_DEFAULT,
    ignore_patterns: IgnorePatternOpt = None,
    verbose: VerboseOpt = False,
    quiet: QuietOpt = False,
    logfile: LogFileOpt = None,
    color: ColorOpt = ColorMode.AUTO,
    allow_deletions: AllowDeletionsOpt = False,
    env_file: EnvFileOpt = None,
) -> None:
    """
    Synchronize a bucket with a central HSYNC server (REST + WebSocket).

    The server hosts the shared bucket and coordinates synchronization between
    all connected clients. ``--server-url`` accepts a full ``https://`` URL
    (optionally with a reverse-proxy path) or a bare ``HOST[:PORT]``, which
    implies ``https://`` on the default port (9753, the hsync-server default).
    The port is always carried by the endpoint: there is no separate ``--port``.

    [bold]Examples:[/bold]

        [dim]# Basic sync[/dim]
        $ hsync server --server-url server.example.com -b photos

        [dim]# Continuous sync (the default: keeps retrying if the server is unreachable)[/dim]
        $ hsync server --server-url server.example.com -b projects

        [dim]# One-shot sync[/dim]
        $ hsync server --server-url server.example.com -b projects --once

        [dim]# Reverse proxy on the standard HTTPS port, behind a path[/dim]
        $ hsync server --server-url https://server.example.com/hsync -b photos
    """
    # Bare-endpoint or full URL: normalize to the https:// URL the sync core
    # consumes. None → the documented HSYNC_SERVER_URL env fallback.
    resolved_url = _build_server_url(server_url)

    # The server URL is mandatory in server mode: when it is omitted, the full
    # URL must come from HSYNC_SERVER_URL (shell env or --env-file). Resolve it
    # here so a missing server fails fast with a clean usage error instead of
    # deep inside run_sync, after the banner and logging are already up.
    if resolved_url is None:
        _load_env_file_or_exit(env_file)
        from hsync_client.config import get_server_url

        resolved_url = get_server_url()
        if not resolved_url:
            raise typer.BadParameter(
                "Missing sync-server URL: pass --server-url (e.g. "
                "'hsync server --server-url server.example.com') or set the "
                "HSYNC_SERVER_URL environment variable."
            )

    (
        parsed_bw_upload,
        parsed_bw_download,
        parsed_min_free_disk_space,
    ) = _validate_shared_options(locals())

    # Server TLS trust (ADR-0036): validate the pin flags before running.
    server_fingerprints = tuple(server_fingerprint or ())
    if len(server_fingerprints) > 2:
        raise typer.BadParameter(
            "at most two --server-fingerprint values are allowed (rotation window)."
        )
    for fp in server_fingerprints:
        if parse_fingerprint(fp) is None:
            raise typer.BadParameter(f"invalid --server-fingerprint value: {fp!r}")
    if insecure and (server_fingerprints or server_tofu):
        raise typer.BadParameter(
            "--insecure is mutually exclusive with --server-fingerprint/--server-tofu."
        )

    from hsync_client.main_logic import run_sync

    run_sync(
        _build_run_options(
            locals(),
            server_url=resolved_url,
            api_key_fd=api_key_fd,
            p2p_key_fd=None,
            server_fingerprints=server_fingerprints,
            server_tofu=bool(server_tofu),
            server_trust=server_trust,
            p2p_mode=False,
            p2p_listen_port=0,
            peer_rendezvous_timeout=None,
            p2p_group=None,
            prefer_master=False,
            never_master=False,
            p2p_trust=None,
            p2p_peer_download_enabled=False,
            p2p_allow_unauthenticated_writes=False,
        )
    )


# =============================================================================
# state management — ``hsync state reset``
# =============================================================================

# The per-folder state DB (merge bases + deletion history) is client-owned and
# guarded by the per-directory ``InstanceLock``, so forgetting it belongs here
# rather than in a front-end that cannot see a concurrent sync.
_state_root = typer.Typer(
    help="Inspect and reset the local sync-state database.",
    no_args_is_help=True,
)
app.add_typer(_state_root, name="state")


@_state_root.command("reset")
def state_reset(
    folder: Annotated[
        str,
        typer.Option(
            "--folder",
            help="Synced folder whose local state (merge bases and deletion history) is forgotten.",
            metavar="DIR",
        ),
    ],
    as_json: bool = typer.Option(False, "--json", help="Emit the summary as JSON."),
    quarantine: bool = typer.Option(
        False,
        "--quarantine",
        help="Move an unreadable state DB aside instead of failing.",
    ),
) -> None:
    """Forget one folder's sync state so the next cycle reconciles from scratch.

    Clears the merge bases (version store) and the deletion markers
    (tombstones) of the folder. No synced file is touched and nothing is
    transferred: the next sync re-scans every file and resolves the surviving
    differences with the causal rules. Refuses while any hsync process is
    syncing the same folder.
    """
    # Lazy imports keep the CLI fast paths free of the state/engine modules.
    import sqlite3
    from dataclasses import asdict
    from pathlib import Path

    from hsync_client.exitcodes import ExitCode
    from hsync_client.identity import InstanceLockError
    from hsync_client.logger import setup_logging
    from hsync_client.state_reset import reset_folder_state

    # A one-shot maintenance command reports on stdout only: the default loguru
    # sink would echo the lock's DEBUG lines on stderr.
    setup_logging(quiet=True)

    if not folder.strip():
        typer.echo("--folder requires a directory path.", err=True)
        raise typer.Exit(code=int(ExitCode.FATAL))
    target = Path(folder).expanduser()
    if not target.is_dir():
        typer.echo(f"Not a directory: {target}", err=True)
        raise typer.Exit(code=int(ExitCode.FATAL))

    try:
        report = reset_folder_state(target, quarantine=quarantine)
    except InstanceLockError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=int(ExitCode.FATAL)) from None
    except sqlite3.DatabaseError as exc:
        typer.echo(
            f"Could not reset the state DB: {exc}. Re-run with --quarantine to move it aside.",
            err=True,
        )
        raise typer.Exit(code=int(ExitCode.FATAL)) from None
    except OSError as exc:
        typer.echo(f"Could not reset the state DB: {exc}", err=True)
        raise typer.Exit(code=int(ExitCode.FATAL)) from None

    if as_json:
        typer.echo(json.dumps(asdict(report), indent=2, sort_keys=True))
        return
    if not report.found:
        typer.echo(f"No sync state found for {report.folder}; nothing to reset.")
        return
    if report.quarantined:
        typer.echo(
            f"The state DB for {report.folder} was unreadable; moved to "
            f"{report.quarantined}. The next sync starts from scratch."
        )
        return
    typer.echo(
        f"Reset the sync state of {report.folder}: {report.versions_deleted} version entries "
        f"and {report.tombstones_deleted} deletion markers removed. "
        "The next sync will scan every file."
    )


# What Typer's runner exits with after an interruption (``typer/core.py::main``
# converts ``KeyboardInterrupt``/``EOFError`` into ``Exit(130)``).
_INTERRUPTED_EXIT_CODE = 130


def _raise_keyboard_interrupt(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


def _install_sigterm_handler() -> None:
    """Turn ``SIGTERM`` into ``KeyboardInterrupt`` so a stop shuts down cleanly.

    The default action kills the process on the spot and skips every ``finally``:
    the P2P/server tasks are never stopped, the last records stay in the
    asynchronous log queue, and the sinks keep the semaphores the resource
    tracker then reports as leaked. Raising ``KeyboardInterrupt`` in the main
    thread reuses the ``SIGINT`` path (``run_p2p_mode`` catches it, stops the
    servers, flushes), which is what ``systemctl stop``, ``docker stop`` and the
    desktop's Stop button (``SIGTERM``, then ``SIGKILL`` after 5 s) expect.
    """
    # ``signal.signal`` is main-thread only; embedded callers (tests, the
    # desktop's own process) simply keep the default action.
    if threading.current_thread() is not threading.main_thread():
        return
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)


def run() -> None:
    """Entry point for the CLI application."""
    _install_sigterm_handler()
    try:
        app()
    except KeyboardInterrupt:
        # An operator stop is not a failure: every mode logs its own shutdown and
        # the process leaves with status 0, without a traceback.
        pass
    except SystemExit as exc:
        # Typer answers an interruption with ``Exit(_INTERRUPTED_EXIT_CODE)``
        # before the exception reaches a handler (``typer/core.py::main``), so a
        # SIGTERM in a mode that does not catch ``KeyboardInterrupt`` itself
        # surfaces here: a stop must be a clean exit, not a failed one.
        if exc.code != _INTERRUPTED_EXIT_CODE:
            raise
    finally:
        # Process-level safety net for the asynchronous log sinks: whatever
        # exit path was taken (normal, SystemExit, unhandled error, SIGTERM),
        # the queued records reach the terminal and the sinks are closed before
        # the interpreter tears the stream down — closing them is also what
        # releases the semaphores ``resource_tracker`` would otherwise report.
        #
        # Drained only when the logging stack was actually initialised: the same
        # entry point serves the fast paths (``--version`` / ``--help``), whose
        # contract is that importing ``hsync_client.cli`` pulls none of the heavy
        # dependencies (pinned by ``tests/test_startup_imports.py``), so the
        # import stays lazy and conditional.
        if "hsync_client.logger" in sys.modules:
            from hsync_client.logger import shutdown_logging

            shutdown_logging()
