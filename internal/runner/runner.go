package runner

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/hrtk91/agent-workflow/internal/model"
)

type PrepareInput struct {
	RepoPath string
	Issue    int
	Label    string
}

type CommandInput struct {
	Command     string
	Workdir     string
	LogDir      string
	Timeout     time.Duration
	UseDocker   bool
	RunID       string
	RepoPath    string
	WorkflowTag string
}

func Prepare(ctx context.Context, input PrepareInput) (model.PrepareOutput, error) {
	if input.RepoPath == "" {
		return model.PrepareOutput{}, fmt.Errorf("repo path is required")
	}
	runID := NewRunID()
	stateRoot := filepath.Join(os.Getenv("HOME"), ".local", "state", "agent-workflow")
	logDir := filepath.Join(stateRoot, "runs", runID)
	worktreeDir := filepath.Join(os.TempDir(), "agent-workflow", runID, "repo")
	if err := os.MkdirAll(logDir, 0o755); err != nil {
		return model.PrepareOutput{}, err
	}
	if err := os.MkdirAll(filepath.Dir(worktreeDir), 0o755); err != nil {
		return model.PrepareOutput{}, err
	}

	baseRef, err := gitOutput(ctx, input.RepoPath, "rev-parse", "--verify", "origin/main")
	if err != nil {
		baseRef, err = gitOutput(ctx, input.RepoPath, "rev-parse", "--verify", "HEAD")
		if err != nil {
			return prepareSnapshot(ctx, input, runID, logDir, worktreeDir)
		}
	}
	baseRef = strings.TrimSpace(baseRef)
	cmd := exec.CommandContext(ctx, "git", "-C", input.RepoPath, "worktree", "add", "--detach", worktreeDir, baseRef)
	if out, err := cmd.CombinedOutput(); err != nil {
		return model.PrepareOutput{}, fmt.Errorf("git worktree add failed: %w: %s", err, string(out))
	}

	return model.PrepareOutput{
		RepoPath:    input.RepoPath,
		RunID:       runID,
		WorktreeDir: worktreeDir,
		LogDir:      logDir,
		BaseRef:     baseRef,
	}, nil
}

func prepareSnapshot(ctx context.Context, input PrepareInput, runID string, logDir string, worktreeDir string) (model.PrepareOutput, error) {
	if err := copyDir(input.RepoPath, worktreeDir); err != nil {
		return model.PrepareOutput{}, err
	}
	if out, err := exec.CommandContext(ctx, "git", "-C", worktreeDir, "init").CombinedOutput(); err != nil {
		return model.PrepareOutput{}, fmt.Errorf("git init snapshot failed: %w: %s", err, string(out))
	}
	if out, err := exec.CommandContext(ctx, "git", "-C", worktreeDir, "add", ".").CombinedOutput(); err != nil {
		return model.PrepareOutput{}, fmt.Errorf("git add snapshot failed: %w: %s", err, string(out))
	}
	if out, err := exec.CommandContext(ctx, "git", "-C", worktreeDir, "-c", "user.name=agent-workflow", "-c", "user.email=agent-workflow@example.invalid", "commit", "-m", "agent-workflow snapshot").CombinedOutput(); err != nil {
		return model.PrepareOutput{}, fmt.Errorf("git commit snapshot failed: %w: %s", err, string(out))
	}
	baseRef, err := gitOutput(ctx, worktreeDir, "rev-parse", "--verify", "HEAD")
	if err != nil {
		return model.PrepareOutput{}, err
	}
	return model.PrepareOutput{
		RepoPath:    input.RepoPath,
		RunID:       runID,
		WorktreeDir: worktreeDir,
		LogDir:      logDir,
		BaseRef:     "snapshot:" + strings.TrimSpace(baseRef),
	}, nil
}

func RunCommand(ctx context.Context, input CommandInput) (model.CommandOutput, error) {
	stdoutPath := filepath.Join(input.LogDir, "stdout.log")
	stderrPath := filepath.Join(input.LogDir, "stderr.log")
	stdout, err := os.Create(stdoutPath)
	if err != nil {
		return model.CommandOutput{}, err
	}
	defer stdout.Close()
	stderr, err := os.Create(stderrPath)
	if err != nil {
		return model.CommandOutput{}, err
	}
	defer stderr.Close()

	runCtx := ctx
	var cancel context.CancelFunc
	if input.Timeout > 0 {
		runCtx, cancel = context.WithTimeout(ctx, input.Timeout)
		defer cancel()
	}

	var cmd *exec.Cmd
	if input.UseDocker {
		cmd = dockerCommand(runCtx, input)
	} else {
		cmd = exec.CommandContext(runCtx, "bash", "-lc", input.Command)
		cmd.Dir = input.Workdir
	}
	cmd.Stdout = stdout
	cmd.Stderr = stderr
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}

	if err := cmd.Start(); err != nil {
		return model.CommandOutput{}, err
	}
	waitCh := make(chan error, 1)
	go func() { waitCh <- cmd.Wait() }()
	var runErr error
	select {
	case runErr = <-waitCh:
	case <-runCtx.Done():
		if cmd.Process != nil {
			_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
			_ = cmd.Process.Kill()
		}
		runErr = <-waitCh
	}
	exitCode := 0
	timedOut := runCtx.Err() == context.DeadlineExceeded
	if runErr != nil {
		var exitErr *exec.ExitError
		if ok := errorsAs(runErr, &exitErr); ok {
			exitCode = exitErr.ExitCode()
		} else if timedOut {
			exitCode = -1
		} else {
			return model.CommandOutput{}, runErr
		}
	}
	return model.CommandOutput{
		Command:    input.Command,
		ExitCode:   exitCode,
		StdoutPath: stdoutPath,
		StderrPath: stderrPath,
		TimedOut:   timedOut,
	}, nil
}

func dockerCommand(ctx context.Context, input CommandInput) *exec.Cmd {
	name := "agent-workflow-" + input.RunID
	args := []string{
		"run", "--rm",
		"--name", name,
		"--label", "agent-workflow=true",
		"--label", "agent-workflow.run-id=" + input.RunID,
		"--label", "agent-workflow.workflow=" + sanitizeLabel(input.WorkflowTag),
		"-v", input.Workdir + ":/workspace",
		"-w", "/workspace",
		"agent-workflow-runner:latest",
		"bash", "-lc", input.Command,
	}
	return exec.CommandContext(ctx, "docker", args...)
}

func WriteSummary(prepared model.PrepareOutput, command model.CommandOutput, checked model.VerificationOutput) (model.FinalOutput, error) {
	path := filepath.Join(prepared.LogDir, "summary.md")
	var buf bytes.Buffer
	fmt.Fprintf(&buf, "# agent-workflow run %s\n\n", prepared.RunID)
	fmt.Fprintf(&buf, "- worktree: `%s`\n", prepared.WorktreeDir)
	fmt.Fprintf(&buf, "- base_ref: `%s`\n", prepared.BaseRef)
	fmt.Fprintf(&buf, "- command: `%s`\n", command.Command)
	fmt.Fprintf(&buf, "- command_exit_code: `%d`\n", command.ExitCode)
	fmt.Fprintf(&buf, "- command_timed_out: `%t`\n", command.TimedOut)
	fmt.Fprintf(&buf, "- stdout: `%s`\n", command.StdoutPath)
	fmt.Fprintf(&buf, "- stderr: `%s`\n", command.StderrPath)
	fmt.Fprintf(&buf, "- verifier_passed: `%t`\n", checked.Passed)
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

func Cleanup(ctx context.Context, repoPath string, worktreeDir string) error {
	if worktreeDir == "" {
		return nil
	}
	parent := filepath.Dir(filepath.Dir(worktreeDir))
	cmd := exec.CommandContext(ctx, "git", "-C", repoPath, "worktree", "remove", "--force", worktreeDir)
	_ = cmd.Run()
	_ = exec.CommandContext(ctx, "git", "-C", repoPath, "worktree", "prune").Run()
	return os.RemoveAll(parent)
}

func CleanupDocker(ctx context.Context) error {
	out, err := exec.CommandContext(ctx, "docker", "ps", "-aq", "--filter", "label=agent-workflow=true").CombinedOutput()
	if err != nil {
		return fmt.Errorf("docker ps failed: %w: %s", err, string(out))
	}
	ids := strings.Fields(string(out))
	if len(ids) == 0 {
		return nil
	}
	args := append([]string{"rm", "-f"}, ids...)
	out, err = exec.CommandContext(ctx, "docker", args...).CombinedOutput()
	if err != nil {
		return fmt.Errorf("docker rm failed: %w: %s", err, string(out))
	}
	return nil
}

func gitOutput(ctx context.Context, repo string, args ...string) (string, error) {
	all := append([]string{"-C", repo}, args...)
	out, err := exec.CommandContext(ctx, "git", all...).CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("git %s failed: %w: %s", strings.Join(args, " "), err, string(out))
	}
	return string(out), nil
}

func randomSuffix() string {
	var b [4]byte
	if _, err := rand.Read(b[:]); err != nil {
		return fmt.Sprintf("%d", time.Now().UnixNano())
	}
	return hex.EncodeToString(b[:])
}

func NewRunID() string {
	return fmt.Sprintf("%s-%s", time.Now().UTC().Format("20060102T150405Z"), randomSuffix())
}

func sanitizeLabel(s string) string {
	s = strings.TrimSpace(s)
	if s == "" {
		return "default"
	}
	return strings.NewReplacer("/", "-", " ", "-", ":", "-").Replace(s)
}

func errorsAs(err error, target any) bool {
	return errors.As(err, target)
}

func copyDir(src string, dst string) error {
	return filepath.WalkDir(src, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(src, path)
		if err != nil {
			return err
		}
		if rel == "." {
			return os.MkdirAll(dst, 0o755)
		}
		name := d.Name()
		if d.IsDir() && (name == ".git" || name == "node_modules") {
			return filepath.SkipDir
		}
		target := filepath.Join(dst, rel)
		info, err := d.Info()
		if err != nil {
			return err
		}
		if d.IsDir() {
			return os.MkdirAll(target, info.Mode().Perm())
		}
		if !info.Mode().IsRegular() {
			return nil
		}
		return copyFile(path, target, info.Mode().Perm())
	})
}

func copyFile(src string, dst string, mode fs.FileMode) error {
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return err
	}
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(dst, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, mode)
	if err != nil {
		return err
	}
	defer out.Close()
	_, err = io.Copy(out, in)
	return err
}
