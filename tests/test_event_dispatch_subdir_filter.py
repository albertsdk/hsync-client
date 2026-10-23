# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""``--subdir`` must scope inbound live events, not only the initial listing."""

from unittest.mock import AsyncMock, patch

from hsync_client.event_dispatch import EventDispatchContext, apply_inbound_event


def _config(tmp_path, **extra):
    return {"LOCAL_BUCKET": str(tmp_path), "CID": "AAAAAAAAAAAAAAAAAAAAA", **extra}


def _file(path: str) -> dict:
    return {"filepath": path, "sha256sum": "a" * 64, "size": 1, "mtime": 1}


class TestInboundSubdirFilter:
    async def test_add_outside_filter_is_ignored(self, tmp_path):
        event = {"name": "added", "cid": "BBBBBBBBBBBBBBBBBBBBB", "src": _file("sub_b/b.txt")}
        with (
            patch("hsync_client.event_dispatch.CONFIG", _config(tmp_path, FILTER_PATH="sub_a")),
            patch("hsync_client.event_dispatch._apply_add_or_modify", new=AsyncMock()) as apply,
        ):
            await apply_inbound_event(event, EventDispatchContext())
        apply.assert_not_awaited()

    async def test_add_inside_filter_is_applied(self, tmp_path):
        event = {"name": "added", "cid": "BBBBBBBBBBBBBBBBBBBBB", "src": _file("sub_a/a.txt")}
        with (
            patch("hsync_client.event_dispatch.CONFIG", _config(tmp_path, FILTER_PATH="sub_a")),
            patch("hsync_client.event_dispatch._apply_add_or_modify", new=AsyncMock()) as apply,
        ):
            await apply_inbound_event(event, EventDispatchContext())
        apply.assert_awaited_once()

    async def test_rename_into_filter_is_an_add(self, tmp_path):
        event = {
            "name": "renamed",
            "cid": "BBBBBBBBBBBBBBBBBBBBB",
            "src": _file("sub_b/b.txt"),
            "dst": _file("sub_a/b.txt"),
        }
        with (
            patch("hsync_client.event_dispatch.CONFIG", _config(tmp_path, FILTER_PATH="sub_a")),
            patch("hsync_client.event_dispatch._apply_add_or_modify", new=AsyncMock()) as apply,
            patch("hsync_client.event_dispatch._apply_delete", new=AsyncMock()) as delete,
        ):
            await apply_inbound_event(event, EventDispatchContext())
        apply.assert_awaited_once()
        delete.assert_not_awaited()

    async def test_rename_out_of_filter_is_a_delete(self, tmp_path):
        event = {
            "name": "renamed",
            "cid": "BBBBBBBBBBBBBBBBBBBBB",
            "src": _file("sub_a/a.txt"),
            "dst": _file("sub_b/a.txt"),
        }
        with (
            patch("hsync_client.event_dispatch.CONFIG", _config(tmp_path, FILTER_PATH="sub_a")),
            patch("hsync_client.event_dispatch._apply_add_or_modify", new=AsyncMock()) as apply,
            patch("hsync_client.event_dispatch._apply_delete", new=AsyncMock()) as delete,
        ):
            await apply_inbound_event(event, EventDispatchContext())
        delete.assert_awaited_once()
        apply.assert_not_awaited()
