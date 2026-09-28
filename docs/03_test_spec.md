# Test Specification

## Test Strategy
- Coverage target: TC-01〜TC-11 + PB-1〜PB-6 全項目 + ドメイン固有ビジネスロジック
- Test types: Unit (`tests/unit/`) / Integration (`tests/integration/`) / Proof-of-Boundary (`tests/proof_of_boundary/`)
- LLM / 外部ストア（asset_store）はすべて注入シームで fake 差替え（実接続は STG）

## Framework Compliance Tests (Mandatory)

| TC-ID | Test | Expected Result | Result |
|-------|------|----------------|--------|
| TC-01 | State contract: flat TypedDict | Type check pass, no Pydantic/dataclass | PASS（`src/schemas/state.py` は `AgentState` 継承 flat TypedDict のみ） |
| TC-02 | SecurityViolationError fires on invalid input | Error raised | PASS（`tests/unit/test_pre_process_node.py` — org_id fail-closed / malformed JSON / unknown mode） |
| TC-03 | No JWT/Credential in State | CI の credential スキャン: 0 violations | PASS（`src/` にリテラル credential 無し。mock 値は tests/ のみ） |
| TC-04 | InvocationContext via configurable only | Direct access raises error | PASS（`src/api/server.py` のみが `InvocationContext` を構築し `config["configurable"]` 経由で渡す） |
| TC-05 | S-4: no duplicate lifecycle events in `execute()` | `node_start` / `node_complete` / `node_error` absent from `execute()` body | PASS（`tests/proof_of_boundary/test_pb_invoke_order.py`） |
| TC-06 | S-2: `_security_gate_input()` not overridden (`FunctionNode` subclass) | `TypeError` raised at class definition if overridden | PASS（`tests/proof_of_boundary/test_security_gate_non_bypassable.py`） |
| TC-07 | S-3: `_security_gate_output()` not overridden (`FunctionNode` subclass) | `TypeError` raised at class definition if overridden | PASS（同上） |
| TC-08 | `required_trust_level` declared and enforced — dedicated S-1 boundary coverage MUST call `node(state)` (via `__call__`) for every privileged node, or use equivalent parameterized coverage | Insufficient trust level → execution refused *before* `execute()`; test must prove `execute()` was not called. Direct `node.execute(state)` is permitted only for isolated business-logic unit tests: it does not satisfy TC-08, but is not a finding when TC-08 and PB-6 coverage pass. | PASS（`tests/proof_of_boundary/test_trust_gate.py` + `tests/proof_of_boundary/test_pb_invoke_order.py::test_s1_denial_refuses_execution_before_execute`） |
| TC-09 | S-2: `_extra_security_gate_input()` non-trivial when domain checks needed | Domain-specific input checks execute correctly | PASS（`PreProcessNode._extra_security_gate_input` — org_id 形式検証・payload サイズ上限・点検者氏名 PII マスク。`tests/unit/test_pre_process_node.py::test_s2_masks_inspector_name_pii_in_free_note`） |
| TC-10 | S-3: `_extra_security_gate_output()` non-trivial when domain checks needed | Domain-specific output checks execute correctly | PASS（`PostProcessNode._extra_security_gate_output` — 根拠ID検証・断定表現抑制・免責付与・施設特定機微情報秘匿・client-echo サニタイズ。`tests/unit/test_post_process_node.py`） |
| TC-11 | S-4: at least one domain `emit_trace_event()` inside each `execute()` | Domain event emitted on every invocation path | PASS（`tests/proof_of_boundary/test_audit_logging.py` — 全ノードモジュールで static scan） |

## Proof-of-Boundary Tests (Mandatory)

| PB-ID | Boundary | Test | Expected Result | Result |
|-------|----------|------|----------------|--------|
| PB-1 | BaseNode → EventEmitter | `emit_trace_event()` fires on every invocation path | No silent failures | PASS |
| PB-2 | State serialization | Post-invoke State is primitives only | No Pydantic/dataclass | PASS（`tests/proof_of_boundary/test_state_safety.py`） |
| PB-3 | L1 → External service (asset_store) | Template connects to asset_store via L1 framework | Real data retrieval（InMemoryAssetStore を注入し read/write を実演。STG では実DBストアに差替え） | PASS（`tests/integration/test_graph.py` ingest→invoke→feedback ライフサイクル） |
| PB-4 | Import isolation | No Level 0 imports | AST scan: 0 violations | PASS（`tests/proof_of_boundary/test_import_isolation.py`） |
| PB-5 | Checkpoint safety | No JWT/Pydantic in checkpoint | Inspection pass | PASS |
| PB-6 | Invoke execution order | S-1→S-4 node_start→S-2→execute()→S-3→S-4 node_complete | Order verified | PASS（`tests/proof_of_boundary/test_pb_invoke_order.py` — 全ノード自動発見・実行順検証。GraphNode 除外はコメントで根拠明記） |

HITL 非採用（`hitl.enabled: false`）のため PB-7 は対象外。

## Business Logic Tests（ドメイン固有）

| TC-ID | Test | Input | Expected Result | Result |
|-------|------|-------|----------------|--------|
| BL-01 | Stage1 経年差分検知: スカラ閾値照合（OPC UA LimitAlarmType） | `value` が `high_limit`/`high_high_limit` 超過 | `severity` = medium/critical、境界値は厳密不等号（`test_value_exactly_on_a_limit_is_not_a_signal`） | PASS |
| BL-02 | Stage1 `finding_id` 生成の決定論性・ASCII安全性 | 同一入力を複数回実行 / 非ASCII asset_id | 同一 `finding_id`、S-3 evidence-ID パターンと完全一致 | PASS |
| BL-03 | §(b) 最小データ要件による縮退 | 点検<2回 or 補修履歴<1件 | `degradation_reason` に `insufficient_inspection_history` を含む（複数理由は `+` 結合で両立） | PASS |
| BL-04 | §(a) 未知 `asset_type` の縮退（fail-closed ではない） | 未登録 `asset_type` | `status="success"` + `degradation_reason` に `asset_profile_not_found`、org_id fail-closed とは異なる扱い | PASS |
| BL-05 | Stage2 類似事例照合（検索・LLM不使用） | 自組織 interventions + 設備プロファイル劣化モード | `matched_intervention_id` / `matched_finding_id` いずれかを根拠として保持 | PASS |
| BL-06 | Stage3 LLM 統合・fallback | `llm=None` / LLM 例外 | `status="success"` + `degradation_reason="llm_unavailable"`（既存の縮退理由は上書きしない） | PASS |
| BL-07 | S-3 根拠ID検証（捏造ID検出） | 実在しない `fnd-`/`inv-` ID を引用 | `[要検証]` へ置換 | PASS |
| BL-08 | S-3 断定表現抑制 | 「区分II」「確定です」等 | 非断定表現へ書換 or 「要専門家確認」 | PASS |
| BL-09 | S-3 免責付与 | 任意の `/invoke` 出力 | 出力末尾に免責定型文を必ず付与 | PASS |
| BL-10 | S-3 施設特定機微情報秘匿 | 緯度経度座標を含むフィールド | `[施設特定機微情報のためマスク]` に置換 | PASS |
| BL-11 | S-3 client-echo サニタイズ（credential/evidence-ID lookalike/制御指示） | 捏造ID・`sk-ant-...`・「直ちに...してください」を含む client 制御フィールド | いずれも出力から漏洩しない | PASS |
| BL-12 | `degradation_reason` が呼び出し元へ確実に到達する（framework `get_output()` の落とし穴） | 縮退動作発生時 | `output.degradation_reason` / `output.degradation_notice_ja` に反映（`AgentBaseGraph.get_output()` は `formatted_output` のみ透過するため post_process で明示的に畳み込み） | PASS（実装中に発見・修正した回帰） |
| BL-13 | inner graph の mode 分岐（state 型注釈） | `mode=invoke/ingest/feedback` | `AssetWorkflowState` 型注釈で正しく route。誤って `AgentState` 注釈にすると `mode` が射影で脱落し誤 route | PASS |
| BL-14 | `/ingest` 異種フォーマット正規化（列名エイリアス） | 手書き報告書/Excel由来の表記ゆれ | 共通スキーマ（asset_id/inspected_at/findings 等）へ正規化 | PASS |
| BL-15 | `/ingest` 最小データ要件（必須フィールド） | asset_id/inspected_at 欠落、findings/free_note 両方欠落 | 書込対象から除外しエラー行へ | PASS |
| BL-16 | `/feedback` 進行捕捉の的中/空振り判定 | 直近 advisory と実際の action の突合 | hit / false_positive_candidate / no_prior_advisory / inconclusive | PASS |
| BL-17 | 資産種別非依存スコープ（初期3サンプル） | bridge_appurtenance / rotating_machine / water_pipe | いずれも `/invoke` が成功しコード変更なしで動作 | PASS |
| BL-18 | Recursion DoS 耐性 | 深くネストしたJSON配列を含む生リクエスト | `RecursionError` を送出せず `status=error` で処理継続 | PASS |
| BL-19 | 件数上限（delta_findings/candidate_cases）と重大所見の生存保証 | `_MAX_FINDINGS` 超過＋末尾に critical 追加 | 上限超過分は切り詰めるが critical は必ず生存、`_truncated`/`_total_count` を開示 | PASS |
| BL-20 | /invoke→/feedback 予測照合ループ（advisory 永続化） | 非縮退 /invoke 成功 → `record_advisory` でサニタイズ済み advisory を永続化 → /feedback(action=repair/observe) | invoke 後に `get_recent_advisory` が S-3 通過後の advisory を返し、feedback の reconciliation が hit / false_positive_candidate を返す（縮退 invoke は記録しない・記録失敗は advisory 提示を落とさない） | PASS |

## Test Execution Summary
- Execution date: 2026-07-13（ローカル venv・`agenticstar-agentcore==1.0.0`）
- Total tests: 125
- Pass: 125 / Fail: 0 / Skip: 0
- Coverage: TC-01〜TC-11（11/11）・PB-1〜PB-6（6/6・PB-7対象外）・BL-01〜BL-20（20項目）全PASS

---

## 実 LLM 1 回転検証（Live LLM Check）

`config/agent.yaml` は `generation_mode: "llm"` を宣言しているが、これまでのテストは
すべて注入シームに double を刺しており、**実 LLM でプロンプトが出力契約を導けるか**は
未検証だった。本節はその 1 回転検証の記録である。

| 項目 | 内容 |
|---|---|
| 実施日 | 2026-08-20 |
| 使用 LLM | Azure OpenAI のモデルを `BaseLLM` 実装として注入 |
| 注入シーム | `config["llm"]`（`graph.py` → `AssetWorkflowGraphNode` → 内側 graph → `AdvisorySynthesisNode`） |
| 入力 | invoke モード。`tests/conftest.py` の `agent_config` / `case_payload` / `live_input_context`（統合テストと同じ asset1 / expansion_joint_gap_mm 65.0 の critical ケース） |
| LLM 呼び出し回数 | 1 回（invoke 経路で LLM を呼ぶのは Stage3 のみ） |
| 結果 | ✅ `status: success` / 縮退なし / **provenance ゲートによる redaction 0 件** |
| 非空だった出力キー | `priority` / `priority_label_ja` / `narrative` / `cited_finding_ids` / `cited_intervention_ids` / `finding_index` / `finding_index_total_count` / `delta_findings_total_count` / `candidate_cases_total_count` |

### 検出した欠陥 4 件（すべて修正済み）

#### D1 — 本番では LLM が絶対に配線されない（core path）

`src/api/server.py` の `wire_services()` が `shared.services.llm.anthropic_client` から
**存在しない名前 `AnthropicLLMClient`** を import し（実クラスは `AnthropicClient`）、
さらに**引数なし**で構築していた（`AnthropicClient.__init__` は `config["api_key"]` 必須）。
両方の失敗を `except Exception: llm = None` が握り潰していたため、`generation_mode: "llm"` を
宣言しながら**本番は毎リクエスト決定論経路**で動作する状態だった。
`wire_services()` は CI でも他のテストでも一度も実行されないため、誰も踏めなかった。

**修正** — 正しいシンボル名 + `secrets_factory(...).require("ANTHROPIC_API_KEY")` で構築。
縮退してよいのは運用ガイドが「サービスを止めない」と定めた 2 条件だけに限定した:
(a) optional extra `[anthropic]` 不在（`ModuleNotFoundError` かつ `exc.name == "langchain_anthropic"`）、
(b) API キー未設定（`MissingSecret`）。
公開シンボルの欠落・コンストラクタ不一致・他モジュールの import 破損は**配線バグとして伝播**させる。


##### 注入シームについての所見

同系テンプレートの一般的な形は「**`server.py` は具体 LLM クライアントを import も生成もせず、
`config["llm"]` は platform / STG 側が注入する**」であり、本 repo はそれと異なり
`server.py` 側で構築する少数派にあたる。そのうえで**クラス名が実在しなかった**ため、
この経路は一度も成立していなかった。本改版はクラス名（`AnthropicLLMClient` → 実在する
`AnthropicClient`）と握り潰しを直したもので、注入シームそのものを一般形へ寄せる設計変更は
行っていない。どちらへ揃えるかは設計判断のため、所見として記録する。

#### D2 — `BaseLLM` 契約違反

ノードが `self._llm.complete(prompt_str)` を呼び、戻り値を `str` として使っていた。
実契約は `complete(messages: list) -> dict`（canonical dict）。本番が注入する `AnthropicClient` は
引数を `ChatAnthropic.invoke()` へ素通しして **dict を返す**ため、advisory の `narrative` に
dict が入るはずだった。

**修正** — `src/services/llm_text.py::complete_text()` に一本化（呼び出し 3 箇所）。
canonical dict でない・`content` キーが無い・`content` が非 str・`content` が空、のいずれも
例外にして各呼び出し元の既存縮退経路へ流す。`str()` 強制は行わない（`{'secret': 'x'}` のような
オブジェクトが読み手向けの文章として出てしまうため）。

#### D3 — conftest がハーネスを欠陥側に合わせていた

`tests/conftest.py` が live-check ハーネスのクライアントの `complete` を monkeypatch して
「文字列を受けて文字列を返す」ようにしていた。**ハーネスを AG の欠陥に適合させていた**ため、
実 LLM 1 回転でも D2 を検出できない状態だった。

**修正** — そのパッチを削除。ハーネスのクライアントを無改変で使う。

#### D4 — プロンプトとゲートの粒度不一致で出力の大半が潰れる

`post_process_node._strip_unsupported_claims` は **文単位**（`。！？\n` で分割）で証拠 ID を要求するが、
プロンプトは "Every claim MUST cite an evidence ID" としか言っていなかった。実 LLM は段落末に
1 個だけ引用し、しかも `。` の**後ろ**に置いたため、ID は次の断片に属し、支えるはずだった文が
`[要検証：根拠となる経年差分/過去事例IDが確認できません]` に置換された（初回実行で 3 文中 3 文が消失）。
それでもハーネスはエンベロープのキーが非空なので ✅ を返していた。

**修正** — **ゲートは一切変更していない**。プロンプト側に、各文が ID を持つこと、および
`〜possibility があります[ID]。` のように**閉じ `。` の直前**へ置くこと（`〜あります。[ID]` は不可）を明記。
再実行で redaction 0 件。

#### 併せて修正 — 証拠ゼロで充足不能なプロンプトを送っていた

`delta_findings` も `candidate_cases` も空のとき、`valid_ids` が空のまま
「各文を `[]` の中の ID で引用せよ」というプロンプトを送っていた。モデルは ID を捏造するか
引用不能な文を書くかで、いずれもゲートが全文を redact する。
証拠ゼロのときは LLM を呼ばず決定論分岐へ回すようにした。ただし **`llm_unavailable` とは報告しない**
（プロバイダは正常であり、そう報告すると同一資産で起きた本物の障害を隠すため）。

### 追加・変更したテストと変異検査

| 変異（本番コードを戻す） | 落ちたテスト |
|---|---|
| `complete_text` を `complete(prompt_str)` 直呼びに戻す | 🔴 10 件 |
| `content` の型検査を外し `str()` 強制に戻す | 🔴 `test_non_string_content_is_rejected_not_stringified` |
| 空 `content` の拒否を外す | 🔴 `test_empty_content_is_rejected` |
| `content` キー欠落の拒否を外す | 🔴 `test_missing_content_key_is_rejected` |
| 存在しないシンボル名 `AnthropicLLMClient` に戻す | 🔴 2 件（`test_server_wiring.py`） |
| 引数なしコンストラクタに戻す | 🔴 2 件（同上） |
| `ModuleNotFoundError` の `name` 判定を外す | 🔴 `test_unrelated_missing_module_is_a_wiring_bug_and_propagates` |
| `MissingSecret` 縮退を外す（= import 時クラッシュ） | 🔴 `test_missing_api_key_degrades_to_none_without_crashing` |
| プロンプトの per-sentence 指示を削除 | 🔴 2 件（`TestPromptGateAlignment` 他） |
| プロンプトの配置（`。`の直前）指示を削除 | 🔴 2 件（同上） |
| 証拠ゼロでも LLM を呼ぶ挙動に戻す | 🔴 `test_no_valid_evidence_skips_the_llm_without_claiming_it_is_down` |

`tests/integration/test_graph.py` の `FakeLLM` は**プロンプト従順**にした（配置規則・per-sentence 規則が
プロンプトに書かれていなければ、実 LLM が実際にそうしたように段落末／`。` の後ろに引用を置く）。
かつ**複数文**を生成する。1 文しか書かない double では「段落末に 1 個だけ引用する」欠陥を再現できず、
プロンプトから指示を消してもテストが緑のままになるため。

### 未解決（本改版の退行ではない・既存の設計課題）

1. **ingest / feedback 経路の LLM 出力が S-3 を素通りして永続化され得る。**
   framework の S-3 出力ゲートは**トップレベルの文字列しか走査しない**。`StructureWithLlmNode` は
   LLM テキストを `normalized_records[*].findings.extracted_text` という入れ子に置き、
   `UpdateInterventionNode` は S-3 実行前に DB 副作用を確定する。D1/D2 を直して実 LLM が
   本当に動くようになったことで、この経路は現実に到達可能になった。
   恒久対策（永続化前の再帰検査／LLM 構造化と DB 更新のノード分離）は設計判断を伴うため未実施。
2. **provenance ゲートは「同じ断片に有効 ID が 1 つあるか」しか見ない。**
   その ID がその主張を実際に支えるかは検証しない。また文分割が `。！？\n` のみのため、
   ASCII 句点だけの文（`First claim. Second claim [fnd-1].`）は 1 断片として通る。
3. **`_build_prompt` が findings / candidate_cases の dict を `repr` のままプロンプトへ入れる。**
   `metric` / `action` 等のクライアント由来文字列を経由した指示注入の余地がある。
4. **`structure_with_llm` の `structured_count` が失敗件数を区別しない**（監視上は成功に見える）。
