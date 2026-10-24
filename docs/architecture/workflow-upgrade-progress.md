# Harness workflow upgrade

Branch: `codex/harness-workflow-upgrade`.

Scope: document sections 2.1 (multi-turn execution), 2.2 (local HITL),
2.3 (checkpoint, interruption, workspace drift), 2.4 (memory write/reuse).
Keep the CLI, provider adapters, file-backed storage and existing public entry points.
No web service, distributed scheduler, new network tool or OS sandbox is included.
File path confinement remains the existing boundary; shell is still approval controlled.

Ordered milestones:

1. Execution: common parameter rules, runtime phases, correlated tool events,
   state-aware repeated-call detection.
2. HITL: local approval records, impact preview, saved pending action, decision audit.
3. Recovery: atomic session writes, controlled interruption, freshness execution gate,
   resumable action records and checkpoint progress.
4. Memory: structured task progress, sourced file summaries, note invalidation,
   retrieval/reuse and deterministic regression tests.

Each milestone is tested and committed separately. Historical benchmark snapshots
are preserved. New measurements must state their inputs and model type.

## Milestone 1

Implemented: common parameter validation (including XML numeric normalization),
phase/failure-phase persistence, call IDs across request/result/history/trace,
and content-version-aware repeated-read detection. Existing textual tool protocol
and CLI entry points remain compatible. Fixed Python benchmark verifiers use
the active interpreter because the baseline `python3` command fails on this Windows host.

Validation: 40 execution/evaluator tests passed. Locale assertions now check the
reported host locale rather than assuming Linux `C.UTF-8`.

## Milestone 2

Implemented: file-backed approval IDs and decisions, bounded file diffs/command
previews, expiry on target/runtime changes, persisted pending actions and local
`/approvals`, `/approve ID`, `/deny ID` commands. Approval execution is single-use;
reloaded requests are revalidated against current policy, tools and file versions.
