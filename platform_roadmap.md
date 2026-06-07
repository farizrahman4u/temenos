# temenos platform roadmap

> A roadmap for turning the temenos **OSS core** (a single-user, single-node agent sandbox)
> into a **hosted multi-tenant SaaS**: sandboxes-as-a-service for AI-agent code execution,
> MCP-native, with checkpoint/restore resume-anywhere.
>
> This is a planning document, not a commitment. It complements [`plan.md`](plan.md) (the v1
> design + decisions) and the user-facing [`docs/`](docs/).

---

## 1. Thesis & positioning

**The product:** an API/SDK + hosted fleet that gives any agent harness an isolated,
policy'd place to run untrusted code — created in milliseconds, billed by the second,
resumable from a checkpoint, and reachable as an **MCP endpoint** so an agent's tools land
in the box and nowhere else.

**Who it's for (wedge → expansion):**
1. **Agent builders** who need to run model-authored code safely (the v1 story, hosted).
2. **Agent-product companies** running fleets of agents on customers' repos/data.
3. **Platforms** that embed code execution (data analysis, CI-for-agents, eval harnesses).

**Why us / differentiation** (vs. E2B, Modal Sandboxes, Daytona, Fly Machines, Cloudflare
Sandboxes, Northflank):

| Lever | temenos angle |
|---|---|
| **MCP-native** | The box *is* an MCP server (`/mcp/<id>`); "sole-execution-path" is a first-class product feature, not glue you write. |
| **Resume-anywhere** | gVisor `fscheckpoint` already works; checkpoint→object store gives hibernate-on-idle, fast cold-start, and live migration as a core primitive, not an add-on. |
| **Security framing** | "Trusted agent, untrusted code" is a crisp, sellable boundary; leak-tested containment is the brand. |
| **Python-native policy** | `Policy` as plain data → the same object drives local dev, the API, and config. Low-friction adoption from the OSS core. |

**One-liner:** *the secure runtime for AI agents — now hosted, at fleet scale.*

---

## 2. From OSS core to platform — the gap

The v1 core (`BoxManager` → `Box` → gVisor backend, one per-user daemon, REST + per-box MCP,
`Policy`, `fscheckpoint`) is the **data-plane kernel**. The platform wraps it. The honest gap:

| Capability | OSS v1 today | SaaS needs |
|---|---|---|
| Tenancy | one per-user daemon, no accounts | multi-tenant control plane + durable store (Postgres) |
| Network | on/off toggle, **on by default** | **filtered egress (default-deny)**, no host net, per-box policy |
| AuthZ | one Bearer token per daemon | per-tenant API keys, scoped per-box tokens, RBAC/orgs |
| Quotas | per-box `RLIMIT`/systemd scope | org-level quotas + metering + admission control |
| Persistence | local `checkpoint/` dir | object-store checkpoints, resume-anywhere, hibernate |
| Scale | single node | scheduler + worker fleet, autoscale, multi-region |
| Audit | in-memory per box | persisted, streamed, retained, exportable |
| Observability | none | logs, metrics, traces, web console, live attach |
| Billing | none | usage metering + Stripe + tiers |
| Isolation | gVisor + systemd scope | + node-isolation tiers, image scanning, **abuse detection** |

The two **load-bearing** gaps that gate *hosting strangers' code* at all are **filtered egress
networking** and **per-tenant authz/quotas+abuse controls**. Everything else is scale/DX.

---

## 3. Target architecture

Split the single daemon into a **control plane** (stateless, multi-tenant, durable-backed)
and a **data plane** (the gVisor fleet).

```
                ┌─────────────────────── Control plane (stateless, HA) ──────────────────────┐
   SDKs / MCP   │  API gateway (REST + MCP routing)   AuthN/Z   Scheduler   Metering/Billing │
   clients ────►│        │                               │          │            │           │
   Dashboard    │   Postgres (tenants, boxes, tokens, quotas, audit index)   Object store ────┼──► checkpoints,
                └────────┼───────────────────────────────┼──────────┼──────────────────────────┘     write-sets,
                         │ place / route                  │ schedule │                                images
                         ▼                                ▼          ▼
                ┌──────────────────────── Data plane (worker fleet) ───────────────────────────┐
                │  node-agent (BoxManager++)  ─►  gVisor boxes  ─►  egress proxy (per-box policy)│
                │  • local overlay + checkpoint push/pull          • default-deny, SNI/domain    │
                │  • per-box systemd scope (mem/cpu/pids)            allowlist, metered egress    │
                └────────────────────────────────────────────────────────────────────────────────┘
```

**Key evolutions of core concepts:**
- **Box identity:** today `hash(realpath(data_dir))`. Hosted: a global `box_id` + `tenant_id`;
  the data dir becomes node-local scratch, and the **durable** identity is the checkpoint in
  object storage. A box can be evicted from a node and resumed elsewhere.
- **The node-agent** is `BoxManager` with remote checkpoint push/pull and a registration/heartbeat
  loop to the scheduler. The checkpoint loop (D17) already gives us the snapshot cadence.
- **MCP routing** moves from in-process mount to the gateway: `/mcp/<box_id>` → look up
  placement → proxy to the owning node, scoped by a per-box token (we already mint these).
- **Networking** gains a per-box **egress proxy** sidecar (the box itself stays `--network`
  off-host); policy is a default-deny allowlist enforced + metered there.

---

## 4. Phased roadmap

Each phase is shippable and has an exit gate. Phases assume the OSS core stays the kernel —
platform work lives in a separate control-plane service + a node-agent, not in the core lib.

### P0 — Multi-tenant safety floor *(can we run a stranger's code at all?)*
The non-negotiables before any hosted box runs untrusted code.
- **Filtered egress networking.** Per-box egress proxy: default-deny, domain/SNI allowlist,
  metered bytes; box runs with no host network. Closes the v1 "network is all-or-nothing" gap.
- **Per-tenant authZ.** Tenant accounts + API keys; per-box scoped tokens; ownership checks on
  every op. Isolation invariant: no writable mount or network path shared across tenants.
- **Quotas & admission control.** Per-org caps (concurrent boxes, vCPU/mem/disk, egress GB,
  wall-clock); reject/queue past quota; enforce the systemd-scope limits as a hard requirement
  (not "degraded" mode).
- **Persisted, tamper-evident audit.** Every exec/network-decision/write recorded to durable
  storage, queryable per tenant.
- **Abuse posture v0.** Egress anomaly + crypto-miner heuristics, global egress rate caps,
  kill-switch per tenant.
- **Exit:** an internal red-team / leak-test fleet runs hostile workloads with no host impact,
  no cross-tenant leakage, and bounded blast radius.

### P1 — Control/data-plane split (single-region MVP, closed beta)
- Control-plane service (stateless API) + Postgres schema (tenants, boxes, tokens, quotas,
  audit index).
- **node-agent** (BoxManager + remote checkpoint to object store + heartbeat).
- **Scheduler / placement** across a static worker pool; box lifecycle API v2 (create/exec/
  attach/checkpoint/delete) keyed by `(tenant, box_id)`.
- **Resume-anywhere:** checkpoint push on idle/close, pull on schedule-elsewhere.
- **SDKs:** Python + TypeScript; MCP endpoint documented as the headline integration.
- **Minimal dashboard:** list boxes, view logs/audit, kill.
- **Exit:** a closed-beta tenant runs agents against the hosted API with the same code as the
  OSS CLI; a box survives a node drain via checkpoint/restore.

### P2 — Differentiated DX (public beta)
- **Hibernate-on-idle + fast resume** (the checkpoint moat): scale-to-zero idle boxes, sub-second
  warm resume; bill only active time.
- **Image registry / templates:** prebuilt language/tooling bases (the `image.py` builders,
  hosted + scanned); "fork from template" boxes.
- **Web terminal / live attach:** browser PTY into a box (builds on the interactive-attach work).
- **Logs/metrics/console:** streamed logs, per-box metrics, audit explorer.
- **Integrations:** GitHub (mount a repo / PR-scoped boxes), and a first-class "Claude Code →
  hosted box" path (`temenos claude --remote`).
- **Exit:** public beta open; self-serve signup; a template gallery; idle boxes cost ~$0.

### P3 — Monetization & scale (GA)
- **Metering + billing:** box-seconds, vCPU/mem, egress GB, storage GB; Stripe; free tier +
  usage tiers; spend caps/alerts.
- **Fleet autoscaling:** bin-packed placement, node autoscale, regional capacity; graceful
  drain/migration.
- **Reliability:** HA control plane, rate limiting, status page, SLOs.
- **Exit:** GA pricing live; paying customers; documented SLOs and on-call.

### P4 — Enterprise & compliance
- **Identity:** SSO/SAML/SCIM, orgs/teams/projects, fine-grained RBAC.
- **Compliance:** SOC 2 Type II, audit export/retention controls, data residency / multi-region.
- **Deployment options:** BYOC / VPC-peered data plane, and a self-hosted **Enterprise** edition
  (control plane + node-agent in the customer's cloud).
- **Paranoid isolation tier:** gVisor *inside* a microVM (Firecracker/Kata) or dedicated nodes
  per tenant for the highest-assurance workloads.
- **Exit:** first enterprise contract with BYOC + SSO + SOC 2 in hand.

---

## 5. Cross-cutting pillars (apply across phases)

- **Isolation & security.** gVisor is the floor; layer node-isolation tiers, image scanning,
  no-shared-writable-mounts (already true), secret injection (never in prompt/env — D8),
  and a recurring red-team + the leak-test battery run against the *fleet*, not just the core.
- **Networking.** Default-deny egress is the product's spine for multi-tenancy; allowlist by
  domain/SNI, meter bytes, and treat egress as both a security and a billing surface.
- **Persistence.** Checkpoints and write-sets are first-class objects in object storage →
  resume, hibernate, migrate, fork, and "share a box state." This is the technical moat.
- **Observability.** Audit is a product feature (compliance + debugging), not just a log;
  stream it, retain it, export it.
- **Abuse & trust-and-safety.** Hosting arbitrary code invites miners/spam/attacks — egress
  monitoring, rate caps, KYC for higher tiers, fast tenant kill-switch. Operational pillar,
  not an afterthought.
- **Reliability & cost.** Scale-to-zero + bin-packing for unit economics; node drain via
  checkpoint for zero-downtime ops.

---

## 6. Packaging & pricing (sketch)

| Tier | For | Shape |
|---|---|---|
| **Free** | trying it / OSS users | small monthly box-seconds, capped egress, community support, no SLA |
| **Pro (usage)** | indie/agent builders | pay-as-you-go box-seconds + vCPU/mem + egress + storage; spend caps |
| **Team** | agent-product companies | seats + pooled quotas, orgs/RBAC, higher limits, email support |
| **Enterprise** | regulated / large fleets | BYOC/self-hosted, SSO, SOC 2, paranoid isolation, SLAs, support |

OSS core stays **Apache-2.0** and fully usable single-node (the adoption funnel); the platform
is the hosted control plane + fleet + DX. (Classic open-core.)

---

## 7. Non-goals (for now)

- A general PaaS / long-running service host (boxes are task/agent execution, not deployments).
- A model/LLM provider — we run the *code an agent emits*, provider-agnostic.
- Replacing CI — adjacent, not the wedge.
- Windows guests; non-Linux *guest* workloads (host can be anywhere the fleet runs).

---

## 8. Open questions & risks

- **Network default.** OSS v1 ships network **on** by default; hosted multi-tenant must default
  **off/filtered**. Decision: the platform overrides the policy default (default-deny egress)
  regardless of the lib default — make this explicit in the control plane.
- **gVisor sufficiency for hostile multi-tenant.** Is the sentry boundary enough, or is a
  microVM tier required as the baseline for stranger code? (Drives node-isolation design + cost.)
- **Checkpoint portability across kernels/platforms.** `fscheckpoint` restore across differing
  host kernels / gVisor platforms (kvm vs systrap) — needs validation; may constrain placement.
- **Cold-start vs. cost.** Hibernate-on-idle economics depend on resume latency; how aggressive
  can scale-to-zero be while keeping warm-resume sub-second?
- **Abuse at signup.** Free-tier code execution is a magnet for abuse; how much KYC/friction
  before it kills adoption?
- **Build vs. buy the data plane.** Run our own fleet vs. ride Firecracker-on-bare-metal vs.
  a substrate (Fly Machines / k8s + gVisor runtimeClass). Affects margin and time-to-market.

---

## 9. Success metrics

- **P0/P1:** time-to-first-box (API → running) < 1s warm; zero cross-tenant incidents in
  red-team; box survives node drain.
- **P2:** idle box cost ≈ $0; warm-resume < 1s; N self-serve beta tenants; activation rate.
- **P3:** gross margin per box-second; paid conversion; SLO attainment.
- **P4:** enterprise logos; SOC 2; BYOC deployments.

---

*Foundation in place (OSS v1): `BoxManager`/`Box`/gVisor backend, `Policy`, per-box MCP,
`fscheckpoint` resume, leak-tested containment. The platform is the multi-tenant control plane,
the worker fleet, filtered networking, and the DX around them — built on that kernel, not in it.*
