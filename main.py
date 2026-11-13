"""Main entrypoint for the Configurable Workflow Decision Platform.

Initializes FastAPI application, loads declarative workflow configurations,
and exposes RESTful APIs for health checks, idempotent decisioning, and audit logs.
"""

from contextlib import asynccontextmanager
import json
import logging
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from core.engine import WorkflowEngine
from core.external_service import default_external_client
from models.schemas import (
    ApplicationIntakeRequest,
    HealthCheckResponse,
    WorkflowConfig,
    WorkflowExecutionResponse,
)
from storage.database import (
    DuplicateActiveRequestError,
    get_db,
    get_execution_by_application_id,
    init_db,
    reconstruct_response_from_record,
)

# ---------------------------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("workflow-engine")

# ---------------------------------------------------------------------------
# Paths and Constants
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
SAMPLE_CONFIG_PATH = BASE_DIR / "config" / "workflow_sample.json"


def load_default_workflow_config() -> WorkflowConfig:
    """Reads and validates the sample workflow configuration JSON from disk."""
    if not SAMPLE_CONFIG_PATH.exists():
        raise FileNotFoundError(f"Workflow config file not found at: {SAMPLE_CONFIG_PATH}")

    try:
        with open(SAMPLE_CONFIG_PATH, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
        config = WorkflowConfig.model_validate(raw_data)
        logger.info(
            "Loaded workflow config '%s' (v%s) with %d stage(s)",
            config.workflow_id,
            config.version,
            len(config.stages),
        )
        return config
    except Exception as exc:
        logger.error("Failed to parse workflow configuration: %s", exc)
        raise RuntimeError(f"Invalid workflow configuration: {exc}") from exc


# ---------------------------------------------------------------------------
# Application Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manages application startup and shutdown lifecycle events."""
    logger.info("Initializing Configurable Workflow Decision Platform...")

    # Initialize SQLite database schema
    try:
        init_db()
        logger.info("Database schema verified and initialized.")
    except Exception as db_exc:
        logger.error("Failed to initialize database schema: %s", db_exc)

    try:
        # Load and cache default workflow configuration in application state
        app.state.workflow_config = load_default_workflow_config()
        app.state.workflow_engine = WorkflowEngine()
        app.state.external_service = default_external_client
        logger.info("Decision engine and external client components successfully initialized.")
    except Exception as exc:
        logger.warning("Starting without pre-loaded sample workflow config: %s", exc)
        app.state.workflow_config = None
        app.state.workflow_engine = WorkflowEngine()
        app.state.external_service = default_external_client

    yield

    logger.info("Shutting down Configurable Workflow Decision Platform.")


# ---------------------------------------------------------------------------
# FastAPI App Initialization
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Configurable Workflow Decision Platform",
    description=(
        "A declarative, rule-driven workflow execution engine for multi-stage "
        "decisioning, eligibility checks, and risk underwriting with built-in "
        "idempotency, automated retries, and comprehensive audit trails."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

# Enable CORS for developer tooling and client integrations
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------
@app.get(
    "/health",
    response_model=HealthCheckResponse,
    tags=["System"],
    summary="Service Health Check",
    description="Returns heartbeat and operational health status of the service."
)
async def health_check() -> HealthCheckResponse:
    """Provides service status and timestamp."""
    return HealthCheckResponse(
        status="healthy",
        service="configurable-workflow-engine",
        version="1.0.0"
    )


@app.get(
    "/workflow/sample-config",
    response_model=WorkflowConfig,
    tags=["Workflow"],
    summary="Retrieve Default Workflow Configuration",
    description="Inspect the active sample workflow configuration loaded from disk."
)
async def get_sample_config() -> WorkflowConfig:
    """Returns the currently active default workflow specification."""
    config: Optional[WorkflowConfig] = getattr(app.state, "workflow_config", None)
    if not config:
        config = load_default_workflow_config()
        app.state.workflow_config = config
    return config


@app.post(
    "/workflow/execute",
    response_model=WorkflowExecutionResponse,
    status_code=status.HTTP_200_OK,
    tags=["Workflow"],
    summary="Execute Workflow Pipeline",
    description=(
        "Ingests an application request, validates attributes, and executes "
        "the multi-stage decision pipeline defined in the workflow configuration. "
        "Guarantees idempotency on duplicate terminal requests and persists full audit logs."
    )
)
async def execute_workflow(
    request: ApplicationIntakeRequest,
    db: Session = Depends(get_db)
) -> WorkflowExecutionResponse:
    """Evaluates incoming intake payload against the configured decision stages with idempotency."""
    config: Optional[WorkflowConfig] = getattr(app.state, "workflow_config", None)
    if not config:
        try:
            config = load_default_workflow_config()
            app.state.workflow_config = config
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Workflow configuration could not be loaded: {exc}"
            )

    engine: WorkflowEngine = getattr(app.state, "workflow_engine", WorkflowEngine())

    logger.info(
        "Processing application intake: application_id=%s, applicant=%s, amount=%.2f",
        request.application_id,
        request.applicant_name,
        request.amount_requested,
    )

    try:
        result = engine.execute(request=request, config=config, db=db)
    except DuplicateActiveRequestError as dup_err:
        logger.warning("Duplicate active request conflict: %s", dup_err)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(dup_err)
        )

    logger.info(
        "Evaluation completed: application_id=%s, final_status=%s, idempotent_replay=%s",
        result.application_id,
        result.status.value,
        result.is_idempotent_replay,
    )
    return result


@app.get(
    "/workflow/execution/{application_id}",
    response_model=WorkflowExecutionResponse,
    tags=["Workflow"],
    summary="Retrieve Application Execution Audit Log",
    description="Fetches an existing workflow decision record and its stage audit logs by application_id."
)
async def get_execution_record(
    application_id: str,
    db: Session = Depends(get_db)
) -> WorkflowExecutionResponse:
    """Retrieves persisted execution audit records for a given application ID."""
    record = get_execution_by_application_id(db, application_id)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No execution record found for application_id '{application_id}'"
        )
    return reconstruct_response_from_record(record)


@app.post(
    "/workflow/external-screening",
    tags=["External Services"],
    summary="Simulate External Bureau & Blacklist Screening",
    description="Directly triggers the external service integration with retries and exponential backoff."
)
async def run_external_screening(
    application_id: str,
    applicant_name: str
) -> Dict[str, Any]:
    """Test endpoint for evaluating mock external service integration and retries."""
    client = getattr(app.state, "external_service", default_external_client)
    bureau = client.fetch_credit_score(application_id=application_id, applicant_name=applicant_name)
    blacklist = client.check_blacklist(application_id=application_id, applicant_name=applicant_name)
    return {
        "credit_bureau": bureau,
        "blacklist_screening": blacklist
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
