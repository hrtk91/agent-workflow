package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"os"

	"github.com/hatchet-dev/hatchet/pkg/cmdutils"
	hatchet "github.com/hatchet-dev/hatchet/sdks/go"

	"github.com/hrtk91/agent-workflow/internal/model"
	"github.com/hrtk91/agent-workflow/internal/workflows"
)

func main() {
	var (
		workflowName      = flag.String("workflow", "ebtemp-agent-pipeline", "Hatchet agent workflow name to register or trigger")
		taktWorkflowName  = flag.String("takt-workflow", "run-takt-workflow", "Hatchet takt supervisor workflow name to register or trigger")
		metaWorkflowName  = flag.String("meta-workflow", "create-workflow-definition", "Hatchet meta-workflow name to register or trigger")
		kind              = flag.String("kind", "takt", "workflow kind: takt, agent, or meta")
		mode              = flag.String("mode", "worker", "worker, trigger, dry-run, or cleanup-docker")
		repo              = flag.String("repo", "/home/h-taminato/repos/eb-temp", "repository path for agent runs")
		issue             = flag.Int("issue", 0, "GitHub issue number for the run")
		command           = flag.String("command", "", "agent command for dry-run or trigger")
		taktArgs          = flag.String("takt-args", "", "arguments passed after the takt command for takt runs")
		taktLock          = flag.String("takt-lock", "", "lock file for takt runs; defaults under ~/.local/state/agent-workflow/locks")
		requireCleanRepo  = flag.Bool("require-clean-repo", true, "refuse to start a takt run when the target repo is dirty")
		allowNewWorktrees = flag.Bool("allow-new-worktrees", false, "allow new git worktrees to remain after takt run")
		verifyCommand     = flag.String("verify-command", "", "verification command; required for an agent run to be marked done")
		timeoutSeconds    = flag.Int("timeout-seconds", 0, "agent command timeout in seconds; 0 disables command timeout")
		useDocker         = flag.Bool("docker", false, "run the agent command inside docker")
		metaRepo          = flag.String("meta-repo", "/home/h-taminato/repos/agent-workflow", "repository path for meta-workflow code generation")
		metaTarget        = flag.String("meta-target-workflow", "draft-generated-workflow", "target workflow name produced by the meta-workflow")
		metaRequest       = flag.String("meta-request", "", "workflow creation request captured in the meta review report")
		metaGenerator     = flag.String("meta-generator-command", "", "command that generates or edits workflow code in the meta worktree")
		metaDryRunRepo    = flag.String("meta-dry-run-repo", "/home/h-taminato/repos/eb-temp", "repository used by meta-workflow sample dry-runs")
		metaSampleCommand = flag.String("meta-sample-agent-command", "printf meta-dry-run > agent-workflow-meta-smoke.txt", "sample agent command used by meta dry-run")
		metaSampleVerify  = flag.String("meta-sample-verify-command", "test -f agent-workflow-meta-smoke.txt", "sample verify command used by meta dry-run")
		metaRequireDocker = flag.Bool("meta-require-docker-cleanup", false, "require Docker label cleanup confirmation in meta-workflow")
	)
	flag.Parse()

	input := model.AgentRunInput{
		RepoPath:              *repo,
		IssueNumber:           *issue,
		AgentCommand:          *command,
		VerifyCommand:         *verifyCommand,
		CommandTimeoutSeconds: *timeoutSeconds,
		UseDocker:             *useDocker,
		WorkflowLabel:         *workflowName,
	}
	metaInput := model.MetaWorkflowInput{
		RepoPath:              *metaRepo,
		TargetWorkflowName:    *metaTarget,
		Request:               *metaRequest,
		GeneratorCommand:      *metaGenerator,
		DryRunRepoPath:        *metaDryRunRepo,
		SampleAgentCommand:    *metaSampleCommand,
		SampleVerifyCommand:   *metaSampleVerify,
		CommandTimeoutSeconds: *timeoutSeconds,
		RequireDockerCleanup:  *metaRequireDocker,
		WorkflowLabel:         *metaWorkflowName,
	}
	taktInput := model.TaktRunInput{
		RepoPath:              *repo,
		TaktArgs:              *taktArgs,
		VerifyCommand:         *verifyCommand,
		CommandTimeoutSeconds: *timeoutSeconds,
		RequireCleanRepo:      *requireCleanRepo,
		AllowNewWorktrees:     *allowNewWorktrees,
		LockPath:              *taktLock,
		WorkflowLabel:         *taktWorkflowName,
	}

	switch *mode {
	case "cleanup-docker":
		if err := workflows.CleanupDocker(context.Background()); err != nil {
			log.Fatalf("docker cleanup failed: %v", err)
		}
		fmt.Println("docker cleanup complete")
	case "dry-run":
		switch *kind {
		case "meta":
			out, err := workflows.RunMetaLocal(context.Background(), metaInput)
			if err != nil {
				log.Fatalf("meta dry-run failed: %v", err)
			}
			fmt.Println(out.ReportPath)
		case "agent":
			out, err := workflows.RunLocal(context.Background(), input)
			if err != nil {
				log.Fatalf("dry-run failed: %v", err)
			}
			fmt.Println(out.SummaryPath)
		case "takt":
			out, err := workflows.RunTaktLocal(context.Background(), taktInput)
			if err != nil {
				log.Fatalf("takt dry-run failed: %v", err)
			}
			fmt.Println(out.SummaryPath)
		default:
			log.Fatalf("unknown -kind %q", *kind)
		}
	case "trigger":
		client, err := newHatchetClient()
		if err != nil {
			log.Fatal(err)
		}
		switch *kind {
		case "meta":
			ref, err := client.RunNoWait(context.Background(), *metaWorkflowName, metaInput)
			if err != nil {
				log.Fatalf("failed to trigger meta-workflow: %v", err)
			}
			fmt.Println(ref.RunId)
			return
		case "agent":
			if *command == "" {
				log.Fatal("-command is required in trigger mode")
			}
			ref, err := client.RunNoWait(context.Background(), *workflowName, input)
			if err != nil {
				log.Fatalf("failed to trigger workflow: %v", err)
			}
			fmt.Println(ref.RunId)
		case "takt":
			ref, err := client.RunNoWait(context.Background(), *taktWorkflowName, taktInput)
			if err != nil {
				log.Fatalf("failed to trigger takt workflow: %v", err)
			}
			fmt.Println(ref.RunId)
		default:
			log.Fatalf("unknown -kind %q", *kind)
		}
	case "worker":
		client, err := newHatchetClient()
		if err != nil {
			log.Fatal(err)
		}
		agentWorkflow := workflows.NewEBTempWorkflow(client, *workflowName)
		taktWorkflow := workflows.NewTaktWorkflow(client, *taktWorkflowName)
		metaWorkflow := workflows.NewMetaWorkflow(client, *metaWorkflowName)
		worker, err := client.NewWorker("agent-workflow-worker", hatchet.WithWorkflows(agentWorkflow, taktWorkflow, metaWorkflow))
		if err != nil {
			log.Fatalf("failed to create worker: %v", err)
		}
		interruptCtx, cancel := cmdutils.NewInterruptContext()
		defer cancel()
		if err := worker.StartBlocking(interruptCtx); err != nil {
			log.Fatalf("worker failed: %v", err)
		}
	default:
		log.Fatalf("unknown -mode %q", *mode)
	}
}

func newHatchetClient() (*hatchet.Client, error) {
	if os.Getenv("HATCHET_CLIENT_TOKEN") == "" && os.Getenv("HATCHET_TOKEN") == "" {
		return nil, errors.New("Hatchet credentials are missing; start Hatchet and export its client token first")
	}
	client, err := hatchet.NewClient()
	if err != nil {
		return nil, fmt.Errorf("create hatchet client: %w", err)
	}
	return client, nil
}
