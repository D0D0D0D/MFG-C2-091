# Template Design Specification — MFG-C2-091 AssetInspectionHistory & Aging-Trend AdvisoryAgent

## Position in AgentCore Architecture

- **Agent Class**: `AssetInspectionHistoryAdvisoryGraph`
- **L1 Base**: `AgentBaseGraph`（main スロットに `GraphNode` を配置する Cat 2 標準構成）
- **Three-Layer Separation**:
  - State: flat TypedDict composition（Pydantic 不使用 — msgpack 非互換のため）
  - Node: L1 継承（Template Method: `execute(self, state: dict) -> dict` のみ override）
  - Graph: composition（`register_nodes()` によるノード差し替え）

## Architecture Overview

固定 5 ノード backbone（`initialize`/`finalize` は framework 自動注入。テンプレは
`pre_process` / `main` / `post_process` の 3 スロットを埋める）。

```
[維持管理担当/点検技術者]
  --(mode: ingest / invoke / feedback + payload)-->
  pre_process   入力検証・org_id 解決（テナント fail-closed）・設備プロファイル選択・S-2 入力ゲート
                （invoke 時は対象 asset_id の過去 inspections/interventions 履歴ロードも担う）
      |
  main (GraphNode → domain_workflow_graph)
      ├ ingest   : 異種フォーマット正規化 → (自由記述所見は LLM 構造化) → 3 系列 INSERT
      ├ invoke   : Stage1 経年差分検知(決定論) → Stage2 類似事例照合(検索) → Stage3 統合解釈(LLM)
      └ feedback : interventions 確定 UPDATE/INSERT + 進行捕捉の的中/空振り記録
      |
  post_process  S-3 出力ゲート（断定表現の抑制・免責付与・根拠なし断定の機械的除去・施設特定機微情報の秘匿）
      |
  finalize
[出力: 経年所見ドラフト＋優先度(Markdown/JSON) / 取込結果サマリ / 記録確定応答]
```

### Node Configuration（outer graph）

| Node | Responsibility | Input State | Output State | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------------|
| Marketplace adapter | `cli.py` が runner の `input_context` を破棄し、`mode="invoke"` と deployment の `org_id` / `asset_type` / `asset_id` で JSON envelope を再構築。Azure LLM は接続値を呼出しごとに解決 | AGENTIC STAR runner の単一 JSON 文字列（通常 envelope または `payload` のみ） | `AssetInspectionHistoryAdvisoryGraph.invoke()` | `MarketplaceGraph`（既存 Graph 継承、`invoke()` / `provision_secrets()` override） |
| initialize | セッション初期化・trace_id 発行 | — | — | InitializeNode (default) |
| pre_process | 入力検証・`org_id` 解決（fail-closed）・設備プロファイル選択・対象 `asset_id` の過去履歴ロード（invoke時）・S-2 入力ゲート | `user_input`（JSON文字列: `mode`, `org_id`, `payload`） | `validated_input`, `org_context`, `asset_profile`, `asset_history`, `status` | `PreProcessNode`（`FunctionNode` 継承） |
| main | `GraphNode` — 内側 `domain_workflow_graph` を `mode` で分岐実行 | `validated_input` | `delta_findings`, `candidate_cases`, `advisory_output`, `ingest_summary`, `feedback_result`, `status`（mode により実際に埋まるフィールドは異なる。詳細は次項「main（GraphNode）契約」参照） | `AssetWorkflowGraphNode`（`GraphNode` 継承） |
| post_process | S-3 出力ゲート・断定抑制・免責付与・**優先度の冒頭明示**（§ユーザビリティ設計レビュー 出力面 参照）・施設特定機微情報の秘匿・output 整形 | `advisory_output` / `ingest_summary` / `feedback_result`（mode 依存）＋ `delta_findings`（優先度導出用） | `formatted_output`, `status` | `PostProcessNode`（`FunctionNode` 継承） |
| finalize | 最終出力の確定 | — | — | FinalizeNode (default) |

### `main`（GraphNode）契約 — `AssetWorkflowGraphNode`

`src/examples/graph_cat2_sample.py` の `GraphNode` 契約（`get_subgraph()` / `extract_input()` /
`merge_output()` / `error_strategy` / `propagate_hitl`）に対する本テンプレートの設計:

| メソッド / 属性 | 設計方針 |
|---|---|
| `get_subgraph()` | `AssetWorkflowGraph`（`src/graph/domain_workflow_graph.py`）を `__init__` でキャッシュして返す（毎回新規生成せず、subgraph 構築コストを避ける） |
| `extract_input()` | `state.get("validated_input", state.get("user_input", ""))` — `pre_process` が正規化した JSON 文字列（`mode`, `org_id`, `asset_id`, `payload`, `org_context`, `asset_profile`, `asset_history` を含む）をそのまま inner graph へ渡す |
| `merge_output()` | inner graph の `get_output()` が返す `sub_result` から **mode に応じて実際に生成されたフィールドのみ**を outer state へ差分反映する: `/invoke` → `delta_findings`, `candidate_cases`, `advisory_output`, `status`／`/ingest` → `ingest_summary`, `status`／`/feedback` → `feedback_result`, `status`。空表示防止のため、mode に応じて存在しないフィールドのキー自体を返さない（`None` 埋めしない）。**新規 state キーを追加する際は `schemas/state.py`・`get_output()`・`merge_output()` の 3 箇所すべてを通す必要がある**（いずれか 1 つでも欠けると LangGraph の state 射影で黙って捨てられる） |
| `error_strategy` | `"propagate"`（デフォルト）— inner graph のエラーは `SubgraphError` として再送出し outer で `status=error` とする。安全に直結しうる誤所見を扱うため、grace的に空の所見を返すより明示的なエラー停止を優先する設計判断 |
| `propagate_hitl` | `False`（デフォルト）— 本テンプレートは HITL を必須としない。重大所見時は「専門家確認を推奨」という自然言語提示に留め、interrupt による一時停止は行わない。将来 HITL を追加する場合のみ `True` 化を検討する |

### Data Flow

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                            ↓ (RETRY, max 3)
                                          pre_process
```

`route()` は framework 標準（`state["status"]` を読み `success`→post_process / `retry`→pre_process /
他→finalize）。`add_edges()` / `route()` は override しない。

### AGENTIC STAR Marketplace entry boundary

Marketplace runner の `conversation_history` は S-2 の走査対象外の自由文なので、`cli.py` は受領した
`input_context` をマージせず空 dict に組み直す。外側 `PreProcessNode` は `mode` / `org_id` /
`asset_type`（invoke では `asset_id` も）を要求し、内側 `DispatchNode` と `_route_entry()`、さらに
`PostProcessNode` が `mode` を読むため、単一文字列を素通しする汎用 adapter では invoke が成立しない。

Marketplace は読取経路だけを公開する。`mode` は adapter 所有の `invoke` 固定、`org_id` /
`asset_type` / `asset_id` はそれぞれ `STG_DEFAULT_ORG_ID` / `STG_DEFAULT_ASSET_TYPE` /
`STG_DEFAULT_ASSET_ID` から呼出しごとに解決する。欠落、空、空白を含む値は `provision_secrets()` の
起動時検査と invoke 時の再解決の双方で fail closed とする。caller が送った scope は採用せず、
`ingest` / `feedback` 指定は `marketplace_mode_not_available` として既存 pre-process で拒否する。

## 経年所見の 3 段パイプライン（`/invoke` 内側 `domain_workflow_graph`）

> 「経年の変化に気づくのは決定論、意味と対応を説明するのが LLM」— 完全 LLM 任せへの明確な否定。
> Stage 1/2 を Tool 相当の決定論・検索処理として切り出し、Agent としての価値の核（LLM 必須性）を
> Stage 3 の統合解釈 1 点に集約する。

| Stage | 処理内容 | 実装方式 | 入力 | 出力 |
|---|---|---|---|---|
| **Stage 1: 経年差分検知** | ①同一資産の直近点検 vs 過去点検の構造化所見・計測値の差分検出（進行速度・変化量）／②設備プロファイルのアラーム閾値照合（§(a) 🔴 スカラ閾値スキーマ）／③重要度×進行速度の組合せによる緊急度シグナル抽出 | **決定論・非 LLM**（同一入力→同一出力・単体テスト可能） | 直近点検ログ + 対象 `asset_id` の `inspections`/`interventions` 履歴 + 設備プロファイル | 構造化「経年差分シグナルリスト」（各要素は `finding_id`（`fnd-{asset_id}-{metric}-{inspected_at}` の決定論的生成。各構成要素は `[0-9A-Za-z_-]` にサニタイズする）, `metric`, `delta`, `progression_rate`, `severity` を持つ配列） |
| **Stage 2: 類似事例照合** | ①自組織 `interventions` KB の類似条件検索（劣化モード・環境条件類似度による近傍探索）／②設備プロファイルの劣化モード知識引き当て | **検索**（LLM 推論を介さない） | Stage 1 の経年差分シグナルリスト（`finding_id` 込み） + 自組織 `interventions` + 設備プロファイル | 「候補類似事例＋根拠レコード ID」のリスト（各要素は `matched_intervention_id`（`interventions.intervention_id`）または `matched_finding_id`（Stage1 の `finding_id`）のいずれかを根拠として保持） |
| **Stage 3: 統合解釈** | 経年差分×類似事例×設備プロファイル劣化知識を統合し、非断定所見・優先度・推奨対応を言語化 | **ここだけ LLM** | Stage 1 の経年差分シグナルリスト（`finding_id` 込み） + Stage 2 の候補類似事例＋根拠 ID | 非断定の経年所見ドラフト＋優先度（自然言語＋`finding_id`/`intervention_id` 引用） |

**Stage 3 の制約（S-3 出力ゲートで機械的に強制）**:
- LLM は Stage 1/2 が提示した材料にないリスクを追加できない（プロンプトで「根拠 ID 必須・提示ソースのみ
  参照」を制約）。
- 全言明に根拠 ID（Stage 1 の `finding_id` or Stage 2 の `matched_intervention_id`）の引用を必須とする。
  各根拠 ID は Stage 1/2 の出力に実在する ID のみが許可され、S-3 が Stage1/2 の出力リストと突合して
  検証する（存在しない ID を LLM が捏造した場合も検出対象）。
- S-3 出力ゲート（`_extra_security_gate_output()`）で根拠なし断定・存在しない根拠 ID 引用・確定的な
  健全性区分表現（「区分II」等の断定）を機械的に検出し、該当文を除去または「要専門家確認」に置換する。
- 確定判定・署名・最終責任は有資格技術者に帰属する旨を出力に必ず付与し、エージェントが健全性を
  確定判定しないことを明示する。

## `/ingest` `/feedback` の内部ノード構成（`domain_workflow_graph` 内トポロジ）

`/invoke` の 3 段パイプライン（Stage1〜3）に加え、inner graph（`AssetWorkflowGraph`）は
`mode` 分岐により以下の 2 系統も実行する。いずれも BaseGraph 継承・カスタムトポロジ
（`src/examples/domain_workflow_graph_sample.py` 相当）とし、mode ごとに実行するノード集合を切り替える。

### `/ingest`（過去記録の取込・正規化）

```
START → normalize_input → (自由記述は structure_with_llm) → validate_records → write_records → END
```

| ノード | 責務 | LLM 使用 |
|---|---|---|
| `normalize_input` | 異種フォーマット（手書き報告書OCR結果・Excel由来JSON・点検管理システムエクスポート）を共通スキーマ候補へ変換 | 不使用（決定論の列マッピング） |
| `structure_with_llm` | 自由記述の点検所見メモ（`free_note` 相当）のみを対象に、構造化フィールド（発見日・部位・欠陥種別・程度等）を抽出 | **使用**（構造化抽出のみ。経年解釈は行わない） |
| `validate_records` | 必須フィールド充足チェック（§(b) 最小データ要件）。不足行はエラーリストへ | 不使用 |
| `write_records` | `assets` / `inspections` / `interventions` へ INSERT。書込件数・エラー行を `ingest_summary` へ集約 | 不使用 |

### `/feedback`（実際の補修結果の記録）

```
START → resolve_intervention → update_intervention → reconcile_prediction → END
```

| ノード | 責務 | LLM 使用 |
|---|---|---|
| `resolve_intervention` | 対象 `intervention_id`（新規なら採番）を解決 | 不使用 |
| `update_intervention` | `outcome_narrative` / `post_condition` / `action` / `status` を確定 UPDATE または新規 INSERT | 不使用（構造化入力を前提。自由記述メモがある場合は `/ingest` と同じ `structure_with_llm` 相当ステップを再利用） |
| `reconcile_prediction` | 直近の `/invoke` `advisory_output`（あれば）と実際の補修結果を突合し、進行捕捉の的中/空振りを `feedback_result` に記録。次回 Stage 2 照合の精度向上シグナルとして扱う | 不使用（決定論の突合ロジック） |

`/ingest` `/feedback` はいずれも書込副作用を持つため、S-1 trust level は `VERIFIED_EXTERNAL` 以上を
要求する（§セキュリティ設計方針 参照）。

### 🔴 inner graph の state 型注釈方針（実装契約・必須）

inner graph（`AssetWorkflowGraph`）は `mode`（`ingest`/`invoke`/`feedback`）による条件分岐を行う。
このとき **分岐条件関数および `get_output()` の `state` 引数の型注釈** について、以下を実装契約
として拘束的に定める。

- inner graph は **自身の state スキーマ `AssetWorkflowState`** を持つ。**この型名は本 Design
  ドキュメントで確定し、改名せずこの名前で実装する。** `src/graph/domain_workflow_graph.py` に、
  outer の `State`（§State Definition のフィールド契約）と同一のカスタムフィールド
  （`mode` / `org_id` / `asset_id` / `delta_findings` 等）を含む形で定義する。
- **`mode` 分岐の条件関数（`add_conditional_edges()` が参照する route 関数）および `get_output()` は、
  `AgentState` ではなく `AssetWorkflowState` で `state` を型注釈しなければならない。**
- 理由: LangGraph の内側グラフで route 関数の `state` 引数を `AgentState`（基底スキーマ）で注釈すると、
  **状態射影によってカスタムフィールド（本件では `mode` 等）が脱落し、`mode` を読めずに誤 route する**。
- この制約は、型注釈と mode 分岐を検査するテストで継続的に検証する。

## State Definition（`src/schemas/state.py` — `AgentState` 継承 flat TypedDict）

> `src/schemas/state.py` の `State(AgentState)` は、下表のフィールド契約に従う。

| Field | Type | Purpose | Required |
|-------|------|---------|----------|
| `validated_input` | `str` | `pre_process` が検証・正規化した入力 JSON 文字列（`mode`/`org_id`/`asset_id`/`payload` 等を含む）。`main`（GraphNode）の `extract_input()` が読む | No（`pre_process` 実行後に設定） |
| `mode` | `str` | `"ingest"` / `"invoke"` / `"feedback"` の 3 値 | Yes |
| `org_id` | `str` | テナント識別子（fail-closed の起点） | Yes |
| `asset_type` | `str` | 設備プロファイル参照キー（例: `"bridge_appurtenance"`, `"rotating_machine"`, `"water_pipe"`） | Yes |
| `asset_id` | `str` | `/invoke` `/feedback` 時の対象資産識別子 | No（`/ingest` 時は不要） |
| `org_context` | `dict` | `pre_process` が解決した組織メタ情報 | No |
| `asset_profile` | `dict` | 選択された設備プロファイル（アラーム閾値・劣化モード知識・対象種別固有の点検/処置種別） | No |
| `asset_history` | `dict` | `/invoke` 時にロードした対象資産の過去 `inspections`/`interventions` 履歴 | No |
| `delta_findings` | `list[dict]` | Stage 1 が生成した経年差分シグナルリスト（各要素に `finding_id` を含む） | No |
| `candidate_cases` | `list[dict]` | Stage 2 が生成した候補類似事例＋根拠 ID リスト（`matched_intervention_id` / `matched_finding_id`） | No |
| `advisory_output` | `dict` | Stage 3 が生成した非断定経年所見＋優先度（`finding_id`/`intervention_id` 引用込み） | No |
| `ingest_summary` | `dict` | `/ingest` の取込結果サマリ（件数・エラー行・正規化後レコード ID 等） | No |
| `feedback_result` | `dict` | `/feedback` の確定結果（更新された `intervention_id`・的中/空振り照合結果） | No |
| `formatted_output` | `dict` | `post_process` が S-3 出力ゲート適用後に整形した最終出力（呼び出し元へ返る値） | No（`post_process` 実行後に設定） |
| `status` | `str` | ノードが返す実行状態。**`AgentStatus` の小文字文字列値のみ**（下記 🔴 参照） | Yes |
| `degradation_reason` | `str` | 縮退動作が起きた理由（`"llm_unavailable"` / `"insufficient_inspection_history"` 等）。正常時は未設定 | No |

### 🔴 `status` の有効値（実装契約・必須）

`status` に代入してよいのは framework の `AgentStatus` enum が定義する値**のみ**であり、
本テンプレートで使用するのは次の 3 値に限る。

| 値 | 用途 | `AgentBaseGraph.route()` の遷移先 |
|---|---|---|
| `"success"` | 正常完了（**縮退動作を含む** — 下記参照） | `post_process` |
| `"retry"` | 一時障害。`pre_process` からやり直す価値がある場合のみ | `pre_process`（`max_retry` まで） |
| `"error"` | 恒久障害・fail-closed（`org_id` 解決失敗等）。リトライしない | `finalize` |

- **`"degraded"` は `AgentStatus` に存在しない。絶対に `status` に代入してはならない。**
  `route()` は `AgentStatus(state["status"])` で値を解決するため、enum 外の文字列は
  `UnknownStatusError` を送出し、パイプラインが実行時に停止する。
- **縮退動作（グレースフルデグレード）は `status="success"` のまま**、別フィールド
  `degradation_reason` に理由を格納し、`post_process` が出力の冒頭に縮退した旨を明示することで表現する。
- `AgentStatus` の残る 4 値（`pending` / `timeout` / `awaiting_human` / `cancelled`）は本テンプレートでは
  使用しない（`pending` は framework が初期値として設定するもので、ノードから返してはならない。
  `awaiting_human` は HITL 用途で本テンプレートは `hitl.enabled: false`）。

**State Constraints（必須）**:
- Flat TypedDict のみ（primitives + JSON シリアライズ可能な型）
- JWT / API キー / credential を State に置かない（checkpoint DB 漏洩を防ぐ）
- `InvocationContext` は `config["configurable"]` 経由のみ（State に含めない）
- Pydantic モデル・dataclass・任意 Python オブジェクトは禁止（msgpack 非互換）
- センシティブ値（施設特定情報・重大欠陥箇所を含む `interventions` の個別詳細等）はグラフ内ノード戻り値に
  直接載せない。API アダプタ層でのみ参照 ID から解決し、checkpoint 永続化への漏洩を防ぐ。
- 上表の `Type` 列は `src/schemas/state.py` の TypedDict 型注釈と一対一で対応させる。
  「上表の全フィールドが宣言済みか」「msgpack 非互換型が混入していないか」を PB-2（State serialization）
  テストで検証する

## データモデル概要（エージェントがスキーマを設計・所有）

共通スキーマ 3 系列 + 設備プロファイルを本セクションのデータモデルとして定義する。
設備種別差は**スキーマではなく設備プロファイル（config/KB）**で吸収する。

```
assets          設備台帳（マスタデータ）
  asset_id, org_id, asset_type(bridge_appurtenance/rotating_machine/water_pipe/...),
  installed_year, spec(JSON), location_context, importance, status

inspections     点検履歴時系列
  inspection_id, org_id, asset_id, inspected_at, inspector, method(visual/hammering/measurement/...),
  findings(構造化), measurements(JSON), rating(optional), free_note(LLM構造化)

interventions   補修・処置と結果（経過記述）★moat の核
  intervention_id, org_id, asset_id, decided_at, action(observe/repair/detailed_investigation/replace),
  triggering_inspection_id, work_detail, cost,
  outcome_narrative(構造化経過記述), post_condition, confirmed_by(human)
```

- 設備種別差は enum ではなく**設備プロファイル参照（`config/asset_types/{asset_type}.yaml`）**とし、
  対象種別ごとの点検項目・アラーム閾値・劣化モード知識・処置種別を定義する。新規設備種別への対応 =
  プロファイル追加のみ（コード変更なし）。
- `interventions.outcome_narrative` は「補修実施結果の経過を構造化ナラティブとして記録・蓄積する」概念。
  施設特定機微情報（正確な位置座標・重大欠陥箇所の詳細）は S-2/S-3 で秘匿対象とする（下記 §(d) 参照）。
  PII 該当は点検者氏名程度で S-2 で除去する。

## 🔴 設計チェックリスト（実装前に確定した事項）

### (a) 初期対象設備種別の選定と設備プロファイル整備方針

**初期リリースは代表2〜3種別に絞ってプロファイル整備し実証する**。ただし以下の初期3種別は
**あくまでサンプル（初期同梱セット）**であり、本エージェントの対象範囲を限定する
ものではない。**対象は保守点検記録が存在する設備全般に拡張されうる前提**で設計する（スキーマ・
パイプラインのどこにも特定設備種別を仮定しない — §データモデル概要の設備プロファイル分離がその実装手段）。

- **初期同梱サンプル（PoC 想定の初期セット）**: `bridge_appurtenance`（橋梁付属物 — 高欄・伸縮装置・
  支承等。橋梁本体の画像診断・様式記載は Dr.Bridge 領域のためスコープ外だが、付属物のテキスト/数値点検
  ログは対象）、`rotating_machine`（回転機器 — ポンプ・送風機等の設備点検）、`water_pipe`（上下水道管路）
  の3種別。選定理由: (1) いずれも法定・定期点検の記録が既存業務として存在し `/ingest` の入力が確保
  しやすい、(2) 橋梁×画像×様式の競合領域（Dr.Bridge/ドコモ）と明確に棲み分けられる、(3) 対象施設の
  性質が互いに異なり（構造物/機械/管路）設備プロファイル抽象化の妥当性を早期検証できる。
- **拡張手段**: 新規設備種別への対応は、設備プロファイル（`config/asset_types/{asset_type}.yaml`）を
  1本追加するだけで完了し、**コード変更を伴わない**。プロファイルの追加はテンプレート提供側だけでなく
  **運用者・導入組織側でも可能**とする（変電設備・鉄塔・ビル設備・トンネル付帯設備等、保守点検記録が
  存在する設備なら同一スキーマ・同一パイプラインで受け入れられる）。これが本テンプレートの再利用性の
  根拠となる。
- **未知 `asset_type` の挙動**: プロファイル未整備の `asset_type` が `/ingest`・`/invoke` された場合は
  エラーにせず、履歴の蓄積・経年差分検知は共通スキーマ上でそのまま実行し、プロファイル依存の判定
  （アラーム閾値照合・劣化モード知識引き当て）のみをスキップして設備種別一般の注意喚起へ縮退する
  （`status="success"` + `degradation_reason="asset_profile_not_found"`。§(b) の縮退設計と同一機構）。
- 各プロファイル（`config/asset_types/{asset_type}.yaml`）の主な内容: 監視対象メトリクスとアラーム
  閾値（下記 §(a)-1 参照）、想定される劣化モードリストと環境トリガー知識、対象種別固有の点検・処置種別
  辞書。初期サンプル3種は公開文献（国交省道路メンテナンス年報・各種設備保全指針）ベースで整備し、
  テンプレートに同梱する。

#### 🔴 メトリクスのアラーム閾値スキーマ（実装契約・必須）

Stage 1 の範囲照合は、**OPC UA `LimitAlarmType` 準拠のスカラ閾値**（OPC 10000-9 §5.8.11）で表現する。
各メトリクスは以下 4 つの閾値を**任意**に
宣言し、順序制約 `low_low_limit ≤ low_limit ≤ high_limit ≤ high_high_limit` を満たすものとする。

```yaml
# config/asset_types/bridge_appurtenance.yaml（例）
metrics:
  expansion_joint_gap_mm:
    unit: "mm"
    high_limit: 40
    high_high_limit: 60
  corrosion_area_ratio:
    unit: "ratio"
    high_limit: 0.15
    high_high_limit: 0.30
degradation_modes:
  - mode_id: "expansion_joint_wear"
    trigger_metrics: ["expansion_joint_gap_mm"]
    typical_progression: "..."
intervention_types:
  - "observe"
  - "detailed_investigation"
  - "repair"
  - "replace"
```

### (b) `/ingest` が前提とする最小データ要件

データ不足時の縮退動作を次のとおり定義する。

- **最小データ要件**: 同一資産（`asset_id`）につき点検記録2回以上 × 補修履歴（`interventions`）1件以上。
  この最小要件を満たさない資産は Stage 2（類似事例照合）の自組織 KB 対象から除外し、Stage 3 は
  「設備種別一般の劣化モード知識」に基づく注意喚起へ**縮退**する（`status="success"` + `degradation_reason
  ="insufficient_inspection_history"`。上記 State Definition 参照）。
- データが薄い導入初期はこの縮退動作が標準的に発生する前提とし、蓄積が進むほど自組織履歴照合（Stage2）
  の比重が上がるグレースフルデグレード設計とする。
- `/ingest` の `validate_records` ノードで必須フィールド（`asset_id`, `inspected_at`, 少なくとも1つの
  `findings` または `free_note`）を検証し、不足行は書き込まずエラーリストへ回す（データ品質検証は
  決定論・LLM不使用）。

### (c) 誤所見に対する S-3 出力ゲート設計（根拠ID必須化・断定表現の機械的除去）

経年進行度の誤った所見が安全上の問題につながりうるため、次の保護を実装する。

- **根拠 ID 必須化**: Stage 3 の出力する全所見文には `finding_id` または `matched_intervention_id` の
  引用を必須とする。`_extra_security_gate_output()` が Stage1/2 の出力リストと突合し、(1) 引用元IDが
  実在しない、(2) 根拠引用のない断定的所見文、のいずれかを検出した場合は当該文を除去または
  「要専門家確認」に置換する（Stage 3 セクション参照）。
- **断定表現の抑制**: 「確定」「間違いなく」「区分II/III/IV」等の確定的健全性区分表現を正規表現ベースで
  検出し、非断定表現（「〜の可能性がある」「〜が疑われる」）へ機械的に書き換えるか、検出不可の場合は
  「専門家確認推奨」を追記する。
- **免責付与**: 出力の末尾に「本所見は非断定のドラフトであり、健全性の確定判定・署名は有資格技術者の
  責任である」旨の定型文を必ず付与する（`post_process` ノードで実装）。
- **重大所見のエスカレーション**: 資産重要度（`assets.importance`）高 × 進行速度（`progression_rate`）
  急速の組合せを Stage 1 が検出した場合、Stage 3 の出力冒頭に「詳細調査・専門家確認を推奨」を明示する
  （HITL interrupt は使わない）。

### (d) 施設特定機微情報・重大欠陥箇所の取扱い（テナント org_id 分離含む）

施設特定情報と重大欠陥箇所の漏洩を防ぐため、次の保護を実装する。

- **テナント分離（fail-closed）**: `pre_process` は `org_id` を解決できない場合、即座に `status="error"`
  で fail-closed とする（データの越境参照を防ぐ）。Stage 2 の類似事例照合は常に `org_id` でスコープされた
  `interventions` のみを検索対象とする（他組織のデータへの参照は設計上不可能）。
- **施設特定機微情報の秘匿**: 正確な位置座標・重大欠陥箇所の詳細記述は `_extra_security_gate_output()`
  で検出し、出力からは資産種別・重要度レベル・進行傾向という抽象化された情報のみを含める設計とする
  （個別施設の脆弱性が外部漏洩するリスクを低減）。
- **センシティブ値はグラフ内ノード戻り値に載せない**（§State Definition 参照）。API アダプタ層のみが
  参照 ID から実際の詳細情報を解決する。

## セキュリティ設計方針

- **S-1 required_trust_level**: `pre_process`（org_id解決・書込のトリガ）/ `main`（GraphNode 経由で
  `/ingest` `/feedback` の書込副作用を持つ） は `VERIFIED_EXTERNAL` を宣言する。`/invoke`（読み取り主体）
  も一律 `VERIFIED_EXTERNAL`（組織固有の点検履歴という機微情報を扱うため `ANONYMOUS` は不可）。
- **S-2 `_extra_security_gate_input()`**: `org_id` 形式検証・`payload` サイズ上限・PII（点検者氏名等）の
  追加スキャン。
- **S-3 `_extra_security_gate_output()`**: 上記 §(c) の根拠ID検証・断定表現抑制・§(d) の施設特定機微情報
  秘匿を実装する。preservation variant ではなく content-filtering variant（安全でない断定表現をブロック
  する側）として実装する。
- **S-4 `emit_trace_event()`**: 各ノード `execute()` 内で最低1回。`pre_process`（org_id解決イベント）、
  `main`（ingest/invoke/feedback 各モードの処理完了イベント）、`post_process`（出力ゲート適用結果イベント）
  で発火する。
- **Import Isolation**: `framework/` と `shared/` のみ import。`agenticstar` 直 import・他テンプレ直import
  はしない。

### Composition Pattern

- **Pattern**: `GraphNode`（subgraph — `domain_workflow_graph.py` の `AssetWorkflowGraph`）
- **Composition target**: `src/graph/domain_workflow_graph.py`（inner graph）
- **Error propagation strategy**: propagate（`error_strategy="propagate"`。上記 main 契約参照）

## Import Isolation Confirmation
- [x] Template does not import agenticstar-platform SDK (Level 0)
- [x] Import targets: framework/ and shared/ only (no agents/base/ required)

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | **AgentBaseGraph** | 固定パイプライン（3段構成）であり自律的な終了判断を要しない。Cat 2 標準構成 |
| Composition pattern | Standalone (BaseNode) | GraphNode (subgraph) | **GraphNode** | mode(ingest/invoke/feedback)ごとの内部トポロジが大きく異なり、内側 subgraph として分離することで見通しと保守性を確保 |
| HITL 採用 | interrupt() 有効化 | 非採用 | **非採用**（`hitl.enabled: false`） | 重大所見でも断定・自動判定はせず、一時停止よりも出力メッセージでの専門家確認推奨で十分と判断。将来必要になれば追加検討 |
| エラー伝播戦略（GraphNode） | propagate | handle (grace的縮退) | **propagate** | 誤った経年進行判断が安全上の問題につながりうるため、内側グラフの真のエラーは隠さず明示的に停止する。データ不足による縮退（§(b)）とは区別し、縮退は `status="success"` + `degradation_reason` で表現する |
| 最小データ要件 | 点検1回以上で照合実施 | 点検2回以上×補修履歴1件以上 | **点検2回以上×補修履歴1件以上** | 経年"差分"を検知するには最低2時点の点検が必須。満たさない場合は §(b) の縮退動作へ |

## ユーザビリティ設計レビュー（実施記録）

エンドユーザー（維持管理担当・点検技術者）のユースケースを具体的に想定して設計を自己レビューし、
以下の修正を反映した。

- **入力面**: `/ingest` は手書き報告書・Excel・点検管理システムエクスポートという「ユーザーが実際に
  手元に持っている」異種フォーマットをそのまま受け付ける設計とし、事前の構造化整形をユーザーに要求
  しない（`normalize_input` + `structure_with_llm` が正規化を担う）。過剰な構造化入力の要求を避けた。
- **出力面**: `advisory_output` は「変化点＋進行速度＋推奨対応＋優先度」を先頭に明示する構成とし、
  受け取った維持管理担当がそのまま補修計画会議の判断材料として使える形にする（post_process が
  優先度を出力冒頭へ配置。上記 Node Configuration 表に反映）。
- **対話面**: 本エージェントは HITL 壁打ちループを正常系として設計しない。経年所見は「1回のinvokeで
  完結する判断材料提示」がユースケースの本質であり、対話的絞り込みを必要としないためである。ただし
  `/invoke` を同一資産に対して繰り返し呼び出す運用
  （複数回点検のたびに呼ぶ）は正常系の使い方であり、回数制限は設けない。
- **エラー/フォールバック面**: `/ingest` データが薄い場合は §(b) の縮退動作（自組織照合→設備種別一般の
  劣化知識ベースの注意喚起）で常に何らかの有用な出力を返す設計とし、「データ不足のためエラー」で
  終わらせない。

## Framework Utilization

### Shared Components Used
- [x] InvocationContext (correlation_id, session_id, permissions, credential handle)
- [x] ConnectionPolicy (retry/timeout strategy)
- [x] SecurityViolationError
- [x] S-2: `_extra_security_gate_input()` — 上記セキュリティ設計方針参照
- [x] S-3: `_extra_security_gate_output()` — 上記 §(c) 参照
- [x] S-4: `emit_trace_event()` — 上記セキュリティ設計方針参照

> **S-2/S-3 gate behaviour by node type (ADR-017)**:
> - `FunctionNode` subclass（`pre_process`/`post_process`）→ framework `@final` gate が自動実行。
>   `_extra_security_gate_input()` / `_extra_security_gate_output()` のみで拡張する
> - `GraphNode`（`main`）→ 意図的な no-op（inner graph 側で S-2/S-3 相当のドメインチェックを実施済みの
>   前提。inner graph 内の各ノードは通常の `BaseNode`/`FunctionNode` として実装するため、実質的には
>   inner 側でも同じ ADR-017 契約が適用される）
