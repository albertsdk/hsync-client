# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Process exit codes for one-shot sync runs.

One-shot runs are HSYNC's primary interface for automation: cron jobs,
CI/CD pipelines, and shell scripts derive their verdict from the process
exit status, never by scraping log output. The codes below are part of
the public contract and are documented in ``docs/scheduling.md``.
"""

from enum import IntEnum


class ExitCode(IntEnum):
    """Exit codes returned by ``hsync`` in one-shot runs.

    The numeric values are stable API for scripts. The priority order in
    which a completed cycle may surface more than one condition is fixed
    in :func:`exit_code_for_result`.
    """

    #: Sync completed without anomalies (or a successful dry-run preview).
    OK = 0
    #: Fatal error: invalid configuration, unreachable server, authentication
    #: failure, directory lock held by another process, or an unexpected
    #: exception. No sync verdict could be produced.
    FATAL = 1
    #: The cycle completed but some transfers failed. Re-runnable: the next
    #: run retries only the missing files.
    TRANSFER_ERRORS = 2
    #: The mass-deletion guard tripped: some unconfirmed deletions were
    #: skipped this cycle. Operator review required before they can proceed.
    DELETION_GUARD = 3
    #: Causal conflicts were resolved by preserving ``*_conflicted_copy_*``
    #: files. Requires operator attention.
    CONFLICTS = 4
    #: P2P one-shot: no peer/master was found within the rendezvous timeout;
    #: nothing was synchronized.
    P2P_NO_PEER = 5


def exit_code_for_result(
    *,
    failed_transfers: int,
    guard_tripped: bool,
    conflicts: int,
) -> int:
    """Derive the process exit code for a completed sync cycle.

    Priority deliberately follows severity for a script operator: transfer
    errors (2) outrank the deletion guard (3), which outranks conflicts (4).
    A clean cycle with none of the above yields OK (0).
    """
    if failed_transfers > 0:
        return int(ExitCode.TRANSFER_ERRORS)
    if guard_tripped:
        return int(ExitCode.DELETION_GUARD)
    if conflicts > 0:
        return int(ExitCode.CONFLICTS)
    return int(ExitCode.OK)
