# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Utility functions for HSYNC Client, split by responsibility.

Submodules:
    cid         — client ID generation (``get_cid``) and CID log suffix
    http        — fingerprint-pinned HTTP sessions, request wrappers, TLS hints
    paths       — path helpers, per-path locks, in-flight partial registry
    permissions — file/directory permission setters (remote-value sanitization)
    ready       — atomic text writes and the ready-file signalling seam
    resources   — host-resource diagnostics (inotify watch limit, open FDs)
    throttle    — bandwidth parsing and token-bucket throttling

Every name historically exposed by ``hsync_client.utils`` is re-exported here,
so ``from hsync_client.utils import <name>`` keeps working unchanged.
"""

from hsync_client.utils import (  # noqa: F401  (submodules, for ``utils.<name>`` access)
    cid,
    http,
    paths,
    permissions,
    ready,
    resources,
    throttle,
)
from hsync_client.utils.cid import cid_suffix, get_cid
from hsync_client.utils.http import (
    _CONFIG_FINGERPRINT,
    _PLAINTEXT_TLS_SIGNATURES,
    _config_cert_fingerprint,
    _insecure_warning_suppressed,
    _suppress_insecure_warning,
    diagnose_connection_error,
    get_http_session,
    get_ssl_verify,
    http_connect_timeout,
    http_delete,
    http_get,
    http_patch,
    http_post,
    http_to_ws,
    redact_headers,
    request_headers,
    upload_timeout,
)
from hsync_client.utils.paths import (
    _inflight_partials,
    _inflight_partials_guard,
    _path_locks,
    _path_locks_guard,
    ensure_str_path,
    inbound_size_cap,
    is_inflight_partial,
    is_same_paths,
    is_valid_logfile,
    normalize_bucket_relpath,
    open_partial_no_follow,
    path_lock,
    path_present,
    register_inflight_partial,
    safe_rmdir_if_empty,
    unregister_inflight_partial,
)
from hsync_client.utils.permissions import (
    _apply_permissions,
    get_permissions,
    set_permissions,
    set_permissions_on_dir,
)
from hsync_client.utils.ready import (
    _atomic_write_text,
    _remove_ready_file,
    _write_ready_file,
)
from hsync_client.utils.resources import (
    WatchBudget,
    inotify_watch_budget,
    warn_if_inotify_watch_limit_low,
    warn_if_open_fds_high,
)
from hsync_client.utils.throttle import (
    _bandwidth_due,
    get_throttle_delay,
    parse_bandwidth,
    reset_bandwidth_throttle,
)

__all__ = [
    # submodules (also reachable as ``hsync_client.utils.<name>``)
    "cid",
    "http",
    "paths",
    "permissions",
    "ready",
    "resources",
    "throttle",
    # cid
    "cid_suffix",
    "get_cid",
    # http
    "_CONFIG_FINGERPRINT",
    "_PLAINTEXT_TLS_SIGNATURES",
    "_config_cert_fingerprint",
    "_insecure_warning_suppressed",
    "_suppress_insecure_warning",
    "diagnose_connection_error",
    "get_http_session",
    "get_ssl_verify",
    "http_connect_timeout",
    "http_delete",
    "http_get",
    "http_patch",
    "http_post",
    "http_to_ws",
    "redact_headers",
    "request_headers",
    "upload_timeout",
    # paths
    "_inflight_partials",
    "_inflight_partials_guard",
    "_path_locks",
    "_path_locks_guard",
    "ensure_str_path",
    "inbound_size_cap",
    "is_inflight_partial",
    "is_same_paths",
    "is_valid_logfile",
    "normalize_bucket_relpath",
    "open_partial_no_follow",
    "path_lock",
    "path_present",
    "register_inflight_partial",
    "safe_rmdir_if_empty",
    "unregister_inflight_partial",
    # permissions
    "_apply_permissions",
    "get_permissions",
    "set_permissions",
    "set_permissions_on_dir",
    # ready
    "_atomic_write_text",
    "_remove_ready_file",
    "_write_ready_file",
    # resources
    "WatchBudget",
    "inotify_watch_budget",
    "warn_if_inotify_watch_limit_low",
    "warn_if_open_fds_high",
    # throttle
    "_bandwidth_due",
    "get_throttle_delay",
    "parse_bandwidth",
    "reset_bandwidth_throttle",
]
