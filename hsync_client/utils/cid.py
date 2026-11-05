# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Client ID generation and CID log formatting."""

from hsync_common.ids import CID_LENGTH, mint_cid


def get_cid(length: int = CID_LENGTH) -> str:
    """Generate a cryptographically secure client ID (NanoID-style).

    Delegates to the shared :func:`hsync_common.models.event.mint_cid`, so the
    21-character alphanumeric shape (``CID_PATTERN``) and the unbiased
    ``secrets.choice`` over the 62-character alphabet have a single definition.
    """
    return mint_cid(length)


def cid_suffix(cid: object) -> str:
    """Return a ``" (CID: <cid>)"`` log suffix, or ``""`` when *cid* is falsy."""
    return f" (CID: {cid})" if cid else ""
