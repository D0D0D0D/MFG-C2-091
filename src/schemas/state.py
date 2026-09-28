"""AgentCore Platform v1.0"""

# ADR-005: State must be a flat TypedDict — never Pydantic BaseModel.
# LangGraph checkpoints use msgpack serialization; Pydantic objects
# cause silent corruption. Extend AgentState with agent-specific
# fields only. Do NOT add credentials, secrets, or Pydantic models.
#
# Field contract source of truth: docs/02_design.md §State Definition.
# See docs/02_design.md §🔴 status の有効値 for the allowed `status` values
# ("success" / "retry" / "error" only — "degraded" is NOT an AgentStatus value;
# graceful degradation is expressed via status="success" + degradation_reason).

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Agent state for MFG-C2-091 AssetInspectionHistoryAdvisoryGraph.

    mode: "ingest" | "invoke" | "feedback" — selects which internal
    workflow domain_workflow_graph.py executes.
    """

    # ── Common to all modes ──────────────────────────────────────────────
    mode: str
    org_id: str
    asset_type: str
    # /invoke /feedback の対象資産識別子。/ingest 時は不要。
    asset_id: str
    # mode 依存のリクエストボディ（例: /invoke の current_inspection、
    # /ingest の records、/feedback の intervention_update）。
    payload: dict

    # ── pre_process が解決するフィールド ──────────────────────────────────
    org_context: dict
    # 選択された設備プロファイル（アラーム閾値・劣化モード知識・処置種別辞書）。
    # 未知 asset_type の場合は空 dict（プロファイル依存判定をスキップして
    # 縮退する起点 — docs/02_design.md §(a) 未知 asset_type の挙動）。
    asset_profile: dict
    # /invoke 時にロードした対象資産の過去 inspections/interventions 履歴。
    asset_history: dict

    # ── /invoke: 3段パイプライン出力 ──────────────────────────────────────
    # Stage 1 が生成した経年差分シグナルリスト（各要素に finding_id を含む）。
    # _MAX_FINDINGS でキャップ済み（severity 優先順・重大所見は必ず生存）。
    delta_findings: list
    # delta_findings がキャップされる前の真の件数（開示契約 — キャップ自体は
    # 隠さない。信号1件も欠落させない # の逆で、キャップした事実を隠さない）。
    delta_findings_total_count: int
    # 存在する場合のみ True（delta_findings_total_count > len(delta_findings)）。
    delta_findings_truncated: bool
    # Stage 2 が生成した候補類似事例＋根拠 ID リスト
    # （matched_intervention_id / matched_finding_id）。同様にキャップ済み。
    candidate_cases: list
    candidate_cases_total_count: int
    candidate_cases_truncated: bool
    # Stage 3 が生成した非断定経年所見＋優先度（finding_id/intervention_id 引用込み）。
    advisory_output: dict

    # ── /ingest 中間パイプラインフィールド ────────────────────────────────
    # normalize_input -> structure_with_llm -> validate_records -> write_records
    # 間で流通する。LangGraph の state_schema 射影で undeclared field は
    # 黙って捨てられるため、ここで宣言しておく必要がある
    # （docs/02_design.md §🔴 inner graph の state 型注釈方針と同種の落とし穴）。
    normalized_records: list
    records_needing_llm_structuring: list
    valid_records: list
    validation_errors: list

    # ── /ingest 出力 ──────────────────────────────────────────────────────
    ingest_summary: dict

    # ── /feedback 中間パイプラインフィールド ──────────────────────────────
    resolved_intervention_id: str
    updated_intervention_fields: dict

    # ── /feedback 出力 ────────────────────────────────────────────────────
    feedback_result: dict

    # ── post_process 出力 ─────────────────────────────────────────────────
    formatted_output: dict

    # ── 縮退動作マーカー（AgentStatus の一部ではない）────────────────────
    # 縮退動作（グレースフルデグレード）が発生した際に status="success" と
    # 併せて設定する。正常時は未設定。
    # 例: "llm_unavailable" / "insufficient_inspection_history" /
    #     "asset_profile_not_found"
    degradation_reason: str
