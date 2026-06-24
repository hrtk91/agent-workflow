package meta

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/hrtk91/agent-workflow/internal/model"
)

func GeneratePatch(ctx context.Context, input model.MetaWorkflowInput, prepared model.PrepareOutput) (model.MetaStepOutput, error) {
	if strings.TrimSpace(input.GeneratorCommand) == "" {
		return failedStep("generate_takt_workflow_patch", "generatorCommand is required"), nil
	}
	step, err := runShellStep(ctx, prepared, "generate_takt_workflow_patch", input.GeneratorCommand)
	if err != nil {
		return model.MetaStepOutput{}, err
	}
	status, err := gitStatus(ctx, prepared.WorktreeDir)
	if err != nil {
		return model.MetaStepOutput{}, err
	}
	if strings.TrimSpace(status) == "" {
		step.Passed = false
		step.Notes = append(step.Notes, "generator command produced no source changes")
	}
	return step, nil
}

func StaticCheck(ctx context.Context, prepared model.PrepareOutput) (model.MetaStepOutput, error) {
	return runShellStep(ctx, prepared, "takt_workflow_doctor", "takt workflow doctor .takt/workflows")
}

func UnitTest(ctx context.Context, prepared model.PrepareOutput) (model.MetaStepOutput, error) {
	return runShellStep(ctx, prepared, "takt_prompt_preview", "takt prompt >/dev/null")
}

func DryRun(ctx context.Context, input model.MetaWorkflowInput, prepared model.PrepareOutput) (model.MetaStepOutput, error) {
	step := model.MetaStepOutput{
		Name:    "draft_registration_check",
		Command: "ensure generated workflow exists and remains draft-only",
		Passed:  true,
	}
	if strings.TrimSpace(input.TargetWorkflowName) == "" {
		step.Passed = false
		step.Notes = append(step.Notes, "targetWorkflowName is required")
		return step, nil
	}
	if !workflowExists(prepared.WorktreeDir, input.TargetWorkflowName) {
		step.Passed = false
		step.Notes = append(step.Notes, "target workflow file was not generated under .takt/workflows")
	}
	step.Notes = append(step.Notes, "production schedule registration is not supported by this meta-workflow")
	return step, nil
}

func FailureInjection(ctx context.Context, input model.MetaWorkflowInput, prepared model.PrepareOutput) (model.MetaStepOutput, error) {
	step := model.MetaStepOutput{
		Name:    "failure_injection",
		Command: "verify invalid workflow definitions are rejected by takt workflow doctor",
		Passed:  true,
	}
	tmp := filepath.Join(prepared.WorktreeDir, ".takt", "workflows", "agent-workflow-invalid.yaml")
	if err := os.WriteFile(tmp, []byte("name: [invalid\n"), 0o644); err != nil {
		return model.MetaStepOutput{}, err
	}
	caseStep, err := runArgsStep(ctx, prepared, "failure_invalid_takt_workflow", []string{"takt", "workflow", "doctor", tmp})
	if err != nil {
		return model.MetaStepOutput{}, err
	}
	if caseStep.ExitCode == 0 {
		step.Passed = false
		step.Notes = append(step.Notes, "invalid workflow passed takt workflow doctor")
	} else {
		step.Notes = append(step.Notes, "invalid workflow was rejected")
	}
	return step, nil
}

func DockerCleanup(ctx context.Context, input model.MetaWorkflowInput, prepared model.PrepareOutput) (model.MetaStepOutput, error) {
	if !input.RequireDockerCleanup {
		return model.MetaStepOutput{
			Name:    "docker_label_cleanup",
			Command: "go run ./cmd/worker -mode cleanup-docker",
			Passed:  true,
			Notes:   []string{"skipped because requireDockerCleanup=false"},
		}, nil
	}
	step, err := runArgsStep(ctx, prepared, "docker_label_cleanup", []string{"go", "run", "./cmd/worker", "-mode", "cleanup-docker"})
	if err != nil {
		return model.MetaStepOutput{}, err
	}
	if step.ExitCode != 0 {
		return step, nil
	}
	out, err := exec.CommandContext(ctx, "docker", "ps", "-aq", "--filter", "label=agent-workflow=true").CombinedOutput()
	if err != nil {
		step.Passed = false
		step.Notes = append(step.Notes, fmt.Sprintf("docker label check failed: %v: %s", err, strings.TrimSpace(string(out))))
		return step, nil
	}
	if strings.TrimSpace(string(out)) != "" {
		step.Passed = false
		step.Notes = append(step.Notes, "labelled agent-workflow containers remain after cleanup")
	}
	return step, nil
}

func WriteReport(ctx context.Context, input model.MetaWorkflowInput, prepared model.PrepareOutput, steps []model.MetaStepOutput) (model.MetaFinalOutput, error) {
	patchPath := filepath.Join(prepared.LogDir, "draft.patch")
	if err := writeGitDiff(ctx, prepared.WorktreeDir, patchPath); err != nil {
		return model.MetaFinalOutput{}, err
	}

	passed := true
	var reasons []string
	for _, step := range steps {
		if !step.Passed {
			passed = false
			reasons = append(reasons, step.Name+" failed")
		}
	}
	status := "draft"
	if !passed {
		status = "rejected"
	}

	reportPath := filepath.Join(prepared.LogDir, "meta-review.md")
	draftPath := filepath.Join(prepared.LogDir, "draft.json")
	out := model.MetaFinalOutput{
		RunID:       prepared.RunID,
		Passed:      passed,
		Status:      status,
		ReportPath:  reportPath,
		PatchPath:   patchPath,
		DraftPath:   draftPath,
		Steps:       steps,
		Reasons:     reasons,
		WorktreeDir: prepared.WorktreeDir,
	}
	if err := writeDraftRecord(input, out); err != nil {
		return model.MetaFinalOutput{}, err
	}
	if err := writeReviewReport(input, out); err != nil {
		return model.MetaFinalOutput{}, err
	}
	return out, nil
}

type failureCase struct {
	name          string
	args          []string
	wantSummaryOK bool
}

func runShellStep(ctx context.Context, prepared model.PrepareOutput, name string, command string) (model.MetaStepOutput, error) {
	return runArgsStep(ctx, prepared, name, []string{"bash", "-lc", command})
}

func runArgsStep(ctx context.Context, prepared model.PrepareOutput, name string, args []string) (model.MetaStepOutput, error) {
	stdoutPath := filepath.Join(prepared.LogDir, sanitizeName(name)+"-stdout.log")
	stderrPath := filepath.Join(prepared.LogDir, sanitizeName(name)+"-stderr.log")
	stdout, err := os.Create(stdoutPath)
	if err != nil {
		return model.MetaStepOutput{}, err
	}
	defer stdout.Close()
	stderr, err := os.Create(stderrPath)
	if err != nil {
		return model.MetaStepOutput{}, err
	}
	defer stderr.Close()

	cmd := exec.CommandContext(ctx, args[0], args[1:]...)
	cmd.Dir = prepared.WorktreeDir
	cmd.Stdout = stdout
	cmd.Stderr = stderr
	err = cmd.Run()
	exitCode := 0
	if err != nil {
		var exitErr *exec.ExitError
		if ok := errorsAs(err, &exitErr); ok {
			exitCode = exitErr.ExitCode()
		} else {
			return model.MetaStepOutput{}, err
		}
	}
	return model.MetaStepOutput{
		Name:       name,
		Command:    strings.Join(args, " "),
		ExitCode:   exitCode,
		StdoutPath: stdoutPath,
		StderrPath: stderrPath,
		Passed:     exitCode == 0,
	}, nil
}

func failedStep(name string, note string) model.MetaStepOutput {
	return model.MetaStepOutput{
		Name:   name,
		Passed: false,
		Notes:  []string{note},
	}
}

func workflowExists(root string, name string) bool {
	name = strings.TrimSpace(name)
	if name == "" {
		return false
	}
	candidates := []string{name}
	if !strings.HasSuffix(name, ".yaml") && !strings.HasSuffix(name, ".yml") {
		candidates = append(candidates, name+".yaml", name+".yml")
	}
	for _, candidate := range candidates {
		paths := []string{candidate}
		if !filepath.IsAbs(candidate) {
			paths = append(paths, filepath.Join(root, ".takt", "workflows", candidate))
		}
		for _, path := range paths {
			if _, err := os.Stat(path); err == nil {
				return true
			}
		}
	}
	return false
}

func gitStatus(ctx context.Context, dir string) (string, error) {
	out, err := exec.CommandContext(ctx, "git", "-C", dir, "status", "--porcelain").CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("git status failed: %w: %s", err, string(out))
	}
	return string(out), nil
}

func writeGitDiff(ctx context.Context, dir string, path string) error {
	if out, err := exec.CommandContext(ctx, "git", "-C", dir, "add", "-N", ".").CombinedOutput(); err != nil {
		return fmt.Errorf("git add -N failed: %w: %s", err, string(out))
	}
	out, err := exec.CommandContext(ctx, "git", "-C", dir, "diff", "--binary").CombinedOutput()
	if err != nil {
		return fmt.Errorf("git diff failed: %w: %s", err, string(out))
	}
	return os.WriteFile(path, out, 0o644)
}

func writeDraftRecord(input model.MetaWorkflowInput, out model.MetaFinalOutput) error {
	type draft struct {
		Status             string   `json:"status"`
		RunID              string   `json:"runId"`
		TargetWorkflowName string   `json:"targetWorkflowName"`
		Request            string   `json:"request"`
		PatchPath          string   `json:"patchPath"`
		ReportPath         string   `json:"reportPath"`
		AutoSchedule       bool     `json:"autoSchedule"`
		Reasons            []string `json:"reasons"`
	}
	data, err := json.MarshalIndent(draft{
		Status:             out.Status,
		RunID:              out.RunID,
		TargetWorkflowName: input.TargetWorkflowName,
		Request:            input.Request,
		PatchPath:          out.PatchPath,
		ReportPath:         out.ReportPath,
		AutoSchedule:       false,
		Reasons:            out.Reasons,
	}, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(out.DraftPath, append(data, '\n'), 0o644)
}

func writeReviewReport(input model.MetaWorkflowInput, out model.MetaFinalOutput) error {
	var buf bytes.Buffer
	fmt.Fprintf(&buf, "# meta-workflow review %s\n\n", out.RunID)
	fmt.Fprintf(&buf, "- target_workflow: `%s`\n", input.TargetWorkflowName)
	fmt.Fprintf(&buf, "- status: `%s`\n", out.Status)
	fmt.Fprintf(&buf, "- passed: `%t`\n", out.Passed)
	fmt.Fprintf(&buf, "- patch: `%s`\n", out.PatchPath)
	fmt.Fprintf(&buf, "- draft: `%s`\n", out.DraftPath)
	fmt.Fprintf(&buf, "- auto_schedule: `false`\n")
	fmt.Fprintf(&buf, "- worktree: `%s`\n", out.WorktreeDir)
	if strings.TrimSpace(input.Request) != "" {
		fmt.Fprintf(&buf, "\n## request\n\n%s\n", input.Request)
	}
	if len(out.Reasons) > 0 {
		fmt.Fprintln(&buf, "\n## reasons")
		for _, reason := range out.Reasons {
			fmt.Fprintf(&buf, "- %s\n", reason)
		}
	}
	fmt.Fprintln(&buf, "\n## steps")
	for _, step := range out.Steps {
		fmt.Fprintf(&buf, "\n### %s\n\n", step.Name)
		fmt.Fprintf(&buf, "- passed: `%t`\n", step.Passed)
		if step.Command != "" {
			fmt.Fprintf(&buf, "- command: `%s`\n", step.Command)
		}
		if step.StdoutPath != "" {
			fmt.Fprintf(&buf, "- stdout: `%s`\n", step.StdoutPath)
		}
		if step.StderrPath != "" {
			fmt.Fprintf(&buf, "- stderr: `%s`\n", step.StderrPath)
		}
		for _, note := range step.Notes {
			fmt.Fprintf(&buf, "- note: %s\n", note)
		}
	}
	return os.WriteFile(out.ReportPath, buf.Bytes(), 0o644)
}

func lastNonEmptyLine(path string) string {
	data, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	lines := strings.Split(string(data), "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		line := strings.TrimSpace(lines[i])
		if line != "" {
			return line
		}
	}
	return ""
}

func summaryHas(path string, needle string) (bool, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return false, fmt.Errorf("read summary %s: %w", path, err)
	}
	return strings.Contains(string(data), needle), nil
}

func hasAgentWorkflowWorktree(ctx context.Context, repoPath string) bool {
	out, err := exec.CommandContext(ctx, "git", "-C", repoPath, "worktree", "list", "--porcelain").CombinedOutput()
	if err != nil {
		return true
	}
	for _, line := range strings.Split(string(out), "\n") {
		if strings.HasPrefix(line, "worktree ") && strings.Contains(line, "/tmp/agent-workflow/") {
			return true
		}
	}
	return false
}

func sanitizeName(s string) string {
	s = strings.TrimSpace(strings.ToLower(s))
	if s == "" {
		return "step"
	}
	replacer := strings.NewReplacer(" ", "-", "/", "-", ":", "-", "_", "-", ".", "-")
	return replacer.Replace(s)
}

func errorsAs(err error, target any) bool {
	return errors.As(err, target)
}
