# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Black-box tests for the LAN WebSocket relay and handshake use case.

Drives the *real* LAN master's public WebSocket surface (port ``lan_port + 1``)
for the handshake negatives (protocol mismatch, bad token) — mirroring
``hsync-server/tests/test_bb_websocket_relay.py`` — and drives real peer
subprocesses for the observable relay semantics (echo suppression, fan-out that
survives a subscriber disconnect).

Requires HSYNC_BB_LAN=1 to run.
"""

import asyncio
import json
import os
import ssl
from pathlib import Path

import pytest
from hsync_common.protocol.handshake import PROTOCOL_VERSION_LAN, build_lan_auth
from websockets.exceptions import ConnectionClosed

from .lan_helpers import (
    _lan_auth_headers,
    _lan_cid,
    _lan_serving_master,
    _lan_start,
    _lan_ws_url,
    _terminate,
    _wait_for_file,
    _wait_for_slave_events,
    _wait_for_text_in_stderr,
)

_LAN_ENABLED = os.environ.get("HSYNC_BB_LAN") == "1"
_LAN_REASON = (
    "LAN e2e needs loopback mDNS + two live client instances; set HSYNC_BB_LAN=1 to enable"
)

pytestmark = pytest.mark.skipif(not _LAN_ENABLED, reason=_LAN_REASON)


def _lan_ws_ssl_ctx() -> ssl.SSLContext:
    """An SSL context that accepts the LAN master's self-signed cert."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _lan_token() -> str:
    """The shared LAN key a real peer sends as the auth token."""
    return _lan_auth_headers()["Authorization"][len("Bearer ") :]


async def _recv_auth_reply(ws, timeout: float = 10.0) -> dict:
    """Receive the next auth reply frame (the greeting is consumed first)."""
    return json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))


class TestLanWsHandshakeNegative:
    async def test_protocol_mismatch_rejected(self, lan_master_dir, lan_slave_dir, lan_bucket):
        # Given a serving LAN master (auth required: a LAN key is set)
        import websockets

        with _lan_serving_master(lan_master_dir, lan_slave_dir, lan_bucket) as lan_port:
            # When authenticating with an incompatible protocol version
            ws = await websockets.connect(
                _lan_ws_url(lan_port), ssl=_lan_ws_ssl_ctx(), max_size=None
            )
            try:
                # Consume the master's auth_required greeting first
                greeting = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                assert greeting["type"] == "auth_required"
                assert greeting["protocol_version"] == PROTOCOL_VERSION_LAN
                frame = build_lan_auth(token=_lan_token(), cid=_lan_cid("proto"), capabilities=[])
                frame["protocol_version"] = "lan/999"
                try:
                    await ws.send(json.dumps(frame))
                except ConnectionClosed:
                    pass

                # Then the master replies with an auth failure
                reply = await _recv_auth_reply(ws)
                assert reply["type"] == "auth"
                assert reply["status"] == "failed"
                assert reply["reason"] == "protocol_incompatible"
            finally:
                await ws.close()

    async def test_bad_token_rejected(self, lan_master_dir, lan_slave_dir, lan_bucket):
        # Given a serving LAN master protected by a LAN key
        import websockets

        with _lan_serving_master(lan_master_dir, lan_slave_dir, lan_bucket) as lan_port:
            # When authenticating with a wrong token (valid protocol version)
            ws = await websockets.connect(
                _lan_ws_url(lan_port), ssl=_lan_ws_ssl_ctx(), max_size=None
            )
            try:
                # Consume the greeting, then send an auth frame with a wrong token
                greeting = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                assert greeting["type"] == "auth_required"
                frame = build_lan_auth(
                    token="wrong-lan-key", cid=_lan_cid("badtok"), capabilities=[]
                )
                try:
                    await ws.send(json.dumps(frame))
                except ConnectionClosed:
                    pass

                # Then the master rejects the token
                reply = await _recv_auth_reply(ws)
                assert reply["type"] == "auth"
                assert reply["status"] == "failed"
                assert reply["reason"] == "invalid_token"
            finally:
                await ws.close()


class TestLanWsRelay:
    def test_upload_no_self_echo_and_fan_out(
        self, lan_master_dir, lan_slave_dir, tmp_path, lan_bucket
    ):
        """A file authored by slave1 is fanned out to slave2, and slave1 never
        receives its own event back as a duplicate (echo suppression)."""
        slave2_dir = str(tmp_path / "slave2")
        Path(slave2_dir).mkdir()

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            slave1 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            slave2 = _lan_start(slave2_dir, lan_bucket, never_master=True)
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=60), "master not ready"
            try:
                assert _wait_for_slave_events(slave1), "slave1 not listening for events"
                assert _wait_for_slave_events(slave2), "slave2 not listening for events"

                # When slave1 authors a file
                content = b"from-slave1\n"
                (Path(lan_slave_dir) / "relay.txt").write_bytes(content)

                # Then slave2 receives it (fan-out)...
                assert _wait_for_file(str(Path(slave2_dir) / "relay.txt"), timeout=30), (
                    "slave2 did not receive the fanned-out file"
                )
                assert (Path(slave2_dir) / "relay.txt").read_bytes() == content

                # ...and slave1 has exactly its own copy over a quiet window: a
                # delayed self-echo would create a duplicate/conflict copy (echo
                # suppression must prevent that). Sample, wait, re-sample so a
                # late duplicate is not missed.
                import time

                def _relay_duplicates() -> list:
                    return [
                        p
                        for p in Path(lan_slave_dir).iterdir()
                        if "relay" in p.name and p.name != "relay.txt"
                    ]

                _ = _relay_duplicates()
                time.sleep(5)
                relay_copies = _relay_duplicates()
                assert relay_copies == [], f"echo produced a duplicate on slave1: {relay_copies}"
            finally:
                _terminate(slave1)
                _terminate(slave2)
        finally:
            _terminate(master)

    def test_fan_out_survives_subscriber_disconnect(
        self, lan_master_dir, lan_slave_dir, tmp_path, lan_bucket
    ):
        """When one subscriber disconnects during broadcast, the other still
        receives the event — the faulty connection must never abort delivery."""
        slave2_dir = str(tmp_path / "slave2")
        Path(slave2_dir).mkdir()

        master = _lan_start(lan_master_dir, lan_bucket, force_master=True)
        try:
            slave1 = _lan_start(lan_slave_dir, lan_bucket, never_master=True)
            slave2 = _lan_start(slave2_dir, lan_bucket, never_master=True)
            assert _wait_for_text_in_stderr(master, "MASTER", timeout=60), "master not ready"
            try:
                assert _wait_for_slave_events(slave1), "slave1 not listening for events"
                assert _wait_for_slave_events(slave2), "slave2 not listening for events"

                # Subscriber slave2 disconnects cleanly before the event is sent
                _terminate(slave2)

                # When the master authors a file
                content = b"fan-out-survives\n"
                (Path(lan_master_dir) / "survive.txt").write_bytes(content)

                # Then slave1 still receives the broadcast despite slave2's exit
                assert _wait_for_file(str(Path(lan_slave_dir) / "survive.txt"), timeout=30), (
                    "slave1 did not receive the broadcast after another subscriber disconnected"
                )
                assert (Path(lan_slave_dir) / "survive.txt").read_bytes() == content
            finally:
                _terminate(slave1)
        finally:
            _terminate(master)
