# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Client ID generation and CID log formatting."""

import secrets

from hsync_client.constants import CID_ALPHABET, CID_LENGTH


def get_cid(length: int = CID_LENGTH) -> str:
    """
    Generate a cryptographically secure client ID (NanoID-style).

    Creates a unique identifier for this client session used to
    identify the source of events and prevent echo. Uses the secrets
    module for cryptographic security.

    The default configuration provides ~125 bits of entropy:
    - 21 characters from a strictly alphanumeric 62-character alphabet
    - Probability of collision: ~1 in 2^125

    The alphabet is strictly alphanumeric (`A-Za-z0-9`, matching the
    ``hsync_common.models.event.CID_PATTERN`` wire contract), so a generated
    CID always starts with a letter or digit — never ``-`` or ``_``.

    Args:
        length: Length of the ID string (default: CID_LENGTH = 21).

    Returns:
        Cryptographically secure random alphanumeric string.
    """
    return "".join(secrets.choice(CID_ALPHABET) for _ in range(length))


def cid_suffix(cid: object) -> str:
    """Return a ``" (CID: <cid>)"`` log suffix, or ``""`` when *cid* is falsy."""
    return f" (CID: {cid})" if cid else ""
