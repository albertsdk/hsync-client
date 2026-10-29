# HSYNC Client — network-change recovery

One daemon thread detects that the host lost or regained connectivity and tells
the long-lived roles to recover **without a process restart**. Before this
module, a link blip left a peer advertising a dead address and kept the other
side in the desktop's "waiting" state until the client was restarted.

## Symbols

| Symbol | Location | Role |
| ------ | -------- | ---- |
| `NetworkSignature` | `net_watch.py::NetworkSignature` | frozen posture (`source_ip`, `online`) the monitor compares across polls |
| `network_signature` | `net_watch.py::network_signature` | samples `net_utils.get_local_ip` (one UDP `connect`, no packet) and marks the loopback fallback as offline |
| `NetworkChangeMonitor` | `net_watch.py::NetworkChangeMonitor` | the polling thread (`NETWORK_POLL_INTERVAL_S`), `subscribe`/`unsubscribe`, `poll_once`, `signature` |
| `get_network_monitor` | `net_watch.py::get_network_monitor` | process-wide singleton, thread started on first use |
| `MdnsDiscovery._on_network_change` / `_rebuild_mdns` / `_recover_mdns` | `p2p_discovery_mdns.py::MdnsDiscovery._on_network_change` | rebuild the Zeroconf stack on the fresh address / repair after a failed announcement |
| `BootnodeDiscovery._on_network_change` / `_republish_stale_endpoint` | `p2p_discovery_bootnode.py::BootnodeDiscovery._republish_stale_endpoint` | wake the lease loop / force a full endpoint re-register |
| `PeerManager.set_local_address` | `p2p_peers.py::PeerManager.set_local_address` | keep the self-discovery check on the address we currently announce |
| `_ReconnectWaker` | `remote_websocket.py::_ReconnectWaker` | bridges a change into the remote mode's backoff sleep |

## Semantics

- **Only transitions are published.** An unchanged poll notifies nobody, so a
  subscriber never sees the same state twice. `NetworkSignature.online` is
  `False` when `get_local_ip` falls back to `127.0.0.1` (`OFFLINE_SOURCE_IP`), the
  "no route" answer.
- **Callbacks run on the monitor thread** and are executed in subscription order;
  one raising subscriber is logged (`NetworkChange subscriber failed`) and never
  stops the others or the thread. A callback must be short and must not wait for
  another thread that itself waits for the monitor.
- The monitor is a process-wide singleton: the three consumers share one timer
  instead of each polling.

## Consumers

**mDNS** (`MdnsDiscovery.start` subscribes, `stop` unsubscribes). On
`online == False` nothing can be announced, so the record keeps the last address
and `_network_offline` latches. On the way back — an address different from the
advertised one, or a previous offline state — `_rebuild_mdns` retires the old
`Zeroconf` (unregister + close), creates a fresh instance, re-registers the
record on the new address, recreates the `ServiceBrowser` and updates
`PeerManager.set_local_address`. A rebuild, not an in-place
`update_service`, is what restores the per-interface sockets and multicast
memberships an interface bounce destroys, and it replaces the advertised A
record. If a heartbeat re-announce raises, `_recover_mdns` first reconciles the
sockets (`Zeroconf.update_interfaces`, which the client never called before) and
escalates to a rebuild when that fails.

**bootnode** (`BootnodeDiscovery.start` subscribes, `stop` unsubscribes). A
`refresh` carries no endpoints — only a full `register` does — so while a lease
survives an outage the node would keep relaying the pre-outage candidates. The
monitor callback only wakes the lease loop (`_wake`); the loop owns every
publication flag and calls `_republish_stale_endpoint` before each action:
when the sampled address differs from `_published_local_ip` (recorded by the
last endpoint register) it clears `_published_endpoints`, drops the UPnP mapping
and external port (`_external_port`, which belonged to the previous network) and
forces a publish, either inside the re-register that follows an unreachable node
or as its own `publish endpoint` step.

**remote mode** (`remote_websocket.remote_connect`). `_ReconnectWaker` subscribes
for the lifetime of the loop and unsubscribes in the `finally`; an online
signature sets an `asyncio.Event` through `call_soon_threadsafe`, and
`_reconnect_backoff` waits on it instead of `sleep`, so a link that is back is
retried immediately instead of after up to `RECONNECT_DELAY_MAX`. The wait clears
a stale signal first, so a change observed while the link was healthy does not
shorten the next backoff for no reason.

## Tests

`tests/test_net_watch.py` drives the probe by hand (no network).
`tests/test_p2p_sync.py::TestNetworkChangeRecovery` covers the mDNS rebuild,
`tests/test_p2p_discovery_bootnode.py::TestNetworkChange` the endpoint
republish, `tests/test_p2p_peer_manager.py::TestDuplicateIdentityDetection` the
self-address update and
`tests/test_websocket_events.py::TestReconnectBackoffWaker` the interruptible
backoff.
