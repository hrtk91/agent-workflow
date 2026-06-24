package taktrun

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/hrtk91/agent-workflow/internal/model"
	"github.com/hrtk91/agent-workflow/internal/runner"
)

func Prepare(ctx context.Context, input model.TaktRunInput) (model.RepoRunOutput, error) {
	if strings.TrimSpace(input.RepoPath) == "" {
		return model.RepoRunOutput{}, fmt.Errorf("repoPath is required")
	}
	runID := runner.NewRunID()
	logDir := filepath.Join(os.Getenv("HOME"), ".local", "state", "agent-workflow", "runs", runID)
	if err := os.MkdirAll(logDir, 0o755); err != nil {
		return model.RepoRunOutput{}, err
	}
	baseRef, err := gitOutput(ctx, input.RepoPath, "rev-parse", "--verify", "HEAD")
	if err != nil {
		return model.RepoRunOutput{}, err
	}
	status, err := gitOutput(ctx, input.RepoPath, "status", "--porcelain")
	if err != nil {
		return model.RepoRunOutput{}, err
	}
	worktrees, err := worktreePaths(ctx, input.RepoPath)
	if err != nil {
		return model.RepoRunOutput{}, err
	}
	return model.RepoRunOutput{
		RepoPath:         input.RepoPath,
		RunID:            runID,
		LogDir:           logDir,
		BaseRef:          strings.TrimSpace(baseRef),
		InitialStatus:    strings.TrimSpace(status),
		InitialWorktrees: worktrees,
		LockPath:         lockPath(input),
	}, nil
}

func Execute(ctx context.Context, input model.TaktRunInput, prepared model.RepoRunOutput) (model.CommandOutput, error) {
	release, err := acquireLock(prepared.LockPath)
	if err != nil {
		return model.CommandOutput{}, err
	}
	defer release()
	if err := preflight(ctx, input); err != nil {
		return model.CommandOutput{}, err
	}
	args := strings.TrimSpace(input.TaktArgs)
	if args == "" {
		return model.CommandOutput{}, fmt.Errorf("taktArgs is required; pass pipeline args such as `--pipeline --skip-git --quiet --workflow <name> --task <text>`")
	}
	return runner.RunCommand(ctx, runner.CommandInput{
		Command:     "takt " + args,
		Workdir:     input.RepoPath,
		LogDir:      prepared.LogDir,
		Timeout:     timeoutFromSeconds(input.CommandTimeoutSeconds),
		RunID:       prepared.RunID,
		RepoPath:    input.RepoPath,
		WorkflowTag: input.WorkflowLabel,
	})
}

func Verify(ctx context.Context, input model.TaktRunInput, prepared model.RepoRunOutput, command model.CommandOutput) (model.VerificationOutput, error) {
	var reasons []string
	if command.ExitCode != 0 {
		reasons = append(reasons, fmt.Sprintf("takt command exited with %d", command.ExitCode))
	}
	if command.TimedOut {
		reasons = append(reasons, "takt command timed out")
	}
	if strings.TrimSpace(input.VerifyCommand) == "" {
		reasons = append(reasons, "verifyCommand is required before a takt run can be marked done")
	} else if err := runVerifyCommand(ctx, input.RepoPath, input.VerifyCommand); err != nil {
		reasons = append(reasons, err.Error())
	}
	if containsNoVerify(command.StdoutPath) || containsNoVerify(command.StderrPath) {
		reasons = append(reasons, "`--no-verify` appeared in takt output")
	}
	if !input.AllowNewWorktrees {
		leaked, err := newWorktrees(ctx, input.RepoPath, prepared.InitialWorktrees)
		if err != nil {
			return model.VerificationOutput{}, err
		}
		if len(leaked) > 0 {
			reasons = append(reasons, "new git worktrees remain after takt run: "+strings.Join(leaked, ", "))
		}
	}
	return model.VerificationOutput{
		Passed:  len(reasons) == 0,
		Reasons: reasons,
	}, nil
}

func WriteSummary(ctx context.Context, prepared model.RepoRunOutput, command model.CommandOutput, checked model.VerificationOutput) (model.FinalOutput, error) {
	path := filepath.Join(prepared.LogDir, "summary.md")
	finalStatus, err := gitOutput(ctx, prepared.RepoPath, "status", "--porcelain")
	if err != nil {
		finalStatus = "git status failed: " + err.Error()
	}
	var buf bytes.Buffer
	fmt.Fprintf(&buf, "# takt workflow run %s\n\n", prepared.RunID)
	fmt.Fprintf(&buf, "- repo: `%s`\n", prepared.RepoPath)
	fmt.Fprintf(&buf, "- base_ref: `%s`\n", prepared.BaseRef)
	fmt.Fprintf(&buf, "- lock: `%s`\n", prepared.LockPath)
	fmt.Fprintf(&buf, "- command: `%s`\n", command.Command)
	fmt.Fprintf(&buf, "- command_exit_code: `%d`\n", command.ExitCode)
	fmt.Fprintf(&buf, "- command_timed_out: `%t`\n", command.TimedOut)
	fmt.Fprintf(&buf, "- stdout: `%s`\n", command.StdoutPath)
	fmt.Fprintf(&buf, "- stderr: `%s`\n", command.StderrPath)
	fmt.Fprintf(&buf, "- verifier_passed: `%t`\n", checked.Passed)
	if prepared.InitialStatus != "" {
		fmt.Fprintln(&buf, "\n## initial status")
		fmt.Fprintf(&buf, "```text\n%s\n```\n", prepared.InitialStatus)
	}
	if strings.TrimSpace(finalStatus) != "" {
		fmt.Fprintln(&buf, "\n## final status")
		fmt.Fprintf(&buf, "```text\n%s\n```\n", strings.TrimSpace(finalStatus))
	}
	if len(checked.Reasons) > 0 {
		fmt.Fprintln(&buf, "\n## verifier reasons")
		for _, reason := range checked.Reasons {
			fmt.Fprintf(&buf, "- %s\n", reason)
		}
	}
	if err := os.WriteFile(path, buf.Bytes(), 0o644); err != nil {
		return model.FinalOutput{}, err
	}
	return model.FinalOutput{
		RunID:       prepared.RunID,
		Done:        checked.Passed,
		SummaryPath: path,
	}, nil
}

func newWorktrees(ctx context.Context, repoPath string, initial []string) ([]string, error) {
	current, err := worktreePaths(ctx, repoPath)
	if err != nil {
		return nil, err
	}
	known := make(map[string]bool, len(initial))
	for _, path := range initial {
		known[path] = true
	}
	var leaked []string
	for _, path := range current {
		if !known[path] {
			leaked = append(leaked, path)
		}
	}
	return leaked, nil
}

func worktreePaths(ctx context.Context, repoPath string) ([]string, error) {
	out, err := gitOutput(ctx, repoPath, "worktree", "list", "--porcelain")
	if err != nil {
		return nil, err
	}
	var paths []string
	for _, line := range strings.Split(out, "\n") {
		if strings.HasPrefix(line, "worktree ") {
			paths = append(paths, strings.TrimSpace(strings.TrimPrefix(line, "worktree ")))
		}
	}
	return paths, nil
}

func preflight(ctx context.Context, input model.TaktRunInput) error {
	if _, err := exec.LookPath("takt"); err != nil {
		return fmt.Errorf("takt command not found: %w", err)
	}
	if _, err := os.Stat(filepath.Join(input.RepoPath, ".git")); err != nil {
		return fmt.Errorf("repoPath is not a git checkout: %s", input.RepoPath)
	}
	if _, err := os.Stat(filepath.Join(input.RepoPath, ".takt")); err != nil {
		return fmt.Errorf("repoPath has no .takt directory: %s", input.RepoPath)
	}
	if input.RequireCleanRepo {
		status, err := gitOutput(ctx, input.RepoPath, "status", "--porcelain")
		if err != nil {
			return err
		}
		if strings.TrimSpace(status) != "" {
			return fmt.Errorf("repository has uncommitted changes; refusing to run takt")
		}
	}
	return nil
}

func acquireLock(path string) (func(), error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, err
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o644)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		_ = f.Close()
		return nil, fmt.Errorf("another takt workflow is already running: %s", path)
	}
	return func() {
		_ = syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
		_ = f.Close()
	}, nil
}

func lockPath(input model.TaktRunInput) string {
	if strings.TrimSpace(input.LockPath) != "" {
		return input.LockPath
	}
	slug := strings.NewReplacer("/", "-", " ", "-", ":", "-", ".", "-").Replace(strings.Trim(input.RepoPath, "/"))
	if slug == "" {
		slug = "default"
	}
	return filepath.Join(os.Getenv("HOME"), ".local", "state", "agent-workflow", "locks", slug+".lock")
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

func containsNoVerify(path string) bool {
	out, err := exec.Command("rg", "--fixed-strings", "--quiet", "--", "--no-verify", path).CombinedOutput()
	if err == nil {
		return true
	}
	_ = out
	return false
}

func trimOutput(s string, limit int) string {
	s = strings.TrimSpace(s)
	if len(s) <= limit {
		return s
	}
	return s[:limit] + "...(truncated)"
}

func gitOutput(ctx context.Context, repo string, args ...string) (string, error) {
	all := append([]string{"-C", repo}, args...)
	out, err := exec.CommandContext(ctx, "git", all...).CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("git %s failed: %w: %s", strings.Join(args, " "), err, string(out))
	}
	return string(out), nil
}

func timeoutFromSeconds(seconds int) time.Duration {
	if seconds <= 0 {
		return 0
	}
	return time.Duration(seconds) * time.Second
}
