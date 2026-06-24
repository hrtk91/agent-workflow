package model

type AgentRunInput struct {
	RepoPath              string `json:"repoPath"`
	IssueNumber           int    `json:"issueNumber"`
	AgentCommand          string `json:"agentCommand"`
	VerifyCommand         string `json:"verifyCommand"`
	CommandTimeoutSeconds int    `json:"commandTimeoutSeconds"`
	UseDocker             bool   `json:"useDocker"`
	WorkflowLabel         string `json:"workflowLabel"`
}

type TaktRunInput struct {
	RepoPath              string `json:"repoPath"`
	TaktArgs              string `json:"taktArgs"`
	VerifyCommand         string `json:"verifyCommand"`
	CommandTimeoutSeconds int    `json:"commandTimeoutSeconds"`
	RequireCleanRepo      bool   `json:"requireCleanRepo"`
	AllowNewWorktrees     bool   `json:"allowNewWorktrees"`
	LockPath              string `json:"lockPath"`
	WorkflowLabel         string `json:"workflowLabel"`
}

type MetaWorkflowInput struct {
	RepoPath              string `json:"repoPath"`
	TargetWorkflowName    string `json:"targetWorkflowName"`
	Request               string `json:"request"`
	GeneratorCommand      string `json:"generatorCommand"`
	DryRunRepoPath        string `json:"dryRunRepoPath"`
	SampleAgentCommand    string `json:"sampleAgentCommand"`
	SampleVerifyCommand   string `json:"sampleVerifyCommand"`
	CommandTimeoutSeconds int    `json:"commandTimeoutSeconds"`
	RequireDockerCleanup  bool   `json:"requireDockerCleanup"`
	WorkflowLabel         string `json:"workflowLabel"`
}

type RepoRunOutput struct {
	RepoPath         string   `json:"repoPath"`
	RunID            string   `json:"runId"`
	LogDir           string   `json:"logDir"`
	BaseRef          string   `json:"baseRef"`
	InitialStatus    string   `json:"initialStatus"`
	InitialWorktrees []string `json:"initialWorktrees"`
	LockPath         string   `json:"lockPath"`
}

type PrepareOutput struct {
	RepoPath    string `json:"repoPath"`
	RunID       string `json:"runId"`
	WorktreeDir string `json:"worktreeDir"`
	LogDir      string `json:"logDir"`
	BaseRef     string `json:"baseRef"`
}

type CommandOutput struct {
	Command    string `json:"command"`
	ExitCode   int    `json:"exitCode"`
	StdoutPath string `json:"stdoutPath"`
	StderrPath string `json:"stderrPath"`
	TimedOut   bool   `json:"timedOut"`
}

type VerificationOutput struct {
	Passed  bool     `json:"passed"`
	Reasons []string `json:"reasons"`
}

type FinalOutput struct {
	RunID       string `json:"runId"`
	Done        bool   `json:"done"`
	SummaryPath string `json:"summaryPath"`
}

type MetaStepOutput struct {
	Name       string   `json:"name"`
	Command    string   `json:"command"`
	ExitCode   int      `json:"exitCode"`
	StdoutPath string   `json:"stdoutPath"`
	StderrPath string   `json:"stderrPath"`
	Passed     bool     `json:"passed"`
	Notes      []string `json:"notes"`
}

type MetaFinalOutput struct {
	RunID       string           `json:"runId"`
	Passed      bool             `json:"passed"`
	Status      string           `json:"status"`
	ReportPath  string           `json:"reportPath"`
	PatchPath   string           `json:"patchPath"`
	DraftPath   string           `json:"draftPath"`
	Steps       []MetaStepOutput `json:"steps"`
	Reasons     []string         `json:"reasons"`
	WorktreeDir string           `json:"worktreeDir"`
}
