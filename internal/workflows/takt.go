package workflows

import (
	"context"

	hatchet "github.com/hatchet-dev/hatchet/sdks/go"

	"github.com/hrtk91/agent-workflow/internal/model"
	"github.com/hrtk91/agent-workflow/internal/taktrun"
)

func NewTaktWorkflow(client *hatchet.Client, name string) *hatchet.Workflow {
	workflow := client.NewWorkflow(
		name,
		hatchet.WithWorkflowDescription("Supervisor workflow that runs takt and closes success through an external verifier."),
	)

	prepare := workflow.NewTask("prepare-takt-run", func(ctx hatchet.Context, input model.TaktRunInput) (model.RepoRunOutput, error) {
		return taktrun.Prepare(ctx, input)
	})

	runTakt := workflow.NewTask("run-takt", func(ctx hatchet.Context, input model.TaktRunInput) (model.CommandOutput, error) {
		var prepared model.RepoRunOutput
		if err := ctx.ParentOutput(prepare, &prepared); err != nil {
			return model.CommandOutput{}, err
		}
		return taktrun.Execute(ctx, input, prepared)
	}, hatchet.WithParents(prepare))

	verify := workflow.NewTask("verify-takt-run", func(ctx hatchet.Context, input model.TaktRunInput) (model.VerificationOutput, error) {
		var command model.CommandOutput
		if err := ctx.ParentOutput(runTakt, &command); err != nil {
			return model.VerificationOutput{}, err
		}
		var prepared model.RepoRunOutput
		if err := ctx.ParentOutput(prepare, &prepared); err != nil {
			return model.VerificationOutput{}, err
		}
		return taktrun.Verify(ctx, input, prepared, command)
	}, hatchet.WithParents(prepare, runTakt))

	_ = workflow.NewTask("report-takt-run", func(ctx hatchet.Context, input model.TaktRunInput) (model.FinalOutput, error) {
		var prepared model.RepoRunOutput
		if err := ctx.ParentOutput(prepare, &prepared); err != nil {
			return model.FinalOutput{}, err
		}
		var command model.CommandOutput
		if err := ctx.ParentOutput(runTakt, &command); err != nil {
			return model.FinalOutput{}, err
		}
		var checked model.VerificationOutput
		if err := ctx.ParentOutput(verify, &checked); err != nil {
			return model.FinalOutput{}, err
		}
		return taktrun.WriteSummary(ctx, prepared, command, checked)
	}, hatchet.WithParents(prepare, runTakt, verify))

	return workflow
}

func RunTaktLocal(ctx context.Context, input model.TaktRunInput) (model.FinalOutput, error) {
	prepared, err := taktrun.Prepare(ctx, input)
	if err != nil {
		return model.FinalOutput{}, err
	}
	command, err := taktrun.Execute(ctx, input, prepared)
	if err != nil {
		return model.FinalOutput{}, err
	}
	checked, err := taktrun.Verify(ctx, input, prepared, command)
	if err != nil {
		return model.FinalOutput{}, err
	}
	return taktrun.WriteSummary(ctx, prepared, command, checked)
}
