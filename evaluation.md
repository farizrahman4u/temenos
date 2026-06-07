# temenos — Evaluation

> An honest assessment of whether temenos is valuable, written after reading the plan,
> the code, and running the test suite + independent isolation probes on this WSL2 box
> (2026-06-07). Verdict first, evidence and reasoning after.

\---

## Verdict

**Valuable, and coherent — not code for code's sake.** The problem is real, the core is
real (verified, not just planned), and the central thesis fits its best use case better
than any alternative framing. The remaining work is mostly *re-prioritization*, not
re-architecture.

The decisive reframing: **temenos is containment for when human-in-the-loop approval
doesn't scale.** Its two audiences —

1. **Platform infrastructure** — running many users' / tenants' agents on untrusted code.
2. **Solo operator of a large agent swarm** — too many parallel agents to allow/deny tool
calls, so agents run in allow-all ("yolo") mode and need a structural backstop.

— are **the same engine**. "Tenant" and "agent" are the same abstraction; `BoxManager`
over N tenants *is* the swarm control plane. The single-repo CLI (`temenos claude`) is a
**demo/testing convenience**, not the product.

\---

## What temenos is

Untrusted-code containment for a *trusted-but-fallible* agent. You declare what an agent's
*executed code* may do (a `Policy`); temenos runs that code inside a **box** — a persistent,
named gVisor sandbox — and returns an audit trail + write-set. Delivered as a daemon
exposing REST (control) + MCP (per-box agent tools); the agent runs on the host, its
payloads run in the box.

**It is a control-plane + policy + harness-integration layer on top of gVisor.** The
isolation engine (`runsc`) already exists; temenos's value is entirely in the layer above
it. That is the correct place for the value to live — see below.

\---

## Technical verification (this box: WSL2 / aarch64 / kernel 6.6 / no KVM)

The plan's headline claims are **real**, not aspirational. Checked directly:

|Claim|Result|
|-|-|
|Test suite|\~90 tests, **89 pass / 1 skip**|
|gVisor integration tests are *real* (drive `runsc`, not mocked)|**19 pass in \~9s** against the installed `runsc`|
|`runsc` actually present|`/usr/local/bin/runsc`|
|Host file outside `read` policy|invisible (`No such file or directory`)|
|`/etc/shadow`|`Permission denied` (gofer runs unprivileged)|
|In-box write to a host path's name|host file **unchanged** (box has its own overlay/tmpfs)|
|Platform auto-detect|falls back to `ptrace` on WSL2 as documented|

The isolation guarantee is genuinely enforced by construction, not by prompt. The spike
findings recorded in `plan.md` (held-run session model, systemd-scope memory enforcement,
image-based writable `/usr`, fscheckpoint roundtrip) are consistent with the code and the
tests that exercise them.

**Scale:** \~2,700 LOC of implementation, well-organized, strict layering
(data → backend → box → manager → surfaces). Through Phase 3 (daemon + `BoxManager`).
Phases 4–6 (project CLI, MCP wiring, leak-test, release) not yet built.

\---

## Why the swarm / allow-all framing is the strongest fit

**Containment-by-construction is exactly what makes allow-all safe.** The common critique
of temenos's T2 model (banning the harness's native tools and routing everything through
box-scoped MCP tools *degrades* the agent) only bites when the alternative is *per-call
human approval*. At swarm scale you have already conceded approval doesn't scale. So the
real comparison is:

> routed-tools-in-a-box  \*\*vs.\*\*  allow-all-with-no-backstop

Against *that*, temenos wins decisively: the agent can yolo freely because **there is
nothing dangerous to allow** — every action structurally lands in a policy'd box. The
project's thesis ("a steering prompt is not enforcement; tool omission is") is *most*
valuable precisely where humans are out of the loop.

**The two audiences are one build.** gVisor is the density sweet spot a swarm needs —
VM-per-agent is too heavy, container-per-agent is a weaker boundary, box-per-agent is
cheap *and* strong. The multi-tenant control plane (`BoxManager`, per-tenant tokens,
quotas, audit, write-set) maps one-to-one onto "fleet of boxes." This collapse is the
project's leverage; it should not be split.

\---

## Where the value is thin (and where it isn't)

|Audience|Valuable?|Why|
|-|-|-|
|Solo dev, one agent, supervised, locally|**Marginal**|A devcontainer does most of this, cross-platform, with less to maintain. This is the demo, not the product.|
|Solo dev, large allow-all swarm|**Yes**|Approval doesn't scale → containment-by-construction is the only safe posture; gVisor density fits.|
|Platform running agents on untrusted / others' code|**Yes**|Multi-tenant isolation + policy + audit is a real, paid need (cf. E2B / Modal / Daytona). Self-hostable + Python-native policy + audit/compliance is the differentiator.|

\---

## Priority corrections implied by the framing

The plan currently leads with the *weakest* audience (single-repo `temenos claude`). To
match the real target:

1. **Promote fleet fan-out from "optional sugar" to core.** `mgr.map()` / batch
create / exec-across-N / teardown / aggregate audit is the swarm operator's main loop,
not a post-v1 nicety.
2. **Filtered network is load-bearing, not post-v1.** This is the biggest real gap. A
swarm of allow-all boxes on `network=host` full passthrough is a data-exfil +
lateral-movement + cloud-metadata surface **multiplied by N**. The all-or-nothing
toggle (D3) forces every net-needing box into full passthrough. Decide explicitly:
either (a) most swarm boxes run `network=none` and that is genuinely sufficient (mount
deps, no live net), or (b) the SNI/allowlist egress proxy is *required* infrastructure.
"Off or wide-open" is the weakest spot for exactly this use case.
3. **Resource fairness must grow up for fleets.** Per-box `MemoryMax` is in place;
a saturated swarm also needs aggregate host caps + backpressure/queueing, not just
per-box limits and per-tenant reject. Today's "reject, don't queue" is a defensible v1
stance — name it as a swarm-scale TODO.
4. **Project-mode (D15/D16) is gold-plating for a demo util.** Git-style `.temenos`
discovery, project-vs-global shadowing, checkpoint-on-stop/restore — a lot of decision
surface for a testing/demo convenience. Keep it minimal; that effort is better spent on
(1) and (2).

\---

## Risks \& honest caveats

* **Market timing.** "Solo dev with a huge agent swarm" is real but a *small* segment in
mid-2026 — it's a bet that fleet-of-agents becomes common. For a swarm power-user today
the value is immediate; as a product it's a wager on the category growing. Build for
self first; the platform story is the same code when/if the category arrives.
* **Harness coupling (T2).** The guarantee depends on the harness exposing *only* temenos
tools. Coupled to Claude Code's exact flags (`--disallowedTools`,
`--strict-mcp-config`); every new native tool is a potential leak. The planned per-harness
leak-test is the right mitigation — it must be the acceptance gate, re-run on every
harness upgrade, not an afterthought.
* **Shared kernel.** All boxes share the gVisor sentry; a sentry CVE is a cross-box risk.
Acceptable under the stated threat model (untrusted *code*, not a nation-state attacker
doing kernel exploitation) — but it must stay explicit in the security docs.
* **v1 platform reach.** Linux/WSL-only, hard `runsc` dependency, memory enforcement needs
systemd user-delegation. macOS/Windows are post-v1. Fine for server-side swarm/platform;
it does limit the local-demo audience (mostly macOS) — another reason the demo isn't the
product. (correction - solid macos plan exists)
* **Planning-to-validation ratio.** The architecture is exquisitely decided (\~800-line
plan for \~2,700 LOC at Phase 3). The bottleneck now is a *usage signal* on the swarm
loop, not more design. Next proof point should be a real fan-out run, not another
decision.

\---

## Bottom line

The engine is right, the thesis matches the use case better than any other framing, and
the two target audiences are a single build. temenos is **infrastructure for containment
when supervision doesn't scale** — valuable as both swarm tooling and platform
infrastructure. The work remaining is to re-aim the headline (fleet fan-out + filtered
network up; single-repo project polish down), prove the swarm loop end-to-end, and make
the per-harness leak-test the hard gate it deserves to be.

