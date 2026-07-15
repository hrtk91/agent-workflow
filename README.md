# agent-workflow

ローカルのエージェントワークフローを、中断後も再開できる軽量ランナーです。

目的はエージェントを賢くすることではありません。エージェントの応答、キューに
入った実行、作成されたPRを、それだけでワークフロー完了の証拠として扱わない
ことが目的です。

## 全体像

```text
Hermes
  トリガー／通知UIのみ

agent-workflow
  タスクパケット
  キュー
  runごとのworktree
  executor
  QCコマンド
  jobs.sqlite（run状態／step試行履歴）
  summary.md／logs
  任意のOTLPエクスポート
```

通常、Hermesは作業をキューに追加してすぐ終了します。長時間の実行は
`aw tick`または`aw worker`が担当します。

## タスクパケット

このランナーはGitHub Issue専用ではありません。次のテキストパケットを受け取ります。

```text
task.md
acceptance.md     任意
constraints.md    任意
context.md        任意
source.json       任意の出典情報
```

GitHub Issue、Hermesのメッセージ、Discordの文章、ローカルメモは、実行前にこの
パケット形式へ変換します。コアランナーが必要とするのはテキストだけです。

## ワークフローの状態

各runでは次のstepを記録します。

- `load_task`
- `create_worktree`
- `run_executor`
- `run_qc`
- `write_summary`

runの状態とstepの試行履歴は、SQLiteを唯一の正本として保存します。人間が読む
summaryとコマンドログはファイルとして残ります。

```text
~/.local/state/agent-workflow/
  jobs.sqlite                       queue / runs / run_steps / step_attempts
  runs/<run-id>/task/*              コピーしたタスクパケット
  runs/<run-id>/summary.md
  runs/<run-id>/logs/*.log
  runs/<run-id>/executor_observability/*
  worktrees/<run-id>/repo
```

新しいrunでは`state.json`と`trace.jsonl`を作成しません。アップグレード後、最初に
writer側のコマンドを起動したとき、既存の`state.json`スナップショットを一度だけ
SQLiteへ取り込みます。旧ファイルは過去の成果物として残しますが、移行完了後に
ランナーが読み取ることはありません。

executorが成功しても、ワークフローが成功したとは限りません。明示したQCコマンド
が成功して初めてrunは`succeeded`になります。QCに失敗すると、同じrunのworktreeで
修正を続けるため、QCの失敗内容をタスクへ追加して最大5回までexecutorへ戻ります。
そのループ後もQCが成功しなければ、ワークフローは`qc_failed`のままです。QCから
executorへ戻る修復予算は正本SQLiteの`runs.qc_repair_attempts`に保存されるため、
`aw resume`と`aw retry`でプロセス内のカウンターがリセットされません。タイムアウト
は`timed_out`、タスク本文がない場合やポリシーで停止した場合は`blocked`になります。

## キュー

タスクをキューに追加して、すぐに戻ります。

```bash
aw enqueue \
  --repo /home/h-taminato/repos/eb-temp-hermes-runtime \
  --task-file /path/to/task.md \
  --workflow default \
  --verify-command 'mise run check-all' \
  --timeout-seconds 7200
```

キューにある作業を1回実行します。

```bash
aw tick --max-runs 1
```

キューにある作業を1回実行し、失敗・ブロック・タイムアウトしたrunをHermesへ通知します。

```bash
aw tick --max-runs 1 \
  --notify-command 'scripts/notify-hermes-workflow-summary.bashx {discord_summary} "[eb-temp workflow]"'
```

cronなどのディスパッチャーから使う場合は、ジョブ自体の失敗をディスパッチャー
プロセスの失敗にせず、`aw`の状態として記録できます。

```bash
aw tick --max-runs 1 \
  --isolate-job-failures \
  --notify-command 'scripts/notify-hermes-workflow-summary.bashx {discord_summary} "[eb-temp workflow]"'
```

キューにある作業を継続的に実行します。

```bash
aw worker --interval-seconds 60 --parallelism 1 --repo-parallelism 1
```

`aw worker`はデーモン型のエントリーポイントです。親プロセスがキューのジョブを
claimし、claimした各ジョブを子プロセスで実行し、子プロセスの終了結果を
`jobs.sqlite`へ記録します。起動時には、以前から`running`だったジョブを失敗として
扱います。デーモンを再起動すると、その子プロセスを所有していないためです。
実装作業では、worktree、ポート、キャッシュ、Dockerリソース、PR作成を同時実行
できるよう設計されていない限り、`--repo-parallelism 1`を維持してください。

通知コマンドでは、`{job_id}`、`{run_id}`、`{status}`、`{summary}`、
`{discord_summary}`のプレースホルダーが使えます。デフォルトでは`blocked`、
`failed`、`qc_failed`、`timed_out`だけを通知します。成功したrunも通知したいworker
では`--notify-statuses all`を指定してください。

内部のDiscord summaryは、対応する通知を送るまで機械的な内容です。送信時には、
`~/.config/agent-workflow/config.toml`で選んだproviderが最終的な通知文を生成します。
設定の作成と確認は次のコマンドで行います。

```bash
aw config init
aw config show
```

デフォルトのproviderはCodexです。ClaudeやGrokのような、サブスクリプションで
利用するCLIは名前付きproviderテーブルに追加できます。コマンドは標準入力から
プロンプトを読み、標準出力へ通知文を書き出します。

```toml
[notification]
provider = "claude"

[notification.providers.codex]
kind = "codex"
command = ["codex", "exec"]
timeout_seconds = 120
model = "gpt-5.6-luna"
reasoning_effort = "medium"

[notification.providers.claude]
kind = "command"
command = ["claude", "--print"]
timeout_seconds = 120
```

サブコマンドより前に`--config-file <path>`を渡すと、別の設定ファイルを使えます。
`AGENT_WORKFLOW_NOTIFICATION_*`環境変数を使うと、そのプロセスだけ読み込んだ設定を
上書きできます。Codexは一時的な空ディレクトリ、読み取り専用sandbox、継承した
shell環境なし、ユーザー設定やルールの読み込みなし、永続化されたsessionなしで
実行されます。独自のproviderコマンドでは、同等の隔離を自分で用意してください。

通常のワークフローが終端失敗したとき、診断ジョブをキューへ追加するには診断ループを
有効にします。

```bash
aw worker --interval-seconds 60 --parallelism 1 --repo-parallelism 1 \
  --auto-repair \
  --repair-model gpt-5.5
```

診断ジョブは`purpose=repair`を持つ通常の`aw` runです。`aw report --run-id`と、
summary、ログ、worktreeから失敗したrunを調べ、`aw repair draft`を呼び出して
検証済みの引き継ぎ成果物を作成する必要があります。診断ジョブが別の診断ジョブを
再帰的に作成することはありません。また、診断ジョブの失敗は通常のワークフロー
通知コマンドには流れません。診断ジョブが成功し、検証済みdraftを残すと、
`human_needed`以外のアクションに対して、`purpose=repair_action`を持つジョブを
`aw`が1件だけ直ちにキューへ追加します。実際のリポジトリやruntimeの修復はこの
アクションジョブで行い、QCを成功させます。

デフォルトでは、auto-repairは現在のworker／tick実行で発生した失敗だけを対象にします。
起動時に過去の失敗runを補完することはありません。過去の失敗を意図的に補完する
場合だけ`--repair-scan-existing`を使ってください。多数の過去失敗を一度にキューへ
追加する可能性があります。

## 直接実行

スモークテストや手動実行向けに、同期実行もできます。

```bash
aw run \
  --repo /home/h-taminato/repos/eb-temp-hermes-runtime \
  --task-file /path/to/task.md \
  --workflow default \
  --verify-command 'mise run check-all'
```

失敗または中断したrunを再開します。

```bash
aw resume --run-id <run-id>
```

指定したstepと、その後続stepをすべて再試行します。

```bash
aw retry --run-id <run-id> --step run_qc
```

最近のキュー済みジョブとrunを表示します。

```bash
aw status
```

## TUIでパイプラインを監視

キュー、実行中のrun、各stepの状態を端末上で確認するには`aw ui`を使います。
`aw tui`も同じコマンドの別名です。SQLiteを読み取り専用で参照するため、画面を
開くだけでrunやキューの状態は変更しません。

```bash
aw ui
```

画面では、矢印キーまたは`j`／`k`でrunを選び、Enterで詳細、`m`でメニューを開きます。
`:`を押すとコマンドを入力できます。

```text
:filter running
:refresh
:detail
:logs
:help
:quit
```

メニューとコマンドパレットは同じ操作を実行します。現在のTUIは状態確認、絞り込み、
詳細表示、ログ末尾の確認に対応しています。`resume`、`retry`、`cleanup`など状態を
変更する操作は、誤操作を避けるためCLIから実行します。

## ローカル分析

モデルやタスク種別を比較したい場合は、runにタグを付けます。

```bash
aw run \
  --repo /path/to/repo \
  --task-file /path/to/task.md \
  --verify-command 'mise run check-all' \
  --provider openai \
  --model gpt-example \
  --task-type bug_fix
```

`jobs.sqlite`から、初回QC、最終的なQC、QC試行回数、経過時間、変更行数を集計します。

```bash
aw report
aw report --group-by model,task_type
aw report --repo /path/to/repo --since 2026-07-01
aw report --group-by model,task_type --format json
```

内部のstateファイルを開かずに、1つのrunの現在のstep状態と全試行履歴を確認します。

```bash
aw report --run-id <run-id>
aw report --run-id <run-id> --format json
```

`aw report`はSQLiteを読み取り専用で開き、行の作成・移行・更新を行いません。データベース
または現在のschemaがまだ存在しない場合、集計結果は空のレポートを返し、どちらも
作成しません。`aw run`、`aw worker`、`aw status`などのwriterコマンドがschemaを
初期化し、旧形式からの一度だけの取り込みを行います。正本となるテーブルは次のとおりです。

- `queue`: workerジョブのキュー
- `runs`: 現在のrun状態、設定、所要時間、タスク識別情報、最終的な変更量
- `run_steps`: すべてのワークフローstepの再開用状態
- `step_attempts`: 試行ごとの生データ。状態、時刻、終了コード／エラー、ログパスを含む
- `storage_schema_migrations`: 保存schemaと旧形式取り込みのバージョン履歴

初回QC、最終的なQC、試行回数は`step_attempts`からレポーターが算出します。変更可能な
集計値を別の行として二重に保存することはありません。

repairとrepair-actionのrunはデフォルトでは除外されます。含める場合は
`--include-repair`を指定してください。

runのworktreeを削除します。

```bash
aw cleanup --run-id <run-id>
```

## Watchdogと修復draft

`aw watchdog scan`は、まだ修復draftが作られていない失敗runを一覧表示します。

```bash
aw watchdog scan --limit 10
```

修復の診断はコアランナーの外で行います。人間またはLLMがMarkdown形式の証拠を作成し、
型付きCLIを呼び出します。CLIは`repair.ini`を生成し、証拠をコピーし、ガードレールを
検証して、draftをSQLiteへ記録します。

```bash
aw repair draft \
  --failed-run-id <run-id> \
  --title "miseの状態を書き込めないためQCに失敗" \
  --category runtime_env \
  --risk low \
  --proposed-action runtime_environment_patch \
  --diagnosis-file diagnosis.md \
  --evidence-file evidence.md \
  --notify-before-file notify-before.md \
  --verify-command 'bashx scripts/agent-workflow-qc.bashx'
```

draftは次の場所に保存されます。

```text
~/.local/state/agent-workflow/
  repairs/<draft-id>/repair.ini
  repairs/<draft-id>/diagnosis.md
  repairs/<draft-id>/evidence.md
  repairs/<draft-id>/notify-before.md
```

draftコマンドは、戻る前に検証を行います。既存の成果物を再確認するには
`aw repair validate --draft-id <draft-id>`を使います。deployやmigrationのアクション
には、`--risk high`、`--environment`、`--healthcheck-command`、空でない
`--rollback-plan-file`が必要です。

## マージゲート

`aw merge-gate`はGitHub PRを評価し、次の3ファイルを作成します。

```text
merge-decision.json
merge-gate.md
hermes-discord-summary.md
```

ゲートはGitHubのchecks、PR headを分離したworktreeでのローカルQC、またはその両方を
使えます。GitHub checksもローカルQCもない場合は、デフォルトでブロックします。

```bash
aw merge-gate \
  --repo hrtk91/eb-temp \
  --pr 853 \
  --repo-path /home/h-taminato/repos/eb-temp-hermes-runtime \
  --verify-command 'bashx scripts/agent-workflow-qc.bashx'
```

`aw merge`は新しい`MERGE_APPROVED` decisionを受け取ります。実行前にPR head SHA、
base SHA、draft状態、マージ状態、live checksを再確認します。デフォルトはdry-runで、
実際にマージする場合は`--execute`を渡します。

```bash
aw merge --decision /path/to/merge-decision.json \
  --repo-path /path/to/repo \
  --verify-command 'bashx scripts/agent-workflow-qc.bashx'
aw merge --decision /path/to/merge-decision.json --execute \
  --repo-path /path/to/repo \
  --verify-command 'bashx scripts/agent-workflow-qc.bashx'
```

live PRにGitHub checksがない場合、`aw merge`は承認済みのhead SHAに対して指定された
ローカルQCを再実行します。この再確認を明示的に省略する場合だけ`--allow-no-checks`
を使ってください。

## OpenTelemetry

SQLiteには、`aw report`が使う永続的なrunと試行の事実が残ります。trace OTLP endpointを
設定すると、各起動が1つの`agent_workflow.run` spanとして直接エクスポートされ、各step
試行には子spanが作られます。

```bash
python3 -m pip install '.[otel]'
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
aw run --repo /path/to/repo --task-file /path/to/task.md \
  --workflow implementation --verify-command 'pytest'
```

root spanとstep spanには、要求したmodel、provider、task type、workflow、run status、
試行番号、コマンド結果、タイムアウト状態が含まれます。resumeやretryでは新しいrun span
が作られ、その起動で実行した試行だけがエクスポートされます。endpointがなければtrace
は何もしません。`OTEL_TRACES_EXPORTER=none`を設定すると、リモートtraceのエクスポートを
明示的に無効化できます。ローカルのtraceファイルは書きません。

集計済みSQLiteレポートを、OTLP/HTTP経由のOpenTelemetry gaugeとしてエクスポートします。

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
aw report --group-by model,task_type --export-otel
```

エクスポートにはrun／QC件数、初回QC率、最終QC率、QC試行回数の中央値、経過時間の中央値、
変更行数の中央値が含まれます。レポートのグループ値はmetric attributeになります。
SQLiteが永続的な正本であり、OTLPエクスポートは`--export-otel`を渡した場合だけ行われます。

## テスト

```bash
bashx scripts/test.bashx
```

Pythonテストはfake gitリポジトリとfake executor binaryを使うため、外部モデルは呼び出しません。
