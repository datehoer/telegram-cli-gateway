# OpenClaw source design review

> A broader Chinese review covering 41 OpenClaw capability areas is available in
> `docs/openclaw-full-source-review.zh-CN.md`.
> This document is research material, not a roadmap. `AGENTS.md` defines the
> current product boundary and implementation rules.

This review is based on the official OpenClaw repository at commit
`c3e3a09fe6a564a7ab6968dfb7e681797264b23d` (2026-08-11), cloned locally to
`/srv/projects/openclaw-source`. It records design ideas only; no credentials,
conversation contents, or OpenClaw runtime state were copied.

## What is worth adopting

### 1. Durable delivery and bounded work lanes

- Persist an outbound Telegram delivery before attempting the network call.
- Acknowledge delivery in two phases so a crash during cleanup does not resend
  an already delivered message.
- Retry only the current Telegram API operation. Honor `retry_after`; otherwise
  use capped exponential backoff with jitter and a dead-letter state.
- Serialize work within one CLI session while allowing independent sessions to
  run concurrently under a process-wide cap.
- Persist inbound tasks with explicit states: `queued`, `running`, `succeeded`,
  `failed`, `timed_out`, `cancelled`, and `lost`.

The current gateway already has in-memory per-session queues and native Codex
steering. Persistence and restart reconciliation are still missing.

### 2. Memory is a governed data pipeline, not a transcript dump

OpenClaw's strongest memory choice is that Markdown files remain canonical and
SQLite is a rebuildable search index. Memory is separated by purpose:

| Tier | Examples | Normal use |
| --- | --- | --- |
| Instructions | `AGENTS.md` | Human-owned and always in context |
| Curated core | `MEMORY.md`, `USER.md` | Small, budgeted, trusted bootstrap context |
| Episodic | daily notes and session transcripts | Search only; never inject wholesale |
| Prospective | cron jobs and standing intents | Trigger only when conditions match |
| Review | `DREAMS.md`, preimages, reports | Operator inspection; never prompt context |

Important write-path safeguards:

- Every indexed chunk receives host-owned provenance: origin class
  (`owner`, `agent`, `untrusted`, or `system`), session kind, observed time,
  and an optional supersession key.
- Cron, heartbeat, subagent, generated dreaming narratives, and recalled text
  are excluded from durable promotion paths.
- The model may judge a bounded set of candidates, but deterministic code owns
  admission, provenance, budgets, deletion limits, structural validation, and
  atomic commit.
- Consolidation binds candidates to exact source lines and exact generated
  entries. It validates that each candidate was handled once and rejects a
  rewrite that loses too much previous memory.
- A content hash prevents committing over a concurrently edited memory file;
  preimages and append-only fallbacks make failure recoverable.
- Memory lookup is fail-open: a timeout or plugin error skips recall and lets
  the user's normal request continue.

Recall is split into two lanes:

1. A fast local lane uses lexical search and deterministic trigger matching.
   It injects at most three trusted matches and caps the added text.
2. A model-assisted recall lane runs only when the message expresses recall
   intent and the fast lane did not already find a strong match.

Project scoping is also useful for a multi-project CLI gateway. Repository keys
are derived from normalized Git origins, only a small active set is retained,
and a memory tagged with multiple projects is eligible only when all of those
projects are active. This prevents a workaround for one repository leaking into
another.

### 3. Skill discovery uses progressive disclosure

OpenClaw does not inject every `SKILL.md` body into every prompt. Discovery
creates a bounded catalog containing name, description, location, version, and
eligibility metadata. The model reads the full skill only when it matches the
task.

Useful details:

- Skill sources have deterministic precedence; workspace skills can override
  personal, managed, bundled, plugin, and extra-root skills with the same name.
- Runtime visibility, model visibility, and user invocability are separate
  decisions. Per-agent allowlists and per-session overrides are also separate.
- Eligibility can require an OS, all/any binaries, environment variables,
  configuration, or an available execution runtime.
- A session keeps a versioned skill snapshot for reproducibility. It refreshes
  only when the catalog/config/filter changes, and a skill content version tells
  the model when it must reread the file.
- Catalog and discovery limits cap skill count, prompt characters, file bytes,
  root depth, and candidates. When over budget, descriptions are compacted or
  omitted with a visible truncation notice.
- Realpath containment and symlink checks prevent a skill root from escaping
  its allowed directory.
- Third-party skill code is scanned for dangerous process execution, dynamic
  evaluation, secret harvesting, exfiltration, mining, obfuscation, and literal
  credentials. Scan work is bounded and cached by file size and modification
  time.

For this gateway, the right first version is a read-only skill bridge rather
than a second skill runtime: index shared and project-local skills, expose a
Telegram `/skills` picker, snapshot the selected path/hash per CLI session, and
tell the native CLI to read the selected skill. Native Codex/Claude skill logic
should remain authoritative.

### 4. Skill changes should be proposals

Skill Workshop provides a reusable governance pattern:

```text
create/update -> pending proposal -> scan/evaluate -> atomic apply
                                    -> reject/quarantine/stale
```

- Generated content is stored as `PROPOSAL.md`; only apply writes a live
  `SKILL.md`.
- An update proposal is bound to the target tree and revision hashes. If either
  changes, the proposal becomes stale instead of overwriting newer work.
- Apply reacquires workspace and target leases, reruns scanning, writes rollback
  metadata before touching files, and makes committed changes observable as
  events.
- Support files are restricted to known directories and reject traversal,
  hidden paths, executables, null bytes, non-UTF-8 files, and overlapping paths.
- Self-learning produces a pending proposal. It does not silently edit a live
  skill unless the operator explicitly chooses an autonomous policy.

We should borrow the proposal/hash/rollback shape only after the read-only skill
picker proves useful. A full autonomous workshop would be premature here.

### 5. Restart recovery is fenced, not guessed

OpenClaw gives each recovery cycle a durable identity and revision. Reservations
and foreground claims include a process lifecycle generation, session id, run
id, and random claim id. Every transition rechecks those facts inside the
storage transaction.

The key safety rule is that an interrupted tool call may already have committed
its side effect even if the result was never persisted. Recovery therefore
classifies transcript tails:

- clearly completed work can settle normally;
- audited replay-safe tools may resume automatically;
- ambiguous side-effecting tool calls may continue only under a restricted
  restart-safe tool policy and are not silently replayed;
- cron, subagent, ACP, and non-main sessions are excluded from main-session
  recovery;
- retries are capped and exhaustion produces a concrete remediation path.

For native CLI processes, the gateway cannot safely reconstruct an in-flight
Claude/Grok/Pi turn after its process dies. It should instead mark the task
`lost`, preserve the prompt and artifacts, reconnect to the CLI's native
session when possible, and offer explicit **retry** and **continue without
replaying** buttons. Codex app-server turns can get a more precise recovery path
only if their thread/turn ids and terminal events are durably recorded.

### 6. Hooks must not destabilize the reply path

OpenClaw uses typed lifecycle events and bounded fire-and-forget execution.
Background hooks have a concurrency limit, finite queue, bounded error logging,
and timeout diagnostics; a full queue drops optional hook work instead of
blocking the user's turn.

The gateway can eventually expose a small internal event set such as
`task.queued`, `turn.started`, `tool.started`, `turn.completed`,
`session.switched`, `artifact.discovered`, and `message.delivered`. Persistence
and delivery must remain core logic; metrics, memory capture, notifications, and
future speech support can subscribe as best-effort hooks.

## Recommended implementation order

1. **Persist the existing inbound queue and task ledger.** Reconcile `running`
   tasks to `lost` on startup and add Telegram retry/continue controls.
2. **Add minimal shared memory.** Keep canonical `MEMORY.md`, `USER.md`, and
   daily notes; build a derived SQLite FTS5 index; add `/remember`, `/memory`,
   and `/forget`; inject only bounded curated core memory. Store explicit source
   and origin metadata outside model-writable text.
3. **Add the read-only skill bridge.** Discover bounded catalogs from shared and
   project roots, validate realpaths and requirements, expose `/skills` buttons,
   and snapshot path/hash per session.
4. **Add optional consolidation.** Promote only interactive, trusted candidates
   through a reviewable proposal with source references, validation, preimage,
   and atomic replace. Do not begin with automatic transcript-wide learning.
5. **Add a bounded hook bus.** Use it for memory capture, observability, and
   later voice transcription/synthesis without coupling them to CLI execution.

## Designs intentionally not copied

- A full plugin platform: the gateway is small and currently has four explicit
  backends.
- Always-on model-assisted Active Memory: it adds latency and cost before enough
  useful memory exists.
- Automatic skill collection rewriting: it needs stronger review UI and demand.
- Cross-session automatic replay of arbitrary tool calls: native CLIs do not
  expose enough durable execution identity to make this safe.
