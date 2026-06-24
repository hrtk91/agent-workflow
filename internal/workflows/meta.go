package workflows

import (
	"context"

	hatchet "github.com/hatchet-dev/hatchet/sdks/go"

	"github.com/hrtk91/agent-workflow/internal/meta"
	"github.com/hrtk91/agent-workflow/internal/model"
	"github.com/hrtk91/agent-workflow/internal/runner"
)

func NewMetaWorkflow(client *hatchet.Client, name string) *hatchet.Workflow {
	workflow := client.NewWorkflow(
		name,
		hatchet.WithWorkflowDescription("Meta-workflow for creating takt workflow definitions as draft patches with strict checks."),
	)

	prepare := workflow.NewTask("prepare-draft-worktree", func(ctx hatchet.Context, input model.MetaWorkflowInput) (model.PrepareOutput, error) {
		return runner.Prepare(ctx, runner.PrepareInput{
			RepoPath: input.RepoPath,
			Label:    input.WorkflowLabel,
		})
	})

	generate := workflow.NewTask("generate-takt-workflow-patch", func(ctx hatchet.Context, input model.MetaWorkflowInput) (model.MetaStepOutput, error) {
		var prepared model.PrepareOutput
		if err := ctx.ParentOutput(prepare, &prepared); err != nil {
			return model.MetaStepOutput{}, err
		}
		return meta.GeneratePatch(ctx, input, prepared)
	}, hatchet.WithParents(prepare))

	staticCheck := workflow.NewTask("run-static-checks", func(ctx hatchet.Context, input model.MetaWorkflowInput) (model.MetaStepOutput, error) {
		var prepared model.PrepareOutput
		if err := ctx.ParentOutput(prepare, &prepared); err != nil {
			return model.MetaStepOutput{}, err
		}
		return meta.StaticCheck(ctx, prepared)
	}, hatchet.WithParents(generate))

	unitTest := workflow.NewTask("run-go-tests", func(ctx hatchet.Context, input model.MetaWorkflowInput) (model.MetaStepOutput, error) {
		var prepared model.PrepareOutput
		if err := ctx.ParentOutput(prepare, &prepared); err != nil {
			return model.MetaStepOutput{}, err
		}
		return meta.UnitTest(ctx, prepared)
	}, hatchet.WithParents(generate))

	dryRun := workflow.NewTask("run-dry-run", func(ctx hatchet.Context, input model.MetaWorkflowInput) (model.MetaStepOutput, error) {
		var prepared model.PrepareOutput
		if err := ctx.ParentOutput(prepare, &prepared); err != nil {
			return model.MetaStepOutput{}, err
		}
		return meta.DryRun(ctx, input, prepared)
	}, hatchet.WithParents(staticCheck, unitTest))

	failureInjection := workflow.NewTask("failure-injection", func(ctx hatchet.Context, input model.MetaWorkflowInput) (model.MetaStepOutput, error) {
		var prepared model.PrepareOutput
		if err := ctx.ParentOutput(prepare, &prepared); err != nil {
			return model.MetaStepOutput{}, err
		}
		return meta.FailureInjection(ctx, input, prepared)
	}, hatchet.WithParents(dryRun))

	dockerCleanup := workflow.NewTask("docker-label-cleanup", func(ctx hatchet.Context, input model.MetaWorkflowInput) (model.MetaStepOutput, error) {
		var prepared model.PrepareOutput
		if err := ctx.ParentOutput(prepare, &prepared); err != nil {
			return model.MetaStepOutput{}, err
		}
		return meta.DockerCleanup(ctx, input, prepared)
	}, hatchet.WithParents(failureInjection))

	report := workflow.NewTask("produce-review-report", func(ctx hatchet.Context, input model.MetaWorkflowInput) (model.MetaFinalOutput, error) {
		var prepared model.PrepareOutput
		if err := ctx.ParentOutput(prepare, &prepared); err != nil {
			return model.MetaFinalOutput{}, err
		}
		steps := make([]model.MetaStepOutput, 0, 6)
		var step model.MetaStepOutput
		if err := ctx.ParentOutput(generate, &step); err != nil {
			return model.MetaFinalOutput{}, err
		}
		steps = append(steps, step)
		if err := ctx.ParentOutput(staticCheck, &step); err != nil {
			return model.MetaFinalOutput{}, err
		}
		steps = append(steps, step)
		if err := ctx.ParentOutput(unitTest, &step); err != nil {
			return model.MetaFinalOutput{}, err
		}
		steps = append(steps, step)
		if err := ctx.ParentOutput(dryRun, &step); err != nil {
			return model.MetaFinalOutput{}, err
		}
		steps = append(steps, step)
		if err := ctx.ParentOutput(failureInjection, &step); err != nil {
			return model.MetaFinalOutput{}, err
		}
		steps = append(steps, step)
		if err := ctx.ParentOutput(dockerCleanup, &step); err != nil {
			return model.MetaFinalOutput{}, err
		}
		steps = append(steps, step)
		return meta.WriteReport(ctx, input, prepared, steps)
	}, hatchet.WithParents(generate, staticCheck, unitTest, dryRun, failureInjection, dockerCleanup))

	_ = workflow.NewTask("cleanup", func(ctx hatchet.Context, input model.MetaWorkflowInput) (model.MetaFinalOutput, error) {
		var final model.MetaFinalOutput
		if err := ctx.ParentOutput(report, &final); err != nil {
			return model.MetaFinalOutput{}, err
		}
		var prepared model.PrepareOutput
		if err := ctx.ParentOutput(prepare, &prepared); err != nil {
			return model.MetaFinalOutput{}, err
		}
		return final, runner.Cleanup(ctx, prepared.RepoPath, prepared.WorktreeDir)
	}, hatchet.WithParents(report))

	return workflow
}

func RunMetaLocal(ctx context.Context, input model.MetaWorkflowInput) (model.MetaFinalOutput, error) {
	prepared, err := runner.Prepare(ctx, runner.PrepareInput{
		RepoPath: input.RepoPath,
		Label:    input.WorkflowLabel,
	})
	if err != nil {
		return model.MetaFinalOutput{}, err
	}
	defer func() { _ = runner.Cleanup(context.Background(), prepared.RepoPath, prepared.WorktreeDir) }()

	steps := make([]model.MetaStepOutput, 0, 6)
	step, err := meta.GeneratePatch(ctx, input, prepared)
	if err != nil {
		return model.MetaFinalOutput{}, err
	}
	steps = append(steps, step)
	step, err = meta.StaticCheck(ctx, prepared)
	if err != nil {
		return model.MetaFinalOutput{}, err
	}
	steps = append(steps, step)
	step, err = meta.UnitTest(ctx, prepared)
	if err != nil {
		return model.MetaFinalOutput{}, err
	}
	steps = append(steps, step)
	step, err = meta.DryRun(ctx, input, prepared)
	if err != nil {
		return model.MetaFinalOutput{}, err
	}
	steps = append(steps, step)
	step, err = meta.FailureInjection(ctx, input, prepared)
	if err != nil {
		return model.MetaFinalOutput{}, err
	}
	steps = append(steps, step)
	step, err = meta.DockerCleanup(ctx, input, prepared)
	if err != nil {
		return model.MetaFinalOutput{}, err
	}
	steps = append(steps, step)
	return meta.WriteReport(ctx, input, prepared, steps)
}
