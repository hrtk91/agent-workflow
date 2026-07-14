# agent-workflow 作業メモ

## 未解決の課題

### QC修復contextをSQLite正本へ移す

- Status: `planned`
- 背景: PR #4のレビューで、QC修復回数をSQLiteへ保存する処理と`context.md`への追記が別処理のため、途中停止時に両者が不一致になる可能性が指摘された。
- 方針: QC修復イベントをSQLiteへ保存し、`context.md`はexecutorへ渡すための投影ファイルとしてatomic・冪等に生成する。
- 必須条件:
  - `run_id`と修復attemptを一意キーにして重複追記を防ぐ
  - `resume` / `retry`でDB上のイベントと`context.md`の不足分を再生成できる
  - DB保存前後、context生成途中の停止を再現する異常系テストを追加する
- スコープ外: ユーザー入力を含む全task packet（`task.md`、`acceptance.md`、`constraints.md`、`context.md`）のDB移行。これは別課題として扱う。
