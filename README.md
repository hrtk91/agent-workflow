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
explicit QC command passes. If QC fails, the runner returns to the executor and
adds the QC failure context to the task up to five times; the workflow is still
`qc_failed` if QC is not green after that loop. Timeouts become `timed_out`;
missing task text or policy stops become `blocked`.

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
exit back into `jobs.sqlite`. On startup it marks pre-existing `running` jobs
as failed, because a single daemon restart means those children are no longer
owned. Keep `--repo-parallelism 1` for implementation work unless the
repo-specific workflow has been designed for concurrent worktrees, ports,
caches, Docker resources, and PR creation.

Notification commands support `{job_id}`, `{run_id}`, `{status}`,
`{summary}`, and `{discord_summary}` placeholders. By default, notifications
are sent only for `blocked`, `failed`, `qc_failed`, and `timed_out`; use
`--notify-statuses all` when a worker should also report successful runs.

The internal Discord summary is mechanical until a matching notification is
sent. At send time, the notification adapter generates the final text with the
provider selected in `~/.config/agent-workflow/config.toml`. Create and inspect
the settings with:

```bash
aw config init
aw config show
```

Codex is the default. Add named provider tables for subscription-backed CLIs
such as Claude or Grok. The command reads the prompt from stdin and writes the
notification to stdout:

```toml
[notification]
provider = "claude"

[notification.providers.codex]
kind = "codex"
command = ["codex", "exec"]
timeout_seconds = 120
model = "gpt-5.6-luna"
reasoning_effort = "medium"

[notification.providers.claude]
kind = "command"
command = ["claude", "--print"]
timeout_seconds = 120
```

Pass `--config-file <path>` before the subcommand to use another file.
`AGENT_WORKFLOW_NOTIFICATION_*` environment variables override the loaded file
for one process. Codex runs in a temporary empty directory with a read-only
sandbox, no inherited shell environment, no loaded user rules or configuration,
and no persisted session. Custom provider commands must provide their own
equivalent isolation.

Enable diagnosis-loop dispatch when the worker should enqueue a diagnosis task
after a normal workflow reaches a terminal failure:

```bash
aw worker --interval-seconds 60 --parallelism 1 --repo-parallelism 1 \
  --auto-repair \
  --repair-model gpt-5.5
```

The diagnosis job is a normal queued `aw` run with `purpose=repair`. It reads
the failed run summary, logs, trace, and worktree, then must call
`aw repair draft` to create a validated handoff artifact. Diagnosis jobs do not
recursively create more diagnosis jobs, and diagnosis-job failures are not sent
through the normal workflow notification command. When a diagnosis job succeeds
and leaves a validated draft, `aw` immediately enqueues one `purpose=repair_action`
job for non-`human_needed` actions. That action job is where the actual repo or
runtime repair is implemented and QC must turn green.

By default, auto-repair only reacts to failures produced by the current
worker/tick execution. It does not backfill old failed runs on startup. Use
`--repair-scan-existing` only for deliberate manual backfill, because it may
enqueue many historical failures.

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

## Local Analytics

Tag runs when model and task-type comparisons are needed:

```bash
aw run \
  --repo /path/to/repo \
  --task-file /path/to/task.md \
  --verify-command 'mise run check-all' \
  --provider openai \
  --model gpt-example \
  --task-type bug_fix
```

Report first-pass QC, eventual QC, QC attempts, elapsed time, and changed lines from `jobs.sqlite`:

```bash
aw report
aw report --group-by model,task_type
aw report --repo /path/to/repo --since 2026-07-01
aw report --group-by model,task_type --format json
```

`aw report` opens SQLite in read-only mode and never creates or updates analytics rows. If the database or analytics schema does not exist yet, it returns an empty report without creating either one. New runs are recorded as they execute. The analytics tables are:

- `run_metrics`: run configuration, QC outcomes, duration, task identity, and final change size
- `step_attempts`: one row per executor, QC, or other workflow step attempt
- `analytics_schema_migrations`: analytics schema version history

Repair and repair-action runs are excluded by default. Pass `--include-repair` to include them.

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
aw merge --decision /path/to/merge-decision.json \
  --repo-path /path/to/repo \
  --verify-command 'bashx scripts/agent-workflow-qc.bashx'
aw merge --decision /path/to/merge-decision.json --execute \
  --repo-path /path/to/repo \
  --verify-command 'bashx scripts/agent-workflow-qc.bashx'
```

When the live PR has no GitHub checks, `aw merge` re-runs the supplied local QC
at the approved head SHA. Use `--allow-no-checks` only to explicitly skip that
re-check.

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

`trace.jsonl` is always retained as the durable local trace. When a trace OTLP
endpoint is configured, the same invocation is also exported as one
`agent_workflow.run` span with a child span for every step attempt:

```bash
python3 -m pip install '.[otel]'
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
aw run --repo /path/to/repo --task /path/to/task.md --workflow implementation --verify 'pytest'
```

The root and step spans include the requested model, provider, task type,
workflow, run status, attempt number, command result, and timeout state. A
resume or retry creates a new run span and exports only the attempts executed by
that invocation. Set `OTEL_TRACES_EXPORTER=none` to retain local JSONL without
remote trace export.

Export a grouped SQLite report as OpenTelemetry gauges over OTLP/HTTP:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
aw report --group-by model,task_type --export-otel
```

The export includes run/QC counts, first-pass and eventual QC rates, median QC
attempts, median elapsed time, and median changed lines. Report group values are
metric attributes. SQLite remains the durable source; OTLP export occurs only
when `--export-otel` is passed.

## Tests

```bash
bashx scripts/test.bashx
```

The Python tests use a fake git repo and fake executor binary, so they do not
call an external model.
