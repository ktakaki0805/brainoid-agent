# 担当別の依頼文

必要な値を埋めて子エージェントへ渡す。子エージェントにはこのワークフロー全体を再実行させず、指定した担当作業だけを依頼する。

## 実装・修正担当

```text
あなたはチケット {ticket_id} の実装・修正担当です。
作業ディレクトリ: {absolute_worktree}
開始時のbase commit: {base_commit}
既存変更と触れない範囲: {existing_changes}

適用されるAGENTS.mdとCONTRIBUTING.mdを確認してください。
docs/08-implementation-tickets.md の対象詳細節、関連する要件・設計・schemaを読み、
この1件の受入条件と検証方法を満たす変更を実装してください。
今回の制約・外部前提: {constraints}
受入条件と予定する検証: {acceptance_checks}
修正ラウンドの場合のレビュー指摘: {findings_or_none}

製品コード・テスト・関連仕様の変更を担当してください。
commit、push、台帳・作業ログの更新、別エージェントへの委譲は親が担当します。
他チケットの実装は含めないでください。
完了時には変更ファイル、受入条件ごとの対応、実行したコマンドと結果、
未実施と理由、指摘ごとの対応または反論の根拠を返し、編集を止めてください。
```

## 独立レビュー担当

実装担当の会話履歴や「問題なし」といった結論は渡さない。前回指摘は再確認に必要な事実として渡してよい。

```text
あなたはチケット {ticket_id} の独立レビュー担当です。
作業ディレクトリ: {absolute_worktree}
base commit: {base_commit}
今回の差分の取得方法・対象パス（未追跡ファイルを含む）: {review_scope}
除外する既存変更: {existing_changes}
検証コマンドと観測結果: {verification_evidence}
前回指摘と修正内容（初回はなし）: {previous_findings}

適用されるAGENTS.md、CONTRIBUTING.md、docs/05-development-and-testing.mdの完了条件、
docs/08-implementation-tickets.mdの対象詳細節と関連設計・schemaを直接確認してください。
要件を実装担当の説明だけから推定しないでください。
受入条件、検証の十分性、回帰、依存契約、対象外への拡張、変更が触れる認可・削除・
外部副作用を、最新差分全体と周辺コードから確認してください。
必要なローカル検証は行えますが、製品コード・index・台帳・ログは変更せず、commitしないでください。
他のエージェントへの委譲も行わないでください。

次の形式で結果を返してください。
- verdict: approve / request_changes / blocked
- 受入条件ごとの充足状況と根拠
- 要修正の指摘: ID、重要度、file:line、問題・影響、必要な修正または検証
- 前回指摘ごとの解消状況
- 自分で実行した検証と、提供された結果だけを確認した検証の区別
- 任意の改善提案（要修正指摘と区別）

要修正指摘または未検証の必須条件があればapproveにしないでください。
```
