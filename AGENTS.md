# Project Instructions

## Product Identity

This project is a small Telegram gateway for remotely using native AI CLIs such as Codex, Claude Code, Grok, and Pi.

It is not a general agent platform and it is not a smaller OpenClaw. The native CLIs own model access, authentication, reasoning, tool use, permissions, context, and their native sessions. This project should only provide the thin control layer that Telegram needs: access control, session selection, input and attachment forwarding, streamed output, interruption, and reliable delivery.

The project should develop its own style around native CLI capabilities. OpenClaw and other systems may provide useful implementation lessons, but their architecture is reference material, not a target to reproduce.

## Core Engineering Principle

Build the smallest runnable end-to-end loop from mature capabilities first. Evolve the architecture only from real usage, observed failures, and stable boundaries.

Do not add complexity for hypothetical requirements. Do not present a temporary implementation as a permanent abstraction. Do not add technical spectacle or novelty for its own sake. Write only the clear, reliable, maintainable code required by the current feature.

## Decision Rules

Before adding a feature or abstraction, establish all of the following:

1. A current user workflow or observed failure requires it.
2. The gateway, rather than a native CLI or Telegram, is the correct owner.
3. The smallest implementation provides meaningful end-to-end value.
4. Its state, failure behavior, and deletion or rollback path are understood.
5. It can be tested without constructing a second platform around it.

If these conditions are not met, defer the work and record the concrete trigger that would justify revisiting it.

## Architecture Boundaries

- Keep one long-running gateway process unless a demonstrated reliability requirement demands another component.
- Prefer the Python standard library and the existing runtime. Add a dependency only when a mature library materially reduces correctness or maintenance risk.
- Keep Telegram transport concerns separate from CLI protocol parsing and persistent session state, but do not create layers that have only one trivial implementation.
- Prefer each CLI's structured, supported protocol. Use terminal emulation only when no stable structured interface exists.
- Represent backend differences explicitly and locally. Introduce a shared capability interface only after at least two implementations need the same stable contract.
- Let native CLIs own model routing, tool execution, context management, memory, skills, and provider authentication.
- Keep gateway-owned state small, bounded, inspectable, and recoverable. JSON is acceptable while it remains sufficient; migrate storage only for a demonstrated transactional or query need.
- Refactor code when a current change exposes duplication, unsafe coupling, or an unstable boundary. Do not refactor solely to make the architecture look more sophisticated.

## Reliability Rules

- Never automatically replay a CLI turn after output or external side effects may have occurred.
- Treat CLI execution and Telegram delivery as separate outcomes.
- Persist only the minimum state needed for user-visible recovery. Do not persist token-level streams or duplicate native CLI transcripts.
- Use atomic file replacement for persistent state and keep collections bounded.
- Make restart behavior explicit: resume only through a backend's supported native mechanism; otherwise report interruption instead of guessing.
- Keep Telegram update processing idempotent where duplicate delivery is plausible.
- A streaming preview may be best-effort. A completed answer must either be delivered or fail visibly and remain recoverable when that requirement is implemented.
- Do not use Telegram Rich Message Drafts for streaming: an interrupted draft cannot be finalized or removed by the gateway, leaving a permanent "loading" bubble that re-renders on every chat open. Stream by editing one persistent message in place.
- Never silently switch a user-selected CLI or model after a turn has started.

## Security Model

- Allowlisted Telegram users are trusted high-privilege operators because the CLIs run in bypass mode.
- Protect the bot token, Telegram account, operating-system account, and runtime directory as the real security boundary.
- Do not describe `ALLOWED_WORKDIRS` as a sandbox. It limits gateway-selected working directories and outbound files, not what bypassed CLIs can access.
- Validate callback ownership, local paths, attachment size, and destructive targets at the gateway boundary.
- Never log tokens, credentials, raw environment files, or unnecessary conversation contents.

## Implementation Style

- Prefer straightforward data structures, explicit control flow, and descriptive names.
- Keep changes narrow. Preserve existing behavior unless the task explicitly changes it.
- Avoid speculative registries, plugin systems, generic workflow engines, event buses, dependency injection frameworks, and distributed components.
- Avoid broad configuration surfaces. Add a setting only when users need to choose between valid behaviors.
- Keep user-facing Telegram messages concise and actionable.
- Comments should explain non-obvious constraints or failure semantics, not restate the code.
- Temporary compatibility code must be labeled by its concrete removal condition.

## Testing and Verification

- Add or update focused tests for every behavior change and regression.
- Prefer tests at stable boundaries: Telegram request payloads, backend event parsing, session state transitions, path validation, queue behavior, and restart recovery.
- Run the smallest relevant tests while iterating, then run the full suite before handoff.
- For changes to a real CLI integration, perform a bounded smoke test when it can be done without exposing credentials or modifying unrelated user state.
- Do not claim durability, resume support, isolation, or delivery guarantees beyond what was actually tested.

## Scope Guidance

Features that fit this project when there is a concrete need include:

- reliable switching among native CLI sessions;
- forwarding text, images, and files;
- streaming meaningful progress and final responses;
- interruption and backend-supported steering;
- small, visible task queues;
- bounded restart and delivery recovery;
- concise health and diagnostic information;
- optional voice-note transcription or speech output through a narrow adapter.

Features that are out of scope without a new, concrete requirement include:

- a model-provider abstraction owned by the gateway;
- a plugin marketplace or general plugin runtime;
- a gateway-owned agent, memory, skill, or tool platform;
- multi-agent orchestration, workboards, workflow engines, or cloud-worker fleets;
- a web control plane, telemetry platform, or distributed node system;
- automatic cross-CLI failover or replay;
- features copied from OpenClaw only for architectural similarity.

## Change Discipline

When proposing the next step, start from the current working loop and identify the smallest missing behavior that users can exercise immediately. State why it belongs in the gateway, what failure it fixes, and what is deliberately left out.

When evidence later contradicts these instructions, update this file explicitly as part of the same change. Do not silently grow the architecture around an obsolete assumption.
