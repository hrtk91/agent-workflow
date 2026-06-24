package workflows

import (
	"context"
	"fmt"
	"time"

	hatchet "github.com/hatchet-dev/hatchet/sdks/go"

	"github.com/hrtk91/agent-workflow/internal/model"
	"github.com/hrtk91/agent-workflow/internal/runner"
	"github.com/hrtk91/agent-workflow/internal/verifier"
)

func NewEBTempWorkflow(client *hatchet.Client, name string) *hatchet.Workflow {
	workflow := client.NewWorkflow(
		name,
		hatchet.WithWorkflowDescription("Agent workflow wrapper for eb-temp: prepare worktree, run agent, verify, report, cleanup."),
	)

	prepare := workflow.NewTask("prepare-worktree", func(ctx hatchet.Context, input model.AgentRunInput) (model.PrepareOutput, error) {
		return runner.Prepare(ctx, runner.PrepareInput{
			RepoPath: input.RepoPath,
			Issue:    input.IssueNumber,
			Label:    input.WorkflowLabel,
		})
	})

	runAgent := workflow.NewTask("run-agent", func(ctx hatchet.Context, input model.AgentRunInput) (model.CommandOutput, error) {
		var prepared model.PrepareOutput
		if err := ctx.ParentOutput(prepare, &prepared); err != nil {
			return model.CommandOutput{}, err
		}
		if input.AgentCommand == "" {
			return model.CommandOutput{}, fmt.Errorf("agentCommand is required")
		}
		return runner.RunCommand(ctx, runner.CommandInput{
			Command:     input.AgentCommand,
			Workdir:     prepared.WorktreeDir,
			LogDir:      prepared.LogDir,
			Timeout:     timeoutFromSeconds(input.CommandTimeoutSeconds),
			UseDocker:   input.UseDocker,
			RunID:       prepared.RunID,
			RepoPath:    input.RepoPath,
			WorkflowTag: input.WorkflowLabel,
		})
	}, hatchet.WithParents(prepare))

	verify := workflow.NewTask("verify", func(ctx hatchet.Context, input model.AgentRunInput) (model.VerificationOutput, error) {
		var prepared model.PrepareOutput
		if err := ctx.ParentOutput(prepare, &prepared); err != nil {
			return model.VerificationOutput{}, err
		}
		var command model.CommandOutput
		if err := ctx.ParentOutput(runAgent, &command); err != nil {
			return model.VerificationOutput{}, err
		}
		return verifier.Verify(ctx, verifier.Input{
			RepoPath:      input.RepoPath,
			WorktreeDir:   prepared.WorktreeDir,
			LogDir:        prepared.LogDir,
			Command:       command,
			VerifyCommand: input.VerifyCommand,
		})
	}, hatchet.WithParents(prepare, runAgent))

	report := workflow.NewTask("report", func(ctx hatchet.Context, input model.AgentRunInput) (model.FinalOutput, error) {
		var prepared model.PrepareOutput
		if err := ctx.ParentOutput(prepare, &prepared); err != nil {
			return model.FinalOutput{}, err
		}
		var command model.CommandOutput
		if err := ctx.ParentOutput(runAgent, &command); err != nil {
			return model.FinalOutput{}, err
		}
		var checked model.VerificationOutput
		if err := ctx.ParentOutput(verify, &checked); err != nil {
			return model.FinalOutput{}, err
		}
		return runner.WriteSummary(prepared, command, checked)
	}, hatchet.WithParents(prepare, runAgent, verify))

	_ = workflow.NewTask("cleanup", func(ctx hatchet.Context, input model.AgentRunInput) (model.FinalOutput, error) {
		var final model.FinalOutput
		if err := ctx.ParentOutput(report, &final); err != nil {
			return model.FinalOutput{}, err
		}
		var prepared model.PrepareOutput
		if err := ctx.ParentOutput(prepare, &prepared); err != nil {
			return model.FinalOutput{}, err
		}
		return final, runner.Cleanup(ctx, prepared.RepoPath, prepared.WorktreeDir)
	}, hatchet.WithParents(report))

	return workflow
}

func RunLocal(ctx context.Context, input model.AgentRunInput) (model.FinalOutput, error) {
	prepared, err := runner.Prepare(ctx, runner.PrepareInput{
		RepoPath: input.RepoPath,
		Issue:    input.IssueNumber,
		Label:    input.WorkflowLabel,
	})
	if err != nil {
		return model.FinalOutput{}, err
	}
	defer func() { _ = runner.Cleanup(context.Background(), prepared.RepoPath, prepared.WorktreeDir) }()

	command, err := runner.RunCommand(ctx, runner.CommandInput{
		Command:     input.AgentCommand,
		Workdir:     prepared.WorktreeDir,
		LogDir:      prepared.LogDir,
		Timeout:     timeoutFromSeconds(input.CommandTimeoutSeconds),
		UseDocker:   input.UseDocker,
		RunID:       prepared.RunID,
		RepoPath:    input.RepoPath,
		WorkflowTag: input.WorkflowLabel,
	})
	if err != nil {
		return model.FinalOutput{}, err
	}
	checked, err := verifier.Verify(ctx, verifier.Input{
		RepoPath:      input.RepoPath,
		WorktreeDir:   prepared.WorktreeDir,
		LogDir:        prepared.LogDir,
		Command:       command,
		VerifyCommand: input.VerifyCommand,
	})
	if err != nil {
		return model.FinalOutput{}, err
	}
	return runner.WriteSummary(prepared, command, checked)
}

func CleanupDocker(ctx context.Context) error {
	return runner.CleanupDocker(ctx)
}

func timeoutFromSeconds(seconds int) time.Duration {
	if seconds <= 0 {
		return 0
	}
	return time.Duration(seconds) * time.Second
}
