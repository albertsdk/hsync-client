# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""L1 — client utility micro-benchmark (pure helpers, no sockets/disk).

Measures the per-request / per-path helpers in :mod:`hsync_client.utils` that sit
on every REST and P2P round-trip but carry no I/O of their own:

* URL/header shaping — ``http_to_ws``, ``redact_headers``, ``request_headers``;
* the anti decompression-bomb bound — ``inbound_size_cap``;
* timeout selection — ``http_connect_timeout`` / ``upload_timeout``;
* path helpers — ``normalize_bucket_relpath`` (the wire-path gate used before
  upload), ``is_same_paths``, ``ensure_str_path``.

Measured ops assert the exact returned value so a helper that silently changed
branch cannot be reported as fast.

Usage:
    cd hsync-client && .venv/bin/python bench/bench_utils.py [--count N] [--json-out FILE]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow ``from common import ...`` when run from the bench/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402  (import after sys.path tweak)
    BenchResult,
    add_json_out,
    maybe_write_json,
    measure,
    print_table,
)

from hsync_client.config import set_config  # noqa: E402
from hsync_client.utils import (  # noqa: E402
    ensure_str_path,
    http_connect_timeout,
    http_to_ws,
    inbound_size_cap,
    is_same_paths,
    normalize_bucket_relpath,
    redact_headers,
    request_headers,
    upload_timeout,
)

_BUCKET_ROOT = "/srv/hsync/bucket"
_AUTH_HEADERS = {
    "X-Client-Name": "hsync-client",
    "Authorization": "Bearer sekrit-token",
    "X-Protocol-Version": "api/1",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=2000)
    add_json_out(parser)
    args = parser.parse_args()
    count = args.count

    set_config(
        {
            "HTTP_TIMEOUT": 30,
            "REQUEST_HEADERS": dict(_AUTH_HEADERS),
            "NO_VERIFY_SSL": False,
            "MASTER_CERT_FINGERPRINT": None,
            "CA_CERT": None,
        }
    )

    http_url = "http://127.0.0.1:8080"
    https_url = "https://127.0.0.1:8443"
    assert http_to_ws(http_url) == "ws://127.0.0.1:8080"
    assert http_to_ws(https_url) == "wss://127.0.0.1:8443"
    assert redact_headers(_AUTH_HEADERS)["Authorization"] == "Bearer ****"
    assert redact_headers({}) == {}
    assert request_headers()["X-Client-Name"] == "hsync-client"
    assert inbound_size_cap(1024, 0) == 1024
    assert inbound_size_cap(1024 * 1024, 1024) == 1024
    assert inbound_size_cap(None, 0) is None
    assert inbound_size_cap(None, 4096) == 4096
    assert http_connect_timeout() == 30.0
    assert upload_timeout() == (30.0, None)
    assert is_same_paths("/srv/a/./b", "/srv/a/b") is True
    assert ensure_str_path(b"abc") == "abc" and ensure_str_path(None) == ""
    assert normalize_bucket_relpath(f"{_BUCKET_ROOT}/a/b.txt", _BUCKET_ROOT) == "a/b.txt"
    assert normalize_bucket_relpath("/srv/other/x", _BUCKET_ROOT) is None

    def op_http_to_ws_http() -> None:
        assert http_to_ws(http_url) == "ws://127.0.0.1:8080"

    def op_http_to_ws_https() -> None:
        assert http_to_ws(https_url) == "wss://127.0.0.1:8443"

    def op_redact_headers_auth() -> None:
        assert redact_headers(_AUTH_HEADERS)["Authorization"] == "Bearer ****"

    def op_redact_headers_plain() -> None:
        assert redact_headers({"X-Client-Name": "hsync-client"})["X-Client-Name"] == "hsync-client"

    def op_request_headers() -> None:
        assert request_headers()["X-Client-Name"] == "hsync-client"

    def op_inbound_declared() -> None:
        assert inbound_size_cap(1024, 0) == 1024

    def op_inbound_capped() -> None:
        assert inbound_size_cap(1024 * 1024, 1024) == 1024

    def op_inbound_unknown() -> None:
        assert inbound_size_cap(None, 0) is None

    def op_http_connect_timeout() -> None:
        assert http_connect_timeout() == 30.0

    def op_upload_timeout() -> None:
        assert upload_timeout() == (30.0, None)

    def op_is_same_paths() -> None:
        assert is_same_paths("/srv/a/./b", "/srv/a/b") is True

    def op_ensure_str_path() -> None:
        assert ensure_str_path(b"abc") == "abc"

    def op_normalize_relpath_in() -> None:
        assert normalize_bucket_relpath(f"{_BUCKET_ROOT}/a/b.txt", _BUCKET_ROOT) == "a/b.txt"

    def op_normalize_relpath_out() -> None:
        assert normalize_bucket_relpath("/srv/other/x", _BUCKET_ROOT) is None

    results: list[BenchResult] = [
        measure("http_to_ws(http)", op_http_to_ws_http, count=count),
        measure("http_to_ws(https)", op_http_to_ws_https, count=count),
        measure("redact_headers(auth)", op_redact_headers_auth, count=count),
        measure("redact_headers(plain)", op_redact_headers_plain, count=count),
        measure("request_headers", op_request_headers, count=count),
        measure("inbound_size_cap(declared)", op_inbound_declared, count=count),
        measure("inbound_size_cap(capped)", op_inbound_capped, count=count),
        measure("inbound_size_cap(unknown)", op_inbound_unknown, count=count),
        measure("http_connect_timeout", op_http_connect_timeout, count=count),
        measure("upload_timeout", op_upload_timeout, count=count),
        measure("is_same_paths", op_is_same_paths, count=count),
        measure("ensure_str_path", op_ensure_str_path, count=count),
        measure("normalize_bucket_relpath(in)", op_normalize_relpath_in, count=count),
        measure("normalize_bucket_relpath(out)", op_normalize_relpath_out, count=count),
    ]

    print(f"utils micro-benchmark (ops={count}, pure CPU, no I/O)")
    print_table(results)
    maybe_write_json(args.json_out, results)


if __name__ == "__main__":
    main()
