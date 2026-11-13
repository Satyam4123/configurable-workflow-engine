"""Unit and integration tests for workflow parsing, condition evaluation, and execution."""

import json
from pathlib import Path
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.engine import WorkflowEngine
from core.evaluator import ConditionEvaluator
from main import app, load_default_workflow_config
from models.schemas import (
    ApplicationIntakeRequest,
    ComparisonOperator,
    DecisionStatus,
    RuleCondition,
    WorkflowConfig,
)
from storage.database import Base, get_db

# Test client
client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_test_db():
    """Sets up an isolated in-memory SQLite database using StaticPool for every test."""
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    Base.metadata.create_all(bind=test_engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    yield
    app.dependency_overrides.clear()


def test_load_workflow_sample_config() -> None:
    """Verifies that the default workflow JSON configuration parses into WorkflowConfig."""
    config = load_default_workflow_config()
    assert config.workflow_id == "loan_approval_workflow"
    assert len(config.stages) == 3
    assert config.stages[0].stage_id == "stage_1_pre_qualification"
    assert "kyc_verified" in config.stages[0].mandatory_checks


def test_condition_evaluator_comparisons() -> None:
    """Tests evaluator operator logic for numerical thresholds."""
    cond = RuleCondition(
        field="credit_score",
        operator=ComparisonOperator.GREATER_THAN_OR_EQUAL,
        value=650,
        error_message="Credit score must be at least 650"
    )
    passed, _ = ConditionEvaluator.evaluate_condition(cond, {"credit_score": 700})
    assert passed is True

    passed, err = ConditionEvaluator.evaluate_condition(cond, {"credit_score": 600})
    assert passed is False
    assert "Credit score" in err or "credit_score" in err


def test_workflow_execution_approved() -> None:
    """Tests that an intake payload satisfying all conditions receives APPROVED status."""
    payload = {
        "application_id": "APP-TEST-001",
        "applicant_name": "Alice Johnson",
        "credit_score": 750,
        "income": 95000.0,
        "amount_requested": 150000.0,
        "metadata": {
            "kyc_verified": True,
            "citizenship_verified": True,
            "blacklist_check_cleared": True,
            "employment_status_active": True,
            "compliance_signoff": True
        }
    }
    response = client.post("/workflow/execute", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["application_id"] == "APP-TEST-001"
    assert data["status"] == DecisionStatus.APPROVED.value
    assert len(data["executed_stages"]) == 3
    assert all(stage["passed"] for stage in data["executed_stages"])


def test_workflow_execution_rejected_on_credit_score() -> None:
    """Tests that an intake payload with substandard credit score receives REJECTED status."""
    payload = {
        "application_id": "APP-TEST-002",
        "applicant_name": "Bob Smith",
        "credit_score": 580,  # Below 650 threshold
        "income": 80000.0,
        "amount_requested": 50000.0,
        "metadata": {
            "kyc_verified": True,
            "citizenship_verified": True,
            "blacklist_check_cleared": True,
            "employment_status_active": True,
            "compliance_signoff": True
        }
    }
    response = client.post("/workflow/execute", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == DecisionStatus.REJECTED.value
    stage_1 = data["executed_stages"][0]
    assert stage_1["passed"] is False
    assert len(stage_1["failed_conditions"]) > 0


def test_workflow_execution_rejected_on_missing_mandatory_check() -> None:
    """Tests that a missing or false mandatory verification check rejects the stage."""
    payload = {
        "application_id": "APP-TEST-003",
        "applicant_name": "Charlie Brown",
        "credit_score": 720,
        "income": 90000.0,
        "amount_requested": 30000.0,
        "metadata": {
            "kyc_verified": False,  # Failed check
            "citizenship_verified": True,
            "blacklist_check_cleared": True,
            "employment_status_active": True,
            "compliance_signoff": True
        }
    }
    response = client.post("/workflow/execute", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == DecisionStatus.REJECTED.value
    stage_1 = data["executed_stages"][0]
    assert stage_1["passed"] is False
    assert "kyc_verified" in stage_1["failed_checks"]


def test_intake_request_validation_failure() -> None:
    """Tests that malformed or out-of-range payloads return 422 Unprocessable Entity."""
    payload = {
        "application_id": "APP-TEST-004",
        "applicant_name": "D",  # Too short (min_length=2)
        "credit_score": 950,    # Above max 850
        "income": -100.0,       # Below 0
        "amount_requested": 0.0 # Must be > 0
    }
    response = client.post("/workflow/execute", json=payload)
    assert response.status_code == 422
