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

## Getting Started

### 1. Environment Setup
```bash
# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Run the Development Server
```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```
Interactive API documentation will be available at:
- **Swagger UI**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **ReDoc**: [http://localhost:8000/redoc](http://localhost:8000/redoc)

### 3. Run the Test Suite
```bash
pytest -v
```

---

## API Overview

- `GET /health`: Service health check and heartbeat status.
- `GET /workflow/sample-config`: Inspect the currently loaded workflow configuration.
- `POST /workflow/execute`: Evaluate an incoming application intake request with idempotency protection and audit persistence.
- `GET /workflow/execution/{application_id}`: Query historical execution and granular stage audit logs.
- `POST /workflow/external-screening`: Directly test external bureau and blacklist screening with simulated retries.
