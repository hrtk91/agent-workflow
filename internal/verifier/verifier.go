package verifier

import (
	"context"
	"fmt"
	"os/exec"
	"strings"

	"github.com/hrtk91/agent-workflow/internal/model"
)

type Input struct {
	RepoPath      string
	WorktreeDir   string
	LogDir        string
	Command       model.CommandOutput
	VerifyCommand string
}

func Verify(ctx context.Context, input Input) (model.VerificationOutput, error) {
	var reasons []string
	if input.Command.ExitCode != 0 {
		reasons = append(reasons, fmt.Sprintf("agent command exited with %d", input.Command.ExitCode))
	}
	if input.Command.TimedOut {
		reasons = append(reasons, "agent command timed out")
	}
	status, err := gitStatus(ctx, input.WorktreeDir)
	if err != nil {
		return model.VerificationOutput{}, err
	}
	if strings.TrimSpace(status) == "" {
		reasons = append(reasons, "no source changes were produced")
	}
	if strings.TrimSpace(input.VerifyCommand) == "" {
		reasons = append(reasons, "verifyCommand is required before a workflow can be marked done")
	} else if err := runVerifyCommand(ctx, input.WorktreeDir, input.VerifyCommand); err != nil {
		reasons = append(reasons, err.Error())
	}
	if containsNoVerify(input.Command.StdoutPath) || containsNoVerify(input.Command.StderrPath) {
		reasons = append(reasons, "`--no-verify` appeared in command output")
	}
	return model.VerificationOutput{
		Passed:  len(reasons) == 0,
		Reasons: reasons,
	}, nil
}

func runVerifyCommand(ctx context.Context, dir string, command string) error {
	cmd := exec.CommandContext(ctx, "bash", "-lc", command)
	cmd.Dir = dir
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("verifyCommand failed: %w: %s", err, trimOutput(string(out), 800))
	}
	return nil
}

func trimOutput(s string, limit int) string {
	s = strings.TrimSpace(s)
	if len(s) <= limit {
		return s
	}
	return s[:limit] + "...(truncated)"
}

func gitStatus(ctx context.Context, dir string) (string, error) {
	out, err := exec.CommandContext(ctx, "git", "-C", dir, "status", "--porcelain").CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("git status failed: %w: %s", err, string(out))
	}
	return string(out), nil
}

func containsNoVerify(path string) bool {
	out, err := exec.Command("rg", "--fixed-strings", "--quiet", "--", "--no-verify", path).CombinedOutput()
	if err == nil {
		return true
	}
	_ = out
	return false
}
