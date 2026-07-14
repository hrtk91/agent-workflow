# agent-workflow レビュー指摘と改善バックログ

最終レビュー日: 2026-07-14

このドキュメントは、システムレビューで挙がった指摘を改善作業用に整理したもの。
**1 項目ずつ着手し、完了したら Status を更新する。**

関連コンテキスト:

- 目的はエージェントを賢くすることではなく、「完了の定義」と「失敗の扱い」を機械的に守ること
- 残すべき設計判断は [守るべき設計判断](#守るべき設計判断) を参照

---

## 使い方

1. 優先度の高い `open` 項目から 1 つ選ぶ
2. 必要なら Issue / ブランチを切る
3. 実装 → テスト → このファイルの Status を `done` に更新
4. 判断が変わった場合は「メモ」に追記する（削除より履歴を残す）

Status 値:

| Status | 意味 |
|--------|------|
| `open` | 未着手 |
| `in_progress` | 作業中 |
| `done` | 完了 |
| `wontfix` | やらない（理由をメモに書く） |
| `deferred` | 今はやらないが将来検討 |

---

## 守るべき設計判断

改善時に壊してはいけない軸。

1. **QC を必須の完了ゲートにする**（executor 成功 ≠ workflow 成功）
2. **診断 (repair) と修復アクション (repair_action) を分離し、診断を再帰させない**
3. **merge decision を実行時に再検証する**（dry-run デフォルト、head/base SHA ロック）
4. **enqueue と long-running worker を分ける**（Hermes はすぐ抜ける）
5. **異常系をテストの主戦場にする**

---

## 総評（要約）

| 面 | 評価 |
|----|------|
| 問題設定 / 完了定義 | 非常に良い |
| 失敗プロセス化 (QC loop / repair / merge gate) | 良い |
| 異常系テスト | かなり良い |
| コード構造（凝集・分割） | 弱い（runner 集中） |
| 状態の正本 | 概ね整理済み（run状態はSQLite正本） |
| 共有 / マルチホスト耐性 | 対象外〜弱い（ローカル単機前提） |

---

## 改善バックログ

### P0 — 正しさ・一貫性（先に直すと効く）

#### IMP-001: QC repair 試行回数を state に永続化する

| 項目 | 内容 |
|------|------|
| Status | `done` |
| 重大度 | 重大寄り |
| 対象 | `src/agent_workflow/runner.py`, `src/agent_workflow/state.py` |
| 現状 | ~~`qc_repair_attempts` が `_run_from` 内のローカル変数。kill → resume でカウンタが 0 に戻る~~ → `RunState.qc_repair_attempts` に永続化済み |
| 望ましい姿 | run 全体で「最大 N 回」が厳密に守られる。resume 後も同じ上限を共有する |
| 受け入れ条件 | |
| | - [x] `RunState`（または step メタ）に QC repair 試行回数が永続化される |
| | - [x] resume / retry 後も上限を超えて executor に戻らない |
| | - [x] 上限到達時は従来どおり `qc_failed` |
| | - [x] 回帰テスト: 途中保存された attempt から resume しても上限が効く |
| メモ | README の「最大 5 回」をrun単位（SQLite `runs.qc_repair_attempts`）と明記。summaryにも `qc_repair_attempts: n/5` を出力。legacy stateはdefault 0 |

---

#### IMP-002: marker ファイルと enqueue を atomic にする

| 項目 | 内容 |
|------|------|
| Status | `open` |
| 重大度 | 重大寄り |
| 対象 | `runner.py` の auto-repair / repair_action enqueue 経路 |
| 現状 | `action-enqueued.json` や auto-repair marker と queue insert が分離。クラッシュで「積んだが marker なし」「marker あるが未積」になりうる |
| 望ましい姿 | 「この失敗に対する diagnosis/action は既に扱った」が DB 上で一意に表現され、二重 enqueue と取りこぼしが起きない |
| 受け入れ条件 | |
| | - repair / repair_action の enqueue 可否が sqlite（または単一トランザクション）で決まる |
| | - 既存 marker 互換 or 移行手順がある |
| | - 二重 tick / worker 再起動でも同一 failed run に diagnosis が二重に積まれないテスト |
| メモ | file marker を残すなら「DB が正、marker はキャッシュ」と明文化する |

---

#### IMP-003: 状態の正本を整理する

| 項目 | 内容 |
|------|------|
| Status | `in_progress` |
| 重大度 | 重大寄り |
| 対象 | `runs`, `run_steps`, `step_attempts`, `queue`, `repair_drafts`, marker ファイル |
| 現状 | run詳細・step状態・attempt履歴はSQLite（`runs` / `run_steps` / `step_attempts`）を正本とし、task・summary・ログはファイル成果物として保持。queue・repair draft・enqueue markerは別の責務として残っている |
| 望ましい姿 | 正本と派生の関係がドキュメントとコードで一致している |
| 残作業（段階導入可） | |
| | 1. queue・repair draft・enqueue markerの正本表を明文化する |
| | 2. `save_state` / `RunStore` 以外からrun状態を更新しない境界を維持する |
| | 3. RepairManager / WorkflowRunner のDB初期化経路を共通化する |
| | 4. marker依存をIMP-002でDB側へ寄せる |
| 受け入れ条件 | |
| | - 「何が正本か」が README または本ドキュメントに明記されている |
| | - DB schema init が単一経路 |
| | - run状態とqueueの不整合を検出する status/doctor 相当がある（任意だが推奨） |
| メモ | いきなり単一ストアに寄せなくてよい。まず関係の明文化からでよい |

---

### P1 — 構造・保守性

#### IMP-004: `runner.py` を責務分割する

| 項目 | 内容 |
|------|------|
| Status | `open` |
| 重大度 | 中 |
| 対象 | `src/agent_workflow/runner.py`（約 1700 行） |
| 現状 | queue / worker / step 実行 / repair 連携 / summary / notify / DB が同居 |
| 望ましい分割案 | |
| | - `queue.py` … enqueue, claim, finish, recover_stale |
| | - `worker.py` … tick/worker ループ、child spawn/timeout |
| | - `steps.py` または `pipeline.py` … `_run_from` と各 step |
| | - `repair_orchestration.py` … auto-repair / repair_action enqueue |
| | - `summary.py` … summary / discord summary 生成 |
| | - `runner.py` … 薄い facade / WorkflowRunner 互換 API |
| 受け入れ条件 | |
| | - 公開 CLI 互換を壊さない |
| | - 既存テストが同等以上に通る |
| | - 1 ファイルが目安 400–600 行以下、または責務がディレクトリで見える |
| メモ | 分割は機械的移動から入り、挙動変更は混ぜない |

---

#### IMP-005: status / purpose / step を型で固定する

| 項目 | 内容 |
|------|------|
| Status | `open` |
| 重大度 | 中 |
| 対象 | `state.py`, runner / repair / cli 出力 |
| 現状 | status や purpose が free string。結果も `dict[str, str]` 中心 |
| 望ましい姿 | `Literal` / `Enum` / TypedDict or dataclass で取りうる値が閉じている |
| 受け入れ条件 | |
| | - run status, step status, purpose の許可集合が型または定数で定義される |
| | - 不正値を load したときの扱いが明確（reject or normalize） |
| | - CLI の機械可読出力を壊す場合は移行方針がある |
| メモ | まずは内部型のみ強化し、CLI 文字列は互換維持が安全 |

---

#### IMP-006: executor / notification を adapter 化する

| 項目 | 内容 |
|------|------|
| Status | `open` |
| 重大度 | 中 |
| 対象 | `_step_run_executor`, `notify/discord.py` |
| 現状 | executor 引数が takt CLI 前提。通知が takt report / session shadow と codex ハードコードに依存 |
| 望ましい姿 | コアは「packet + QC + state」。executor/notify は差し替え可能 |
| 受け入れ条件 | |
| | - executor 起動が adapter インターフェース経由 |
| | - デフォルト adapter が現行 takt 互換 |
| | - 通知の LLM モデル名などが設定可能（ハードコード撤去） |
| | - takt 固有パースは notify 配下に閉じる |
| メモ | コアの汎用性を保つための境界づくり。全面プラグイン化は不要 |

---

### P2 — 運用・安全

#### IMP-007: 信頼境界とセキュリティモデルを明文化する

| 項目 | 内容 |
|------|------|
| Status | `open` |
| 重大度 | 中（個人運用では許容、共有化で重大） |
| 対象 | README / docs、将来の入力検証 |
| 現状 | QC / notify が `bash -lc`。enqueue できる人 = 任意コマンド実行可能 |
| 望ましい姿 | 「単一信頼ユーザー・ローカル state dir」前提が明記され、共有時の注意が分かる |
| 受け入れ条件 | |
| | - README に Trust model 節がある |
| | - 共有 worker にする場合の禁止事項 / 必要なガードが書いてある |
| | - （任意）verify/notify を allowlist または argv 配列で渡せる経路 |
| メモ | 実装変更より先にドキュメントでも価値がある |

---

#### IMP-008: worker 再起動時の stale recovery を優しくする

| 項目 | 内容 |
|------|------|
| Status | `open` |
| 重大度 | 中 |
| 対象 | `recover_stale_running`, worker startup |
| 現状 | worker 起動時に pre-existing `running` をまとめて failed 扱い |
| 望ましい姿 | 実プロセスが生きている job は failed にしない。死んでいるものだけ回収 |
| 受け入れ条件 | |
| | - child PID / heartbeat / lease のいずれかで存活判定 |
| | - 本当に orphan な running だけ failed になる |
| | - 再起動直後の過剰 auto-repair を抑える |
| | - テストで「生きている child は recover しない」を固定 |
| メモ | 単一 daemon 前提なら現状でも可。再起動頻度が上がったら必須 |

---

#### IMP-009: worktree / 成果物の GC ポリシーを入れる

| 項目 | 内容 |
|------|------|
| Status | `open` |
| 重大度 | 中 |
| 対象 | `cleanup`, worker / cron 運用 |
| 現状 | `aw cleanup` はあるが自動 GC が薄い。長期運用で disk 圧迫しやすい |
| 望ましい姿 | 成功/失敗ごとに保持期間が決まり、安全に掃除できる |
| 受け入れ条件 | |
| | - 例: succeeded は N 日後に worktree 削除、logs は M 日保持、などポリシーが文書化 |
| | - `aw cleanup --older-than` または worker オプションがある |
| | - running / 未診断 failure は消さない |
| | - dry-run で削除対象を見られる |
| メモ | 最初は明示コマンドだけでよく、自動 GC は次段 |

---

#### IMP-010: repair の意味的ガードを段階的に足す（任意）

| 項目 | 内容 |
|------|------|
| Status | `open` |
| 重大度 | 中（常時 auto-repair ON なら重要） |
| 対象 | repair draft 検証、auto-repair ポリシー |
| 現状 | 構造検証は強いが、「この失敗にこの action が妥当か」は見ない |
| 望ましい姿 | 危険 action の自動実行条件が狭い |
| 案 | |
| | - risk=high は auto enqueue しない（human 承認必須） |
| | - category と proposed_action の許可マトリクス |
| | - 同一 repo の repair_action 連続失敗でサーキットブレーカ |
| 受け入れ条件 | 採用したポリシーがテストと README に反映されていること |
| メモ | 全部やらなくてよい。auto-repair 運用方針に合わせて最小セットから |

---

### P3 — 製品化・観測（余裕があれば）

#### IMP-011: パッケージ出荷形態を整える

| 項目 | 内容 |
|------|------|
| Status | `open` |
| 重大度 | 軽微 |
| 対象 | `pyproject.toml`, インストール手順 |
| 現状 | 最小構成。entry point / 依存の体裁が個人リポ寄り |
| 望ましい姿 | `pip install -e .` や `aw` コンソールスクリプトが素直に使える |
| 受け入れ条件 | |
| | - `[project.scripts] aw = ...` など入口が定義されている |
| | - README の Install が実際に通る |
| メモ | 個人利用だけなら後回しでよい |

---

#### IMP-012: 運用メトリクスの一階建て集計を足す

| 項目 | 内容 |
|------|------|
| Status | `open` |
| 重大度 | 軽微 |
| 対象 | `aw status` 拡張 or レポートコマンド |
| 現状 | run 単位の summary/trace はあるが、横断集計がない |
| 見たい指標例 | 失敗率、QC loop 回数分布、repair 成功率、repo 別スループット、timeout 率 |
| 受け入れ条件 | |
| | - 直近 N 件から最低限の集計が CLI で出る |
| | - 外部 SaaS 必須にしない |
| メモ | SQLiteのcanonical run factsから出せる範囲で十分 |

---

#### IMP-013: 命名の takt 残滓を整理する

| 項目 | 内容 |
|------|------|
| Status | `open` |
| 重大度 | 軽微 |
| 対象 | notify、summary フィールド、互換キー |
| 現状 | `executor_bin` への移行は済み。通知や観測パスに takt 語彙が残る |
| 望ましい姿 | 外部向けは executor 中立。takt 固有名は adapter 内 |
| 受け入れ条件 | 互換を壊す場合は旧キー読み取りを残す |
| メモ | IMP-006 とまとめてやってよい |

---

## 推奨する着手順

一人で順番に潰す場合の推奨順。

| 順番 | ID | 理由 |
|------|-----|------|
| 1 | IMP-001 | 仕様の穴が小さく、テストで閉じやすい |
| 2 | IMP-002 | 二重修復 / 取りこぼしの実害防止 |
| 3 | IMP-003 | 以降の変更の土台になる |
| 4 | IMP-004 | 以降の改修コストを下げる |
| 5 | IMP-005 | 分割後に型を閉じると安全 |
| 6 | IMP-007 | 実装前に信頼境界を文章化 |
| 7 | IMP-006 | 結合度を下げる |
| 8 | IMP-008 / IMP-009 | 運用時間が増えたら |
| 9 | IMP-010 | auto-repair 常時 ON にするとき |
| 10 | IMP-011〜013 | 余裕があれば |

---

## 良いところ（改善で失わないこと）

改善作業中に回帰させたくない強み。

1. **完了条件が QC に固定されている**
2. **Task packet が GitHub 非依存**
3. **失敗を終了ではなくプロセス化している**（QC loop / diagnosis / action）
4. **merge gate が安全側デフォルト**
5. **ローカル運用の現実解**（worktree、resume、claim、stale recovery、cron isolate）
6. **異常系ファーストのテスト**
7. **依存が薄い**（標準ライブラリ中心）

---

## 進捗サマリ

| ID | タイトル | P | Status |
|----|----------|---|--------|
| IMP-001 | QC repair 試行回数の永続化 | P0 | `done` |
| IMP-002 | marker と enqueue の atomic 化 | P0 | `open` |
| IMP-003 | 状態の正本整理 | P0 | `in_progress` |
| IMP-004 | runner.py 分割 | P1 | `open` |
| IMP-005 | status/purpose/step の型固定 | P1 | `open` |
| IMP-006 | executor/notify adapter 化 | P1 | `open` |
| IMP-007 | 信頼境界の明文化 | P2 | `open` |
| IMP-008 | stale recovery の改善 | P2 | `open` |
| IMP-009 | worktree/成果物 GC | P2 | `open` |
| IMP-010 | repair 意味的ガード | P2 | `open` |
| IMP-011 | パッケージ出荷形態 | P3 | `open` |
| IMP-012 | 運用メトリクス集計 | P3 | `open` |
| IMP-013 | takt 命名整理 | P3 | `open` |

---

## 変更履歴

| 日付 | 内容 |
|------|------|
| 2026-07-12 | 初版。システムレビュー結果を改善バックログ化 |
| 2026-07-14 | IMP-001 done。`qc_repair_attempts` を `RunState` / SQLite `runs` に永続化。IMP-003をrun状態のSQLite正本化に合わせて更新 |
