# agent-workflow

Hatchet-based workflow harness for local agent work.

The goal is not to make the agent smarter. The goal is to stop treating an
agent response, a queued run, or a created PR as proof that the workflow is
done.

## Shape

```text
Hatchet
  queue / retry / durable state / cron / dashboard / OpenTelemetry

Go worker
  lock target repo
  run takt
  verify takt outcome
  report

Meta-workflow
  generate takt workflow patch
  takt workflow doctor
  prompt preview
  failure injection
  draft report
  cleanup

Hermes
  optional UI / trigger / notification only
```

## Workflow States

The main workflow is `run-takt-workflow`. It keeps these boundaries explicit:

- `prepare-takt-run`: create a run directory and record the target repo state.
- `run-takt`: acquire a lock, check preconditions, and run `takt`.
- `verify-takt-run`: inspect exit code, timeout, explicit verifier, and known unsafe output such as
  `--no-verify`.
- `report-takt-run`: write a summary file under
  `~/.local/state/agent-workflow/runs`.

`takt run` success is not workflow success. Only `verify-takt-run` can mark a
run as done. A run without `-verify-command` is never marked done.

The older `agent` workflow is still available for direct command experiments,
but the default path is to let `takt` do the actual agentic work and let
Hatchet supervise it.

## Meta-Workflow

`create-workflow-definition` is the workflow for creating takt workflow
definitions. It exists so Hermes can request or generate `.takt/workflows/*.yaml`
without being trusted to decide that the generated workflow is production-ready.

The meta-workflow:

- creates a detached worktree of the target repo.
- runs `-meta-generator-command` in that worktree.
- requires the generator to produce a git diff.
- runs `takt workflow doctor .takt/workflows`.
- previews takt prompts.
- injects a malformed workflow and confirms `takt workflow doctor` rejects it.
- optionally confirms Docker label cleanup with
  `-meta-require-docker-cleanup`.
- writes `meta-review.md`, `draft.patch`, and `draft.json` under
  `~/.local/state/agent-workflow/runs/<run-id>`.

The draft record always has `autoSchedule: false`. This tool does not merge,
deploy, or register production schedules automatically.

## Docker

Docker is optional in v0. If `-docker` is set, the worker runs:

```text
docker run --rm \
  --label agent-workflow=true \
  --label agent-workflow.run-id=<run-id> \
  --label agent-workflow.workflow=<workflow> \
  -v <worktree>:/workspace \
  -w /workspace \
  agent-workflow-runner:latest \
  bash -lc <command>
```

Containers are labelled so cleanup can target only this system. The image is
not built by this repo yet. The worker uses `--rm` and bind mounts rather than
named volumes. If a run is interrupted, clean labelled containers with:

```bash
go run ./cmd/worker -mode cleanup-docker
```

Do not run broad `docker system prune` from this tool. Images and unrelated
volumes are intentionally left alone.

## microsandbox

This host is WSL2 and has no `/dev/kvm`, so microsandbox is not a viable local
executor here. Keep isolation behind an executor boundary. Docker is the v0
fallback.

## Run

Start Hatchet locally first:

```bash
hatchet server start
```

Export the client token shown by Hatchet, then start the worker:

```bash
go run ./cmd/worker -mode worker
```

Trigger a takt workflow:

```bash
go run ./cmd/worker \
  -mode trigger \
  -kind takt \
  -repo /home/h-taminato/repos/eb-temp \
  -takt-args '--pipeline --skip-git --quiet --workflow default --issue 123' \
  -timeout-seconds 7200 \
  -verify-command 'test -n "$(find .takt/runs -mindepth 1 -maxdepth 1 -type d -printf x -quit)"'
```

Dry-run without Hatchet:

```bash
go run ./cmd/worker \
  -mode dry-run \
  -kind takt \
  -repo /home/h-taminato/repos/eb-temp \
  -takt-args '--version' \
  -require-clean-repo=false \
  -verify-command 'takt --version >/dev/null'
```

If you intentionally want to consume `.takt/tasks.yaml`, pass `-takt-args
'run --ignore-exceed'` explicitly. That path can create takt-managed
worktrees, so the verifier fails by default if new git worktrees remain after
the run. Use `-allow-new-worktrees` only when leaving those worktrees around is
the intended outcome.

Dry-run the meta-workflow without Hatchet:

```bash
go run ./cmd/worker \
  -mode dry-run \
  -kind meta \
  -meta-repo /home/h-taminato/repos/eb-temp \
  -meta-target-workflow ebtemp-next-workflow \
  -meta-request 'Create an eb-temp takt workflow draft' \
  -meta-generator-command 'cp .takt/workflows/default.yaml .takt/workflows/ebtemp-next-workflow.yaml'
```
