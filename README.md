# agent-workflow

Lightweight resumable runner for local agent workflows.

The goal is not to make the agent smarter. The goal is to stop treating an
agent response, a queued run, or a created PR as proof that the workflow is
done.

## Shape

```text
Hermes
  trigger / notification UI only

agent-workflow runner
  task packet
  per-run worktree
  takt pipeline
  QC command
  state.json / jobs.sqlite
  summary.md / trace.jsonl

takt
  actual agentic implementation
```

Hatchet is intentionally not required for the default path. If worker fleets,
remote machines, or a durable distributed queue become necessary later, Hatchet
can be added as an outer backend. The current focus is one issue/task being
implemented and verified reliably.

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
- `run_takt`
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

`takt` success is not workflow success. The run is only `succeeded` after the
explicit QC command passes. Test failures become `qc_failed`; timeouts become
`timed_out`; missing task text or policy stops become `blocked`.

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

## Run

Run a text task:

```bash
scripts/aw run \
  --repo /home/h-taminato/repos/eb-temp-hermes-runtime \
  --task-file /path/to/task.md \
  --workflow default \
  --verify-command 'mise run clippy' \
  --timeout-seconds 7200
```

Resume a failed or interrupted run:

```bash
scripts/aw resume --run-id <run-id>
```

Retry one step and all downstream steps:

```bash
scripts/aw retry --run-id <run-id> --step run_qc
```

Show recent runs:

```bash
scripts/aw status
```

Remove a run worktree:

```bash
scripts/aw cleanup --run-id <run-id>
```

## Tests

```bash
shx scripts/test.shx
```

The Python tests use a fake git repo and fake `takt` binary, so they do not call
an external model.

## Legacy Go Supervisor

The existing Go supervisor remains in the repo while the lightweight runner is
validated. It still supports the earlier Hatchet and dry-run experiments, but it
is not the default path for Hermes-triggered issue work.

