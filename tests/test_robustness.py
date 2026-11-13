"""Tests for Engineering Robustness layer: Retries, Idempotency, and Audit Logging."""

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from core.external_service import (
    ExternalServiceTimeoutError,
    ExternalServiceTransientError,
    MockCreditBureauClient,
)
from main import app
from models.schemas import DecisionStatus
from storage.database import (
    Base,
    StageAuditLog,
    WorkflowExecution,
    get_db,
)

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_test_db():
    """Sets up an isolated in-memory SQLite database using StaticPool for each test."""
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
    yield TestingSessionLocal
    app.dependency_overrides.clear()


# ===========================================================================
# 1. External Service & Retries Tests
# ===========================================================================

def test_external_service_successful_first_try() -> None:
    """Verifies that the mock client succeeds immediately without retries when healthy."""
    service = MockCreditBureauClient()
    result = service.fetch_credit_score(application_id="APP-EXT-01", applicant_name="John Doe")
    assert result["application_id"] == "APP-EXT-01"
    assert result["bureau_status"] == "VERIFIED"
    assert service.total_attempts == 1
    assert service.successful_calls == 1


def test_external_service_retries_transient_failure_then_succeeds() -> None:
    """Verifies that transient errors (e.g. 503) trigger exponential backoff retries and recover."""
    service = MockCreditBureauClient(transient_failures_before_success=2)
    result = service.check_blacklist(application_id="APP-EXT-02", applicant_name="John Doe")

    assert result["blacklist_check_cleared"] is True
    assert service.total_attempts == 3
    assert service.successful_calls == 1


def test_external_service_exhausts_retries_on_persistent_failure() -> None:
    """Verifies that persistent transient failures exhaust 3 retry attempts and raise exception."""
    service = MockCreditBureauClient(transient_failures_before_success=5)
    with pytest.raises(ExternalServiceTransientError):
        service.check_blacklist(application_id="APP-EXT-03", applicant_name="John Doe")

    assert service.total_attempts == 3
    assert service.successful_calls == 0


def test_external_service_timeout_failure() -> None:
    """Verifies that simulated timeout triggers transient error retry exhaustion."""
    service = MockCreditBureauClient(force_timeout=True)
    with pytest.raises(ExternalServiceTimeoutError):
        service.fetch_credit_score(application_id="APP-EXT-04", applicant_name="John Doe")

    assert service.total_attempts == 3


def test_external_service_screening_blacklist_detection() -> None:
    """Verifies that blacklist simulation flags high-risk or blocked applicant names."""
    service = MockCreditBureauClient()
    result = service.check_blacklist(application_id="APP-EXT-05", applicant_name="Fraudster Blocked Corp")
    assert result["blacklist_check_cleared"] is False
    assert result["match_found"] is True


# ===========================================================================
# 2. Idempotency Handling Tests
# ===========================================================================

def test_idempotent_execution_returns_cached_decision_on_duplicate(setup_test_db) -> None:
    """Submitting the exact same application_id returns cached decision without re-running stages."""
    payload = {
        "application_id": "APP-IDEM-001",
        "applicant_name": "Diana Prince",
        "credit_score": 780,
        "income": 120000.0,
        "amount_requested": 100000.0,
        "metadata": {
            "kyc_verified": True,
            "citizenship_verified": True,
            "blacklist_check_cleared": True,
            "employment_status_active": True,
            "compliance_signoff": True
        }
    }

    # First submission: full execution
    res1 = client.post("/workflow/execute", json=payload)
    assert res1.status_code == 200
    data1 = res1.json()
    assert data1["status"] == DecisionStatus.APPROVED.value
    assert data1["is_idempotent_replay"] is False

    # Second submission: idempotent replay from database
    res2 = client.post("/workflow/execute", json=payload)
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["status"] == data1["status"]
    assert data2["is_idempotent_replay"] is True
    assert data2["execution_id"] == data1["execution_id"]
    assert len(data2["executed_stages"]) == len(data1["executed_stages"])

    # Verify only 1 database execution record was created
    session_factory = setup_test_db
    with session_factory() as db:
        records = db.scalars(
            select(WorkflowExecution).where(WorkflowExecution.application_id == "APP-IDEM-001")
        ).all()
        assert len(records) == 1


def test_duplicate_active_request_returns_conflict_409(setup_test_db) -> None:
    """An active (non-terminal PROCESSING) application rejects concurrent duplicate requests with 409."""
    session_factory = setup_test_db
    with session_factory() as db:
        active_rec = WorkflowExecution(
            application_id="APP-ACTIVE-999",
            workflow_id="loan_approval_workflow",
            workflow_name="Retail Loan Pipeline",
            applicant_name="Clark Kent",
            credit_score=720,
            income=80000.0,
            amount_requested=25000.0,
            status=DecisionStatus.PROCESSING.value,
            is_terminal=False,
        )
        db.add(active_rec)
        db.commit()

    payload = {
        "application_id": "APP-ACTIVE-999",
        "applicant_name": "Clark Kent",
        "credit_score": 720,
        "income": 80000.0,
        "amount_requested": 25000.0,
        "metadata": {}
    }

    response = client.post("/workflow/execute", json=payload)
    assert response.status_code == 409
    assert "currently processing" in response.json()["detail"].lower()


# ===========================================================================
# 3. Persistence & Audit Logging Tests
# ===========================================================================

def test_database_persistence_and_audit_trail_verification(setup_test_db) -> None:
    """Verifies that all evaluated stages, condition rules, and decisions are stored in database."""
    payload = {
        "application_id": "APP-AUDIT-101",
        "applicant_name": "Bruce Wayne",
        "credit_score": 820,
        "income": 500000.0,
        "amount_requested": 250000.0,
        "metadata": {
            "kyc_verified": True,
            "citizenship_verified": True,
            "blacklist_check_cleared": True,
            "employment_status_active": True,
            "compliance_signoff": True
        }
    }

    res = client.post("/workflow/execute", json=payload)
    assert res.status_code == 200

    session_factory = setup_test_db
    with session_factory() as db:
        exec_record = db.scalars(
            select(WorkflowExecution).where(WorkflowExecution.application_id == "APP-AUDIT-101")
        ).first()

        assert exec_record is not None
        assert exec_record.status == DecisionStatus.APPROVED.value
        assert exec_record.is_terminal is True
        assert exec_record.applicant_name == "Bruce Wayne"
        assert exec_record.credit_score == 820

        # Verify stage audit logs in database
        stages = exec_record.stages
        assert len(stages) == 3
        for stage_log in stages:
            assert stage_log.passed is True
            assert "rules_evaluated" in stage_log.details
            assert len(stage_log.details["rules_evaluated"]) > 0


def test_get_execution_audit_endpoint_success_and_404() -> None:
    """Tests GET /workflow/execution/{application_id} endpoint."""
    payload = {
        "application_id": "APP-AUDIT-202",
        "applicant_name": "Barry Allen",
        "credit_score": 670,
        "income": 60000.0,
        "amount_requested": 20000.0,
        "metadata": {
            "kyc_verified": True,
            "citizenship_verified": True,
            "blacklist_check_cleared": True,
            "employment_status_active": True,
            "compliance_signoff": True
        }
    }
    client.post("/workflow/execute", json=payload)

    # Fetch existing audit log
    get_res = client.get("/workflow/execution/APP-AUDIT-202")
    assert get_res.status_code == 200
    data = get_res.json()
    assert data["application_id"] == "APP-AUDIT-202"
    assert len(data["executed_stages"]) == 3

    # Query non-existent ID
    res_404 = client.get("/workflow/execution/NON-EXISTENT-ID")
    assert res_404.status_code == 404
