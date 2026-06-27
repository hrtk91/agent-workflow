# agent-workflow

Lightweight resumable runner for local agent workflows.

The goal is not to make the agent smarter. The goal is to stop treating an
agent response, a queued run, or a created PR as proof that the workflow is
done.

## Shape

```text
Hermes
  trigger / notification UI only

agent-workflow
  task packet
  queue
  per-run worktree
  executor
  QC command
  state.json / jobs.sqlite
  summary.md / trace.jsonl
```

Hermes should normally enqueue work and exit quickly. Long-running execution is
owned by `aw tick` or `aw worker`.

## Task Packets

The runner is not GitHub-issue centric. It consumes text packets:

```text
task.md
acceptance.md     optional
constraints.md    optional
context.md        optional
source.json       optional provenance
```

GitHub issues, Hermes messages, Discord text, or local notes should be converted
to this packet format before execution. The core runner only needs the text.

## Workflow States

Each run records these steps:

- `load_task`
- `create_worktree`
- `run_executor`
- `run_qc`
- `write_summary`

Step state is stored in:

```text
~/.local/state/agent-workflow/
  jobs.sqlite
  runs/<run-id>/state.json
  runs/<run-id>/summary.md
  runs/<run-id>/trace.jsonl
  runs/<run-id>/logs/*.log
  worktrees/<run-id>/repo
```

Executor success is not workflow success. The run is only `succeeded` after the
explicit QC command passes. Test failures become `qc_failed`; timeouts become
`timed_out`; missing task text or policy stops become `blocked`.

## Queue

Queue a task and return immediately:

```bash
aw enqueue \
  --repo /home/h-taminato/repos/eb-temp-hermes-runtime \
  --task-file /path/to/task.md \
  --workflow default \
  --verify-command 'mise run check-all' \
  --timeout-seconds 7200
```

Run queued work once:

```bash
aw tick --max-runs 1
```

Run queued work once and notify Hermes on failed/blocked/timed-out runs:

```bash
aw tick --max-runs 1 \
  --notify-command 'scripts/notify-hermes-workflow-summary.bashx {discord_summary} "[eb-temp workflow]"'
```

For cron dispatchers, keep job failures in `aw` state without failing the
dispatcher process:

```bash
aw tick --max-runs 1 \
  --isolate-job-failures \
  --notify-command 'scripts/notify-hermes-workflow-summary.bashx {discord_summary} "[eb-temp workflow]"'
```

Run queued work continuously:

```bash
aw worker --interval-seconds 60 --parallelism 1 --repo-parallelism 1
```

`aw worker` is the daemon-style entrypoint. It claims queued jobs in the
parent process, runs each claimed job in a child process, and records the child
exit back into `jobs.sqlite`. Keep `--repo-parallelism 1` for implementation
work unless the repo-specific workflow has been designed for concurrent
worktrees, ports, caches, Docker resources, and PR creation.

Notification commands support `{job_id}`, `{run_id}`, `{status}`,
`{summary}`, and `{discord_summary}` placeholders. By default, notifications
are sent only for `blocked`, `failed`, `qc_failed`, and `timed_out`; use
`--notify-statuses all` when a worker should also report successful runs.

Direct synchronous execution is available for smoke tests and manual use:

```bash
aw run \
  --repo /home/h-taminato/repos/eb-temp-hermes-runtime \
  --task-file /path/to/task.md \
  --workflow default \
  --verify-command 'mise run check-all'
```

Resume a failed or interrupted run:

```bash
aw resume --run-id <run-id>
```

Retry one step and all downstream steps:

```bash
aw retry --run-id <run-id> --step run_qc
```

Show recent queued jobs and runs:

```bash
aw status
```

Remove a run worktree:

```bash
aw cleanup --run-id <run-id>
```

## Watchdog and Repair Drafts

`aw watchdog scan` lists failed runs that do not yet have a repair draft:

```bash
aw watchdog scan --limit 10
```

Repair diagnosis stays outside the core runner. A human or LLM writes plain
Markdown evidence, then calls the typed CLI. The CLI generates `repair.ini`,
copies the evidence, validates the guardrails, and records the draft in
SQLite.

```bash
aw repair draft \
  --failed-run-id <run-id> \
  --title "QC fails because mise state is not writable" \
  --category runtime_env \
  --risk low \
  --proposed-action runtime_environment_patch \
  --diagnosis-file diagnosis.md \
  --evidence-file evidence.md \
  --notify-before-file notify-before.md \
  --verify-command 'bashx scripts/agent-workflow-qc.bashx'
```

Drafts are stored under:

```text
~/.local/state/agent-workflow/
  repairs/<draft-id>/repair.ini
  repairs/<draft-id>/diagnosis.md
  repairs/<draft-id>/evidence.md
  repairs/<draft-id>/notify-before.md
```

The draft command validates before returning. `aw repair validate --draft-id
<draft-id>` can be used to re-check an existing artifact. Deploy and migration
actions require `--risk high`, `--environment`, `--healthcheck-command`, and a
non-empty `--rollback-plan-file`.

## Merge Gates

`aw merge-gate` evaluates a GitHub PR and writes three files:

```text
merge-decision.json
merge-gate.md
hermes-discord-summary.md
```

The gate can use GitHub checks, local QC in a detached PR-head worktree, or
both. With no GitHub checks and no local QC, it blocks by default.

```bash
aw merge-gate \
  --repo hrtk91/eb-temp \
  --pr 853 \
  --repo-path /home/h-taminato/repos/eb-temp-hermes-runtime \
  --verify-command 'bashx scripts/agent-workflow-qc.bashx'
```

`aw merge` consumes a fresh `MERGE_APPROVED` decision. It re-checks the PR head
SHA, base SHA, draft state, merge state, and live checks before doing anything.
The default is dry-run; pass `--execute` to merge.

```bash
aw merge --decision /path/to/merge-decision.json
aw merge --decision /path/to/merge-decision.json --execute
```

## OpenTelemetry

Every step writes one OpenTelemetry-shaped span record to `trace.jsonl`. This is
always available without external services and includes:

- `trace_id`
- `span_id`
- step name
- duration
- status code
- exit code
- timeout flag
- stdout/stderr log paths

The JSONL format is intentionally close to OTel span fields so an OTLP exporter
can be added without changing the runner state model.

## Tests

```bash
bashx scripts/test.bashx
```

The Python tests use a fake git repo and fake executor binary, so they do not
call an external model.
