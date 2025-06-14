# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests for the LAN (peer-to-peer) sync use case (stretch).

LAN mode requires two real client instances discovering each other via
mDNS/zeroconf on the local network, electing a master, and syncing over
HTTPS/WSS with TOFU certificate pinning. This is inherently environment-
sensitive (multicast on loopback, self-signed cert generation, election
timing) and is therefore **disabled by default** to keep the suite
deterministic.

Set ``HSYNC_BB_LAN=1`` to opt in when the host supports loopback mDNS.
The scenarios below document the intended behavioural coverage.
"""

import os

import pytest

_LAN_ENABLED = os.environ.get("HSYNC_BB_LAN") == "1"
_LAN_REASON = (
    "LAN e2e needs loopback mDNS + two live client instances; set HSYNC_BB_LAN=1 to enable"
)

pytestmark = pytest.mark.skipif(not _LAN_ENABLED, reason=_LAN_REASON)


class TestLanElection:
    def test_force_master_wins_and_slave_syncs(self):
        # Given two LAN clients where one is pinned --force-master
        # When they discover each other and elect
        # Then the force-master becomes master and the slave syncs to it
        pytest.skip("LAN e2e harness not implemented in the default suite")


class TestLanTofuPinning:
    def test_first_sight_trusts_and_syncs(self):
        # Given a slave connecting to a master advertising a cert fingerprint
        # When it is the first sight (TOFU)
        # Then the pin is recorded and sync proceeds
        pytest.skip("LAN e2e harness not implemented in the default suite")

    def test_changed_fingerprint_is_refused(self):
        # Given a master whose fingerprint changed after first trust
        # When the slave reconnects
        # Then the connection is refused (HTTPS-only, possible MITM)
        pytest.skip("LAN e2e harness not implemented in the default suite")


class TestLanHttpsOnly:
    def test_master_without_cert_fingerprint_refused(self):
        # Given a master that does not advertise a cert fingerprint
        # When a slave considers connecting
        # Then it refuses (no plaintext HTTP fallback)
        pytest.skip("LAN e2e harness not implemented in the default suite")
