"""Unit tests verifying runtime rule change scenarios, multi-domain workflows, and manual review routing."""

import json
from pathlib import Path
from core.engine import WorkflowEngine
from models.schemas import (
    ApplicationIntakeRequest,
    DecisionStatus,
    WorkflowConfig,
)

BASE_DIR = Path(__file__).resolve().parent.parent


def test_rule_change_scenario_flips_decision_without_code_changes() -> None:
    """Verifies that changing a threshold in workflow configuration alters the decision

    without requiring any modifications to engine or backend code.
    """
    engine = WorkflowEngine()

    # Load baseline loan approval workflow (Stage 1 requires credit_score >= 650)
    config_path = BASE_DIR / "config" / "workflow_sample.json"
    with open(config_path, "r", encoding="utf-8") as f:
        config_data = json.load(f)

    baseline_config = WorkflowConfig.model_validate(config_data)

    # Borderline applicant with credit_score = 620
    applicant = ApplicationIntakeRequest(
        application_id="APP-RULE-CHANGE-01",
        applicant_name="Grace Hopper",
        credit_score=620,  # Below baseline 650
        income=75000.0,
        amount_requested=25000.0,
        metadata={
            "kyc_verified": True,
            "citizenship_verified": True,
            "blacklist_check_cleared": True,
            "employment_status_active": True,
            "compliance_signoff": True,
        }
    )

    # 1. Baseline evaluation -> REJECTED due to credit_score < 650
    baseline_result = engine.execute(request=applicant, config=baseline_config)
    assert baseline_result.status == DecisionStatus.REJECTED
    assert baseline_result.executed_stages[0].passed is False

    # 2. Rule change scenario: Underwriting policy relaxes credit_score threshold to 600
    relaxed_config_data = json.loads(json.dumps(config_data))
    for stage in relaxed_config_data["stages"]:
        for condition in stage["conditions"]:
            if condition["field"] == "credit_score":
                condition["value"] = 600

    updated_config = WorkflowConfig.model_validate(relaxed_config_data)

    # 3. Re-evaluation under updated rule -> transitions to APPROVED
    updated_result = engine.execute(request=applicant, config=updated_config)
    assert updated_result.status == DecisionStatus.APPROVED
    assert all(stage.passed for stage in updated_result.executed_stages)


def test_manual_review_stage_routing_on_borderline_claim() -> None:
    """Verifies that a borderline condition with on_failure_status='IN_REVIEW'

    routes the application to DecisionStatus.IN_REVIEW instead of hard rejection.
    """
    engine = WorkflowEngine()

    # Load the insurance claim workflow
    claim_config_path = BASE_DIR / "config" / "claim_workflow.json"
    with open(claim_config_path, "r", encoding="utf-8") as f:
        claim_config_data = json.load(f)

    claim_config = WorkflowConfig.model_validate(claim_config_data)

    # Claim for $35,000 (Passes coverage & fraud check, but exceeds $25,000 straight-through payout limit)
    claim_request = ApplicationIntakeRequest(
        application_id="CLAIM-REVIEW-001",
        applicant_name="Arthur Dent",
        credit_score=650,
        income=60000.0,
        amount_requested=35000.0,
        metadata={
            "policy_active": True,
            "premium_paid_up_to_date": True,
            "incident_report_attached": True,
        }
    )

    result = engine.execute(request=claim_request, config=claim_config)

    # Must route to IN_REVIEW rather than REJECTED or APPROVED
    assert result.status == DecisionStatus.IN_REVIEW
    assert "manual underwriter review" in result.decision_summary.lower()
    assert result.executed_stages[0].passed is True  # Coverage stage passed
    assert result.executed_stages[1].passed is True  # Fraud stage passed
    assert result.executed_stages[2].passed is False # Adjudication stage routed to review


def test_multi_domain_claim_workflow_approved_flow() -> None:
    """Verifies that a standard $5,000 claim passes straight-through automated adjudication."""
    engine = WorkflowEngine()

    claim_config_path = BASE_DIR / "config" / "claim_workflow.json"
    with open(claim_config_path, "r", encoding="utf-8") as f:
        claim_config_data = json.load(f)

    claim_config = WorkflowConfig.model_validate(claim_config_data)

    # Straight-through claim: $5,000
    claim_request = ApplicationIntakeRequest(
        application_id="CLAIM-APPROVED-002",
        applicant_name="Ford Prefect",
        credit_score=710,
        income=80000.0,
        amount_requested=5000.0,
        metadata={
            "policy_active": True,
            "premium_paid_up_to_date": True,
            "incident_report_attached": True,
        }
    )

    result = engine.execute(request=claim_request, config=claim_config)
    assert result.status == DecisionStatus.APPROVED
    assert len(result.executed_stages) == 3
    assert all(stage.passed for stage in result.executed_stages)

