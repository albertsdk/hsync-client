# HSYNC Client — Public discovery relay: feasibility study & measurement plan

> **Status: Plan (Rev. 1, 2026-10-21). Nothing implemented.**
> No code, configuration, protocol or packaging file is changed by this document;
> it exists so the work can be resumed later without re-deriving the analysis.
>
> **Next action:** §9 step 1 — create `scripts/dev/measure_relay_discovery.py`
> and run tests `N1`–`N10` (§7.3), then fill §7.5 and take the decision in §6.
>
> **Scope:** `hsync-client` p2p discovery only. `hsync-server`, the data plane
> (`p2p/1`) and the master election are out of scope and must not change.

## 1. Question

Can `hsync p2p` use a **public, possibly free, discovery service** instead of a
self-hosted `boot/1` bootnode — even if that means adding a new discovery
backend ("another kind of bootnode") to the client?

Sub-questions that decide the answer:

1. Does any public service already provide the contract hsync needs (a
   `(bucket, group)` registry returning endpoint candidates, certificate
   fingerprints and election flags with a TTL)?
2. If not, can a **generic** public relay (HTTP pub/sub, MQTT) carry that
   contract, with records that are **self-authenticating** so the relay does not
   have to be trusted?
3. What does that cost in security, privacy, reliability and maintenance
   compared with running a bootnode?

## 2. What a bootnode substitute must provide

The bootnode is not a generic "find my friends" service: the client depends on
specific fields and semantics. Anything replacing it must cover rows 1–5; rows
6–7 are what a trusted node currently contributes and what an untrusted relay
must replace with cryptography or accept losing.

| # | Requirement | Where it lives today |
|---|---|---|
| 1 | A record per `(cid, bucket, group)`: `endpoints[]`, `cert_fp`, `version`, `capacity`, `connected`, `server_ready`, `prefer_master`, `never_master`, `startup_time`, `sync_direction` | `hsync_common/protocol/bootnode.py::BootRegister`, `::BootPeer` |
| 2 | Random-sample query (`limit` 1–50), TTL 60 s, client refresh 30 s | `hsync-bootnode/protocol-spec/boot-1.md`; `hsync_client/p2p_discovery_bootnode.py::BootnodeDiscovery` |
| 3 | Echo of the observed public address (`ack.observed_endpoint`) — the STUN-equivalent answer | `hsync_client/stun.py::observed_address`, `hsync_client/nat_traversal.py::candidate_endpoints` |
| 4 | Bucket authentication: `HMAC-SHA256(key, "boot/1" \|\| bucket)[:32]` | `hsync_common/protocol/bootnode.py::derive_bucket_token` |
| 5 | Record ownership / anti-hijack (only the source that registered a record may replace or renew it) | `hsync-bootnode` daemon only (`routing.py`) |
| 6 | Transport encryption **and node identity** via certificate pinning / TOFU | `hsync_client/bootnode_trust.py::BootnodeTrustStore`, `hsync_client/boot_client.py::BootnodeClient`, ADR-0034 / ADR-0035 |
| 7 | A **no-log, RAM-only** operator, with `--private` authenticating every frame | `hsync-bootnode` invariants |

Consequences for the design: with an untrusted public relay, (5) becomes
"records are signed by the device", (6) becomes "the record proves the identity
on its own", and (7) is **lost** and must be documented.

## 3. Current state (what already exists)

The client is already structured for a new source — the seam exists:

* `hsync_client/p2p_discovery.py::DiscoveryPlan` — the `Protocol` the sync core
  consumes; `p2p_handler.run_p2p_mode` never branches on the plan.
* `hsync_client/p2p_discovery.py::build_discovery_plan` — the **single decision
  point**: no bootnode → `MdnsDiscovery`; bootnode configured →
  `HybridDiscovery(mdns, bootnode)`.
* `hsync_client/p2p_discovery.py::DiscoveryPlanState` — shared trivial state
  (port, `PeerManager`, connected-clients provider, election advertisement), the
  single construction point for the peer manager.
* `hsync_client/p2p_discovery_hybrid.py::HybridDiscovery` — two children only,
  **hardcoded** to mDNS + bootnode (ADR-0039); a third source needs it
  generalised to N children.
* `hsync_client/p2p_peers.py::Peer` — already carries `endpoints`,
  `cert_fingerprint`, `capabilities_known`, election flags: a relay record maps
  onto it without model changes.
* `hsync_client/boot_client.py::BootnodeClient` — QUIC/TLS client with the trust
  gate; `build_register_frame` / `build_query_frame` / `build_refresh_frame` /
  `build_leave_frame` build the `boot/1` frames.
* `hsync_client/p2p_discovery_bootnode.py` — the `BootPeer` → `Peer` mapping
  lives **inside this module**; a second network backend must reuse it, not copy
  it (DRY, see §8.1).
* Identity material for signing records already exists with no new dependency:
  the device keeps a persistent ECDSA P-256 key + self-signed certificate
  (`hsync_client/ssl_utils.py::load_or_create_master_cert`, stored under
  `$HSYNC_DEVICE_DIR`; `cryptography` is already a dependency of both
  `hsync-client` and `hsync-common`).

Cross-project mirrors that make a new backend expensive (see §8):

* Android mirrors the `boot/1` frames in
  `hsync-android/app/src/main/java/com/hsync/android/data/api/BootnodeProtocol.kt`
  with parity tests and shared fixtures.
* The desktop app exposes the bootnode endpoint and trust policy in its settings
  UI (`hsync_desktop/engine/endpoints.py::normalize_bootnode_endpoint`) and in
  **12 locales**.
* `hsync-client/packaging/wd-os5` and the Debian man page must follow any new
  CLI flag or `HSYNC_*` variable (project rule).

## 4. Candidates and verified facts (probed 2026-10-21)

### 4.1 Reachability baseline measured from the development host

Read-only probes only (DNS resolution, TCP connect, TLS handshake, one
`GET /v1/health`). No message was published. Re-run before trusting the numbers
on a different network.

| Target | DNS | TCP | TLS handshake | Notes |
|---|---|---|---|---|
| `ntfy.sh` | 53 ms | 144 ms | 394 ms | `GET /v1/health` → 200 in 325 / 464 / 351 / 403 / 350 ms (p50 ≈ 351 ms). Let's Encrypt `YR1`, SAN `ntfy.sh`, expires 2026-12-08 |
| `ntfy.sh` read path (`?poll=1&since=all`) | — | — | — | 3/3 → 200 in ~360 ms, empty body: the subscribe API behaves as needed |
| `test.mosquitto.org:8883` | 44 ms | 71 ms | 174 ms | 1883 / 8081 / 8883 reachable. **Private CA** (`O=Mosquitto, CN=mosquitto.org`), cert valid 2020 → 2030 |
| `broker.emqx.io` | **resolves to `0.0.0.0`** | ❌ | ❌ (rc=1 after 5 s) | DNS blocked on this network: a concrete demonstration that a public name can vanish |
| `relay.pkarr.org` | — | 94 ms | — | reachable |

Tooling available: `openssl`, `nc`, `dig`, Docker `29.8.0` (`/snap/bin/docker`).
`paho-mqtt` is **not** installed; `aiohttp`, `websockets`, `requests`,
`cryptography` and `zeroconf` are present in `hsync-client/.venv`.

### 4.2 Payload budget (measured, not estimated)

A relay record must carry enough to be verified without trusting the relay: the
device's self-signed certificate DER (so the receiver can recompute
`cert_fp = sha256(DER)` locally) plus the advertisement fields.

| Item | Size |
|---|---|
| `p2p_master_cert.pem` (real device identity) | 574 B |
| Same certificate, DER | **384 B** |
| Same certificate, base64 (JSON-safe) | **512 B** |
| Full record, compact JSON (`rv/1` draft shape) | **992 B** |

Against the candidate limits:

* ntfy `message-size-limit` default **4 KiB** → 992 B plus the envelope
  (~1.2 KB) fits with ~3× headroom.
* pkarr relay payload limit **1072 B** → the 992 B record is already borderline
  and the envelope does not fit. Dropping the certificate would shrink it to
  ~480 B, but then the receiver cannot bind the signing key to `cert_fp`, i.e.
  the trust model gets weaker. This is the main technical argument against
  pkarr for this use case.

### 4.3 Candidate services

| Service | What it is | Free? | Real terms / limits | Fit |
|---|---|---|---|---|
| **ntfy.sh** | HTTP pub/sub: publish to a topic, subscribe by long-poll/SSE, `since=<id>` replay | Yes | 60-request burst then ~1/10 s per IP; 12 h message cache; a **single** DigitalOcean droplet; topic name = password; logs contain topics and IPs | ⚠️ only turnkey relay; latency fine, rate limit must be measured (`N3`) |
| **Public MQTT** (`broker.emqx.io`, `test.mosquitto.org`) | Broker pub/sub with TLS / WSS / (EMQX) QUIC | Yes | EMQX: "prototype, learn, and test", "all messages are visible to other users"; Mosquitto: "do not rely upon it for anything of importance", often runs under valgrind, private CA | ❌ as a product default (blocked DNS here, private CA, explicit no-production terms). ✅ self-hosted Mosquitto |
| **pkarr** (`pkarr.pubky.app`, `pkarr.pubky.org`, `relay.pkarr.org` + Mainline DHT BEP44) | Signed DNS records addressed by Ed25519 key | Yes, no account | Payload ≤ **1072 B**, one record per key, relays rate-limited | ⚠️ per-key model, not a multi-peer directory; see §4.2 size problem |
| **iroh / n0** (`dns.iroh.link`, public relays) | DNS/Pkarr address lookup + relay infrastructure | Yes | "development and hobby use only", no SLA, rate-limited, latest stable release only, metadata visible | ⚠️ good design reference, not a product dependency |
| **Hysteria Realms** (`realm.hy2.io`, token `public`) | Free public rendezvous for UDP hole punching | Yes, best effort | "No guarantees … may go down, change limits, get blocked, or disappear without notice"; shared token; realm name = secret | ❌ 1:1 introduction protocol, not an N-peer directory; useful precedent for the disclaimer model |
| **libp2p Rendezvous** (`/rendezvous/1.0.0`) | Correct protocol shape: namespace + TTL (default 2 h, max 72 h) | — | **No stable public service**; `py-libp2p` immature | ❌ integration cost with no infrastructure saved |
| **Mainline DHT** (`get_peers`) | Announcer `IP:port` per infohash | Yes | No `cert_fp`, no candidate list, no election flags; inbound UDP required | ❌ insufficient alone |
| **Oracle Always Free / Cloudflare Tunnel / small VPS** | A **self-hosted** bootnode that is publicly reachable | Yes / ~€3–4 per month | You operate it | ✅ no protocol change (option C) |
| **Tailscale / ZeroTier** | Managed mesh VPN | Yes (device limits) | Account + third-party control plane; no per-bucket discovery | ⚠️ solves reachability, not discovery |

Key reading of the table: **no public service provides §2 rows 1–5**. Only two
viable shapes remain: a generic relay carrying a signed record (option A), or a
hosted-in-someone's-name bootnode (option C).

## 5. Options

### A — Public relay backend with signed records (**no trusted node**)

Add a new `DiscoveryPlan` backed by a generic public relay. The relay is
**untrusted by construction**: presence records are signed with the device's
identity key, so the relay cannot forge or substitute a peer.

Design constraints (all mandatory):

1. **Self-authenticating record.** Sign `{cid, ns, cert_fp, endpoints[],
   election fields, issued_at, ttl, seq}` with the device key; ship the
   self-signed **cert DER** in the record so the receiver verifies the signature
   *and* recomputes `sha256(DER) == cert_fp` locally.
2. **Keyed namespace.** Topic = `HMAC(group_key, "hsync-rv/1" || bucket)`, with a
   domain-separation label **distinct** from `boot/1`'s `derive_bucket_token`,
   so the relay operator cannot read the bucket/group names and the two token
   domains cannot collide.
3. **Replay/rollback protection:** `issued_at` + `seq` monotonic per `(cid, ns)`,
   `ttl` bounded; stale/duplicate records dropped.
4. **Real STUN.** The relay does not echo the observed address; `stun.py` today
   only *reads* the `ack`. A minimal RFC 5389 binding client is required, with
   configurable servers (public ones are free, with `HSYNC_STUN_SERVERS`
   override).
5. **No node pinning.** `--bootnode-fingerprint/-tofu/-trust` must be **rejected**
   when a relay backend is selected; there is no node identity to pin.
6. **Explicit opt-in only.** The docs must state that the relay is third-party,
   best-effort and sees IP/cadence metadata.

Costs: rate-limit etiquette on a shared free service, O(N) bandwidth per round
(the bootnode returns a random sample, a pub/sub bus returns every peer),
server-side message retention instead of a 60 s TTL, and the loss of the
no-log/`--private` guarantees.

### B — Pure DHT (pkarr / Mainline DHT)

Elegant and serverless, but per-key records do not model "N peers in a bucket".
A shared group key makes the value overwritable by any member; a per-peer key
leaves no index to enumerate peers. Adding an aggregator reintroduces the
service. Combined with §4.2 (1072 B), pkarr is a long-term research direction,
not this task.

### C — Public hsync bootnode fleet (**recommended default**)

Ship one or more default bootnodes in the client (the Bitcoin `seed` /
Ethereum `bootnodes.txt` model), with fallback. Protocol unchanged, pin/TOFU
works, STUN-echo works, Android parity untouched.

Cost: someone must operate the nodes. Zero-cost hosting exists (Oracle Always
Free ARM instances, or Cloudflare Tunnel in front of a home machine — no public
IP needed), otherwise ~€3–4/month. Privacy: the operator sees metadata, which
is already true for a self-hosted node. Reliability: a single shipped seed is a
single point of failure; ship at least two, ideally with an SRV record.

### D — `boot/1` over HTTPS/WSS (new transport profile)

Needed only to run the bootnode on serverless free tiers (Cloudflare Workers,
Deno Deploy: no UDP/QUIC). That is a **new transport contract**, therefore
`boot/2` or an ALPN-negotiated profile, with spec + fixtures + Python mirror +
Kotlin mirror + trust review. Medium-high cost; benefit is cheap hosting and
443/TCP reachability.

### Option summary

| | Security model | Infra to operate | Protocol change | Cross-repo blast radius | Effort |
|---|---|---|---|---|---|
| A | Signed records (relay untrusted) | None (third-party) | New `rv/1` + fixtures | client + common (+ desktop/android if reached) | 8–12 pd (Python only) |
| B | Signed records | None | New record + aggregator | large | high, unresolved directory model |
| C | Pin/TOFU (unchanged) | 1–2 bootnodes | **None** | config/docs only | 1–2 pd + hosting |
| D | Pin/TOFU (unchanged) | Free-tier hosting | New transport profile | large (spec + 2 mirrors) | 5–8 pd |

## 6. Decision criteria (go/no-go)

The thresholds are derived from the existing cadence, not invented: with a
bootnode the client raises liveness to `max(configured, 2 ×
BOOTNODE_REFRESH_INTERVAL)` = 120 s in the worst case, so a relay latency of a
few seconds is harmless. **Latency is not the risk — rate limits and loss are.**

| Gate | Requirement | Rationale |
|---|---|---|
| G1 | publish→receive p95 ≤ 2 s, p99 ≤ 4 s | far below `PEER_TIMEOUT` (60–120 s) |
| G2 | message loss **0** over 30 samples | a lost presence update is recovered by the next refresh, but should not happen |
| G3 | **zero** HTTP 429 with 1 publish/30 s **per bucket**, up to 3 buckets from one IP | otherwise the free tier is unusable for a real multi-bucket user → opt-in only |
| G4 | recovery after a 90 s subscriber outage ≤ 60 s (via `since`) | bounded catch-up |
| G5 | 1.2 KB record accepted end-to-end | §4.2 |
| G6 | TLS verifiable with **public** CAs (no shipped private CA) | a product cannot trust `mosquitto.org`'s private CA by default |
| G7 | 60-minute soak: no gap > 2 × refresh interval | best-effort service must be *stable enough* |

Decision matrix:

| Outcome | Decision |
|---|---|
| G1–G7 pass on ntfy | **Option A** viable as an explicit opt-in; proceed with `rv/1` (§8.1–§8.4). |
| G3 fails | ntfy cannot carry real multi-bucket users → default stays **Option C**; A is dropped or demoted to a hobby toggle. |
| G6 fails for public MQTT but a local broker passes | **Hybrid**: keep `boot/1` as the default, add a self-hosted relay profile for operators who cannot expose UDP/QUIC (cheaper than a bootnode, works on TCP-only hosting). |
| G1/G2/G7 fail broadly | No third-party relay: **Option C** (and optionally **D**). |

Whatever the outcome, **Option C remains the only zero-protocol-change path**
and should be evaluated in parallel as the fallback.

## 7. Measurement prototype

### 7.1 Location, dependencies, interface

| Aspect | Decision |
|---|---|
| Path | `scripts/dev/measure_relay_discovery.py` (workspace root) |
| Why there | `scripts/dev/` belongs to no Poetry project and is explicitly out of the Quality Gate; the prototype must not pollute `hsync_client` or its venv |
| Dependencies | **stdlib only** (`urllib`, `ssl`, `socket`, `threading`, `json`, `statistics`, `secrets`, `time`, `argparse`). MQTT via a ~150-line minimal 3.1.1 client inside the prototype (CONNECT/SUBSCRIBE/PUBLISH, QoS 0–1, keepalive) — no `paho-mqtt` added to the repo |
| Sanity | It simulates peers with the projected `rv/1` record shape; it does **not** import or modify hsync code, so no protocol decision is taken before the data exists |
| Output | JSONL samples to `/tmp/relay-results.jsonl` + a printed summary (p50/p95/p99/max, loss, duplicates, 429s, `Retry-After`, bytes/min) |

```text
measure_relay_discovery.py ntfy  --topic hsdev-<random> --duration 300 --interval 10 [--peers 3]
measure_relay_discovery.py mqtt  --url mqtt://127.0.0.1:1883 --topic hsdev/<random> [--peers 2]
measure_relay_discovery.py report /tmp/relay-results.jsonl
```

### 7.2 Sample record and peer simulation

Each publish carries one JSON object of the projected record (≈992 B, §4.2):
`id`, `peer`, `sent_wall_ns`, `cert_b64` (the real 384 B DER, base64), `fp`,
`endpoints[]`, election fields, `issued_at`, `ttl`, `seq`, plus a filler string so
the payload matches the real size. Latency = `receive_wall_ns − sent_wall_ns`
(same host → no clock skew). Payloads are **synthetic and non-sensitive**.

### 7.3 Test matrix

| ID | Purpose | Command | Measured | Gate |
|---|---|---|---|---|
| N1 | ntfy publish→receive latency | `ntfy --duration 300 --interval 10` | p50/p95/p99/max, loss | G1, G2 |
| N2 | ntfy rate-limit shape | `ntfy --burst 70` | HTTP 429 count, `Retry-After` values | characterisation |
| N3 | ntfy sustained multi-bucket | `ntfy --peers 3 --duration 1800 --interval 30` | 429 per topic, loss | **G3** |
| N4 | ntfy fan-out cost | `ntfy --subscribers 5 --duration 600 --interval 30` | bytes/min, loss | G2 (cost documented) |
| N5 | ntfy outage recovery | subscriber stopped 90 s, resume with `since=<id>` | collected messages, gap | G4 |
| N6 | MQTT QoS0/QoS1 latency | `mqtt --qos 0` then `--qos 1 --duration 300` | p95, duplicates | G1 |
| N7 | MQTT payload ceiling | `mqtt --payload 1024/4096/16384` | accepted/rejected | G5 |
| N8 | MQTT retained delivery | `mqtt --retained` + late subscriber | state received | supports TTL replacement |
| N9 | MQTT keepalive | `mqtt --duration 300 --idle-probe` | disconnects | stability |
| N10 | 60-minute soak | `ntfy --duration 3600 --interval 30 --peers 2` | gaps > 2× interval | **G7** |

Public-broker limits are checked on `test.mosquitto.org:8883` (with its CA file)
to evaluate **G6**; functional MQTT tests (N6–N9) run against a **local**
Mosquitto so that restart, retained and idle behaviour are controllable and no
public service is abused.

### 7.4 Execution procedure

1. Create the harness (§9 step 1) and lint it from `hsync-client` (so its ruff
   config applies), pointing at the file in the sibling `scripts/dev/`:
   `.venv/bin/python -m ruff check --fix ../scripts/dev/measure_relay_discovery.py`.
2. Start a local broker for N6–N9:

   ```bash
   mkdir -p /tmp/hsync-mqtt
   printf 'listener 1883\nallow_anonymous true\n' \
     > /tmp/hsync-mqtt/mosquitto.conf
   docker run -d --rm --name hsync-mqtt -p 1883:1883 \
     -v /tmp/hsync-mqtt/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro \
     eclipse-mosquitto:2
   ```

3. Run N1, N2, N3, N4, N5, N10 against ntfy with a **random unused topic**
   (`hsdev-<16 hex>`); run N6–N9 against `mqtt://127.0.0.1:1883`; run one
   `mqtts://test.mosquitto.org:8883` session only to record G6.
4. Stop and remove the container: `docker rm -f hsync-mqtt`.
5. Copy `/tmp/relay-results.jsonl` to a durable path (or paste the summary into
   §7.5) before the machine reboots.
6. Optionally repeat N1/N3 from a second network (VPS + mobile hotspot) to
   separate relay jitter from the local uplink.

Etiquette: no retained messages on public brokers, no sensitive payloads,
random topics, no volume beyond the matrix. All of this stays inside what the
services' terms allow (test/hobby use).

### 7.5 Results

_Fill after running the matrix. Keep the raw JSONL path and the date._

| ID | Date | p50 | p95 | p99 | Loss | 429 | Notes | Verdict |
|---|---|---|---|---|---|---|---|---|
| N1 | _TBD_ | | | | | | | |
| N2 | _TBD_ | | | | | | | |
| N3 | _TBD_ | | | | | | | |
| N4 | _TBD_ | | | | | | | |
| N5 | _TBD_ | | | | | | | |
| N6 | _TBD_ | | | | | | | |
| N7 | _TBD_ | | | | | | | |
| N8 | _TBD_ | | | | | | | |
| N9 | _TBD_ | | | | | | | |
| N10 | _TBD_ | | | | | | | |

Raw data: _TBD (`/tmp/relay-results.jsonl`, or a durable copy)_

### 7.6 Known limitations of the measurement

* One vantage point (the dev host, residential uplink) unless step 6 is done:
  absolute latency includes the local uplink, while rate limits are per public
  IP and therefore already representative.
* Simulated peers, not real `hsync` instances: the harness validates the
  *transport* assumptions (latency, limits, retention), not the integration.
  End-to-end validation belongs to a later phase, after the decision.
* Clock assumption: `sent_wall_ns` uses the same host clock, so cross-host runs
  need NTP.

## 8. Post-decision implementation outline

Nothing below is implemented. It exists to make the follow-up work mechanical
once §6 picks a direction. Phases are ordered so the shared refactors (§8.1)
happen once regardless of which option wins.

### 8.1 Shared enablers (needed by both A and C)

1. **Generalise `HybridDiscovery` to N children.**
   `hsync_client/p2p_discovery_hybrid.py::HybridDiscovery` hardcodes
   `mdns` + `bootnode`. Replace with a list of child plans, keeping the ADR-0039
   semantics: one shared `PeerManager`, presence emitted once, `check_fatal()`
   across every child, `endpoint_published()` = AND of all children, `set_*`
   fan-out.
   Tests: the existing hybrid tests must be extended, not rewritten — one child
   must behave exactly like today.
2. **Extract the `BootPeer` → `Peer` mapper.** The mapping currently lives inside
   `p2p_discovery_bootnode.py`; move it to a shared module (e.g.
   `p2p_discovery_peer.py`) and have both backends call it. This is the
   "no copy-paste-modify" guarantee for the new backend.
3. **Keep `build_discovery_plan` the only decision point.** Every new backend is
   selected there; `p2p_handler.run_p2p_mode` must not learn about it.

### 8.2 Option A — relay backend (if G1–G7 pass)

| Step | Artifact | Notes |
|---|---|---|
| A1 | `hsync-common/hsync_common/protocol/rendezvous.py` | `rv/1` record: strict `TypedDict` + hand-written validators (same style as `protocol/bootnode.py`, no pydantic), `derive_bucket_namespace`, `sign_record`, `verify_record`; the docstring must declare its consumers (public-API traceability rule) |
| A2 | `hsync-common/tests/test_protocol_rendezvous.py` + `protocol/fixtures/rendezvous-*.json` | Golden fixtures; tampered signature, stale `seq`, expired `ttl`, foreign namespace |
| A3 | `hsync-client/hsync_client/stun.py` | RFC 5389 binding client (replaces the ack-only path **for this backend**); keep `observed_address` for the bootnode; `HSYNC_STUN_SERVERS` override |
| A4 | `hsync-client/hsync_client/p2p_discovery_relay.py` | New `DiscoveryPlan`: publish/refresh on the relay, subscribe/long-poll (or MQTT), verify → map via §8.1.2 → `PeerManager` |
| A5 | `hsync-client/hsync_client/config.py`, `cli.py` | Endpoint schema (`ntfy://…`, `mqtts://…`) or a dedicated `HSYNC_DISCOVERY_RELAY`; reject `--bootnode-fingerprint/-tofu/-trust`; validate next to `_build_bootnode` without changing its bootnode semantics |
| A6 | Events | The `discovery` event `plan` value becomes `"mdns+relay"`; verify the events schema in `hsync-common` and the desktop consumer |
| A7 | Tests | `tests/test_p2p_discovery_relay.py` with a fake local relay (deterministic), signature/replay/namespace cases; `test_bb_*` only against the local fake relay — never against `ntfy.sh` in CI |
| A8 | Desktop | `hsync_desktop/engine/endpoints.py::normalize_bootnode_endpoint` + settings/wizard + **12 locales** |
| A9 | Android | Only if the backend must be reachable from the app: a mirror in the style of `data/api/BootnodeProtocol.kt` + parity test + shared fixtures. Otherwise document the asymmetry explicitly |
| A10 | Packaging/docs | Man page `packaging/deb/usr/share/man/man1/hsync.1`; `packaging/wd-os5` (`start.sh`, `config_get.php`, `config_set.php`, `app.js`, `index.html`, i18n ×5: EN/IT/DE/ES/FR); `docs/p2p-bootnode.md` (or a new `docs/p2p-relay.md`), `docs/configuration-p2p.md`, `docs/faq-p2p.md`, `docs/troubleshooting.md`, `llm-wiki/modules/p2p-mode.md`, ADR |

Security obligation: record signing, namespace derivation and replay handling
are a protocol contract — independent review plus fixtures, not only unit tests.

### 8.3 Option C — public bootnode fleet (if A fails, or as the default)

| Step | Artifact | Notes |
|---|---|---|
| C1 | `hsync_client/constants.py` | `BOOTNODE_SEEDS_DEFAULT: tuple[str, ...]` (≥ 2 seeds) + docstring naming the consumers |
| C2 | `build_discovery_plan` + §8.1.1 | Use the seeds when the user configured none; fall back across seeds so one dead node does not disable cross-network discovery |
| C3 | Trust UX | Default nodes are still pinned/TOFU (ADR-0034/0035); decide whether a shipped seed may be `accept-new` by default (recommendation: keep `ask`, document the non-interactive path) |
| C4 | Ops | Two tiny nodes (Oracle Always Free ARM, or Cloudflare Tunnel + a home host); fingerprints published out of band |
| C5 | Docs | `docs/p2p-bootnode.md` (zero-config section), `docs/faq-p2p.md`, `llm-wiki/modules/p2p-mode.md`, man page if a new env/flag appears, ADR |
| C6 | Optional | SRV seed discovery (`_hsync-boot._udp.<domain>`); needs a DNS-SRV library decision (`dnspython` is **not** a dependency today) |

### 8.4 Option D — `boot/1` over HTTPS/WSS (only if C is too expensive to host)

Spec bump (`boot/2` or a negotiated transport profile) → fixtures →
`hsync-common` mirror → transport abstraction in `boot_client.py` (today
aioquic-specific) → Kotlin mirror + parity tests → trust review (the pinning
model is unchanged, only the transport). Highest blast radius of the four; do not
start before §6/C is settled.

## 9. Resume checklist

Work top-down; each item is independent and leaves the tree consistent.

- [ ] **1.** Create `scripts/dev/measure_relay_discovery.py` (stdlib only; ntfy + minimal MQTT client + `report`); lint it with ruff from `hsync-client`.
- [ ] **2.** Run `N1`–`N5` on ntfy with a random topic; paste the summary into §7.5.
- [ ] **3.** Start the local Mosquitto container, run `N6`–`N9`, then one `mqtts://test.mosquitto.org:8883` session to record **G6**.
- [ ] **4.** Run `N10` (60-minute soak); record the gaps.
- [ ] **5.** Evaluate the §6 gates, fill the "Verdict" column and add a one-line `Decision:` under §6.
- [ ] **6.** If Option A: write ADR `hsync-common/docs/internal/adr/0040-*.md` and add it to `adr/README.md`; implement §8.1 then §8.2 phase by phase.
- [ ] **7.** If Option C: implement §8.1.1 + C1–C2, then provision the nodes (C4) before touching documentation.
- [ ] **8.** If the answer is "no third-party relay": record it in the ADR's *Alternatives rejected* section with the §7.5 numbers — negative results are the point of this document.
- [ ] **9.** Update the `docs/README.md` internal index if this file is renamed or split.

Verification commands for any code phase (from the workspace root, one per
command, per the project rules):

```bash
cd hsync-client && .venv/bin/python -m pytest -q tests/test_p2p_discovery_relay.py
cd hsync-client && .venv/bin/python -m pytest -q
cd hsync-client && .venv/bin/python -m ruff check .
cd hsync-client && .venv/bin/python -m mypy hsync_client
cd hsync-common && .venv/bin/python -m pytest -q tests/test_protocol_rendezvous.py
cd hsync-common && .venv/bin/python -m ruff check .
```

## 10. Risks and limitations

1. **Terms of service.** None of the free services is intended for production
   (iroh: "development and hobby use only"; ntfy: best effort, single droplet;
   Mosquitto: "do not rely upon it"; EMQX: "prototype, learn, and test").
   Shipping one as a **default** is a legal and continuity risk: ship it as an
   explicit opt-in, or operate your own node.
2. **Availability.** A public name can disappear (observed: `broker.emqx.io` →
   `0.0.0.0` from this network). A relay failure must degrade to mDNS with a
   clear error — never a silent retry loop.
3. **Privacy regression.** The current bootnode is no-log, RAM-only, and
   `--private` authenticates every frame. A third-party relay sees source IPs,
   cadence and message sizes; a keyed namespace hides bucket names but not the
   traffic pattern. This must be stated in the user docs.
4. **First-contact trust.** With no node to pin, security rests on signed records
   plus the existing `(cid, bucket)` TOFU pin. `--require-pairing` becomes close
   to mandatory for sensitive groups.
5. **Bandwidth shape.** A random-sample query is O(1); a pub/sub bus delivers
   every peer, i.e. O(N) per refresh. Fine for small groups, must be documented.
6. **Retention semantics.** ntfy keeps messages for 12 h while the bootnode TTL
   is 60 s. Clients must expire records locally from `issued_at`/`ttl`, or stale
   peers reappear after a restart.
7. **Shared-IP limits.** Rate limits are per public IP: users behind CGNAT share
   a budget they do not control. That is why G3 uses three buckets on one IP.
8. **Asymmetry if Android is skipped.** A relay reachable only from the Python
   client splits the ecosystem; if Android is out of scope, the docs must say so.

## 11. Open questions

1. Is the goal *zero configuration for the end user* (→ Option C: you must run
   the fleet) or *zero infrastructure for the project* (→ Option A: relay +
   signed records, opt-in)?
2. May a shipped default point at a third-party service at all, or must every
   default be hsync-operated?
3. Should a relay be an **alternative** to `boot/1` or a **coexisting** third
   source (mDNS + bootnode + relay) once §8.1.1 lands?
4. If MQTT is kept, is a new dependency (`paho-mqtt`) acceptable, or must the
   mini client stay test-only forever?
5. Is the Android mirror in scope for the first release of the feature?

## 12. References

**Code anchors**

* `hsync_client/p2p_discovery.py::DiscoveryPlan`, `::DiscoveryPlanState`,
  `::build_discovery_plan`
* `hsync_client/p2p_discovery_bootnode.py::BootnodeDiscovery`
* `hsync_client/p2p_discovery_hybrid.py::HybridDiscovery`
* `hsync_client/p2p_peers.py::Peer`, `::PeerManager`, `::dial_candidates`
* `hsync_client/boot_client.py::BootnodeClient`, `::build_register_frame`,
  `::build_query_frame`, `::build_refresh_frame`, `::build_leave_frame`
* `hsync_client/bootnode_trust.py::BootnodeTrustStore`
* `hsync_client/stun.py::observed_address`,
  `hsync_client/nat_traversal.py::candidate_endpoints`
* `hsync_client/ssl_utils.py::load_or_create_master_cert`
* `hsync_client/cli.py::_build_bootnode`
* `hsync_common/protocol/bootnode.py::BootRegister`, `::BootPeer`,
  `::derive_bucket_token`
* `hsync_common/protocol/tls_trust.py::TrustPolicy`

**ADRs**

* `hsync-common/docs/internal/adr/0032-bootnode-discovery.md`
* `hsync-common/docs/internal/adr/0034-bootnode-tls-trust.md`
* `hsync-common/docs/internal/adr/0035-bootnode-trust-policy.md`
* `hsync-common/docs/internal/adr/0038-bootnode-endpoint-candidate-list.md`
* `hsync-common/docs/internal/adr/0039-combined-discovery.md`

**Protocol specs**

* `hsync-bootnode/protocol-spec/boot-1.md`
* `hsync-common/docs/bootnode-protocol.md`
* `hsync-common/docs/p2p-protocol.md`

**External**

* ntfy publish/subscribe API — <https://docs.ntfy.sh/publish/>
* ntfy server config and limits — <https://docs.ntfy.sh/config/>
* ntfy terms of service — <https://docs.ntfy.sh/terms/>
* pkarr relays and payload limit — <https://pubky.github.io/pkarr/design/relays.html>
* iroh address lookup — <https://docs.iroh.computer/concepts/address-lookup>
* iroh public relays (support policy) — <https://docs.iroh.computer/iroh-services/relays/public>
* Hysteria Realms (public rendezvous disclaimer) — <https://github.com/apernet/hysteria-website/blob/master/docs/docs/advanced/Realms.md>
* libp2p rendezvous protocol — <https://github.com/libp2p/docs/blob/master/content/concepts/discovery-routing/rendezvous.md>
* EMQX public broker — <https://www.emqx.com/en/mqtt/public-mqtt5-broker>
* `test.mosquitto.org` — <https://test.mosquitto.org/>

## Revision history

| Rev. | Date | Change |
|---|---|---|
| 1 | 2026-10-21 | Initial plan: requirements, candidate survey, measured reachability and payload baseline, options A–D, go/no-go gates, measurement harness spec, implementation outline, resume checklist |
