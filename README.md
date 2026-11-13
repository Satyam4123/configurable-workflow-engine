# Configurable Workflow Decision Platform

A configurable, declarative workflow decision platform in Python (FastAPI) capable of executing multi-stage business workflows, eligibility checks, and threshold evaluations under changing requirements.

---

## Architecture & Engineering Robustness

1. **Mock External Services & Retries**:
   - `core/external_service.py` simulates credit bureau verification and AML/fraud blacklist lookups.
   - Handles transient network failures (timeouts, HTTP 500/503) using exponential backoff retries (via `tenacity` with automatic fallback). Retries up to 3 times before failing gracefully.

2. **Idempotency Handling**:
   - Every incoming evaluation request is matched against `application_id`.
   - Applications in terminal states (`APPROVED` or `REJECTED`) immediately return their cached decision and full audit trail (`is_idempotent_replay: true`) without re-evaluating rules.
   - Concurrent active requests (`PROCESSING`) are handled gracefully with `409 Conflict`.

3. **Persistence & Granular Audit Logging**:
   - SQLAlchemy 2.0 ORM models (`WorkflowExecution` and `StageAuditLog`) persist all pipeline transitions to SQLite.
   - Every stage records executed mandatory checks, evaluated rule conditions (field, operator, threshold, actual value, error message), and final execution state.
   - Full audit trails can be retrieved anytime via `GET /workflow/execution/{application_id}`.

---

## Project Structure

```text
configurable-workflow-engine/
├── config/
│   └── workflow_sample.json     # Sample multi-stage declarative workflow definition
├── core/
│   ├── __init__.py
│   ├── engine.py                # Pipeline execution engine with idempotency & audit persistence
│   ├── evaluator.py             # Rule condition and mandatory check evaluator
│   └── external_service.py      # Credit bureau & blacklist client with exponential retries
├── models/
│   ├── __init__.py
│   └── schemas.py               # Pydantic v2 schemas for intake, config, audit, and responses
├── storage/
│   ├── __init__.py
│   └── database.py              # SQLAlchemy 2.0 models (WorkflowExecution, StageAuditLog) & helpers
├── tests/
│   ├── __init__.py
│   ├── test_health.py           # Health check endpoint unit tests
│   ├── test_robustness.py       # Retries, idempotency, and audit logging test suite
│   └── test_workflow.py         # Workflow execution, validation, and stage rule tests
├── .gitignore
├── main.py                      # FastAPI application entrypoint with REST endpoints
├── README.md
└── requirements.txt             # Pinned project dependencies
```

---

## Prerequisites

Before setting up the project, verify that your environment meets the following runtime requirements:

- **Python 3.11+** (Specifically tested and verified on **Python 3.11.9**).
- **Pre-Built Wheels Note**: Using Python 3.11+ ensures that official, pre-compiled binary wheels for packages with native extensions (such as `pydantic-core` and `greenlet`) are downloaded directly from PyPI. This eliminates any need for Microsoft Visual C++ Build Tools (MSVC) or a local Rust compiler on Windows/macOS/Linux.
- A virtual environment tool (`venv`, `virtualenv`, or `uv`).

Verify your Python installation:
```bash
python --version
# Expected: Python 3.11.x (e.g., Python 3.11.9)
```

---

## Environment Setup & Installation

### 1. Create and Activate Virtual Environment

```bash
# macOS / Linux
python3.11 -m venv .venv
source .venv/bin/activate

# Windows (PowerShell)
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1

# Windows (Command Prompt)
.venv\Scripts\activate.bat
```

### 2. Install Pinned Dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

*(Optional alternative with `uv` for sub-second installs)*:
```bash
uv pip install -r requirements.txt
```

---

## Running the Server

Start the application with Uvicorn:

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Once started:
- **Base URL**: `http://localhost:8000`
- **Interactive Swagger UI**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **ReDoc Documentation**: [http://localhost:8000/redoc](http://localhost:8000/redoc)
- **OpenAPI Schema**: [http://localhost:8000/openapi.json](http://localhost:8000/openapi.json)

---

## Running the Test Suite

Execute the full suite of unit, integration, and robustness tests with Pytest:

```bash
pytest -v
```

### Test Suite Execution Output (16 Passed)

```text
============================= test session starts =============================
platform win32 -- Python 3.11.9, pytest-8.1.1, pluggy-1.7.0 -- .venv\Scripts\python.exe
cachedir: .pytest_cache
rootdir: D:\Clg\7th sem\scoreme\configurable-workflow-engine
plugins: anyio-4.16.0
collecting ... collected 16 items

tests/test_health.py::test_health_check_returns_200 PASSED               [  6%]
tests/test_robustness.py::test_external_service_successful_first_try PASSED [ 12%]
tests/test_robustness.py::test_external_service_retries_transient_failure_then_succeeds PASSED [ 18%]
tests/test_robustness.py::test_external_service_exhausts_retries_on_persistent_failure PASSED [ 25%]
tests/test_robustness.py::test_external_service_timeout_failure PASSED   [ 31%]
tests/test_robustness.py::test_external_service_screening_blacklist_detection PASSED [ 37%]
tests/test_robustness.py::test_idempotent_execution_returns_cached_decision_on_duplicate PASSED [ 43%]
tests/test_robustness.py::test_duplicate_active_request_returns_conflict_409 PASSED [ 50%]
tests/test_robustness.py::test_database_persistence_and_audit_trail_verification PASSED [ 56%]
tests/test_robustness.py::test_get_execution_audit_endpoint_success_and_404 PASSED [ 62%]
tests/test_workflow.py::test_load_workflow_sample_config PASSED          [ 68%]
tests/test_workflow.py::test_condition_evaluator_comparisons PASSED      [ 75%]
tests/test_workflow.py::test_workflow_execution_approved PASSED          [ 81%]
tests/test_workflow.py::test_workflow_execution_rejected_on_credit_score PASSED [ 87%]
tests/test_workflow.py::test_workflow_execution_rejected_on_missing_mandatory_check PASSED [ 93%]
tests/test_workflow.py::test_intake_request_validation_failure PASSED    [100%]

======================== 16 passed, 1 warning in 2.56s ========================
```

---

## API Endpoints Overview

| Method | Path | Summary | Description |
|---|---|---|---|
| `GET` | `/health` | Service Health Check | Returns service operational heartbeat and UTC timestamp. |
| `POST` | `/workflow/execute` | Execute Workflow Pipeline | Evaluates incoming application intake against declarative workflow stages with automatic idempotency protection and database audit persistence. |
| `GET` | `/workflow/execution/{application_id}` | Retrieve Execution Audit Log | Fetches historical decision records, stage breakdown, and rule evaluation audit logs for an application ID. |
| `POST` | `/workflow/external-screening` | Simulate External Services | Directly triggers external credit bureau and blacklist verification with automated exponential retries. |
| `GET` | `/workflow/sample-config` | Retrieve Active Workflow Spec | Returns the parsed sample configuration JSON currently powering the decision engine. |

### Sample Workflow Execution Request (`POST /workflow/execute`)

```json
{
  "application_id": "APP-2026-90412",
  "applicant_name": "Jane Doe",
  "credit_score": 720,
  "income": 85000.0,
  "amount_requested": 250000.0,
  "metadata": {
    "kyc_verified": true,
    "citizenship_verified": true,
    "blacklist_check_cleared": true,
    "employment_status_active": true,
    "compliance_signoff": true
  }
}
```

### Sample Workflow Execution Response

```json
{
  "workflow_id": "loan_approval_workflow",
  "workflow_name": "Retail Loan Decision Pipeline",
  "application_id": "APP-2026-90412",
  "status": "APPROVED",
  "decision_summary": "Application satisfies all mandatory checks and threshold conditions across all stages.",
  "executed_stages": [
    {
      "stage_id": "stage_1_pre_qualification",
      "stage_name": "Pre-Qualification & KYC",
      "order": 1,
      "passed": true,
      "failed_checks": [],
      "failed_conditions": [],
      "details": {
        "mandatory_checks_evaluated": ["kyc_verified", "citizenship_verified", "blacklist_check_cleared"],
        "rules_evaluated": [
          {
            "field": "credit_score",
            "operator": ">=",
            "threshold_value": 650,
            "actual_value": 720,
            "passed": true,
            "error_message": null
          },
          {
            "field": "income",
            "operator": ">=",
            "threshold_value": 50000.0,
            "actual_value": 85000.0,
            "passed": true,
            "error_message": null
          }
        ]
      }
    }
  ],
  "evaluated_at": "2026-11-13T10:43:35.123456",
  "is_idempotent_replay": false,
  "execution_id": 1
}
```
