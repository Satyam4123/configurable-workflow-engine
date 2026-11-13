"""Database configuration, SQLAlchemy 2.0 ORM models, and repository operations.

Provides persistent storage for workflow executions, stage evaluations,
and audit trails, alongside robust idempotency checking.
"""

from datetime import datetime
import json
import logging
from typing import Any, Dict, Generator, List, Optional
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    create_engine,
    select,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from models.schemas import (
    DecisionStatus,
    StageExecutionResult,
    WorkflowExecutionResponse,
)

logger = logging.getLogger("workflow-database")

# Default SQLite storage connection string; overrideable via environment
DATABASE_URL = "sqlite:///./workflow_engine.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    echo=False
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)


class Base(DeclarativeBase):
    """Base declarative class for all SQLAlchemy ORM models."""
    pass


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class DuplicateActiveRequestError(Exception):
    """Raised when an application_id is currently being evaluated in a non-terminal state."""
    pass


# ---------------------------------------------------------------------------
# ORM Models
# ---------------------------------------------------------------------------

class WorkflowExecution(Base):
    """Stores high-level workflow decision records and execution metadata."""
    __tablename__ = "workflow_executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    application_id: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    workflow_id: Mapped[str] = mapped_column(String(100), nullable=False)
    workflow_name: Mapped[str] = mapped_column(String(200), nullable=False)
    applicant_name: Mapped[str] = mapped_column(String(200), nullable=False)
    credit_score: Mapped[int] = mapped_column(Integer, nullable=False)
    income: Mapped[float] = mapped_column(Float, nullable=False)
    amount_requested: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    decision_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_terminal: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    request_payload: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Cascading relation to granular stage audit records
    stages: Mapped[List["StageAuditLog"]] = relationship(
        "StageAuditLog",
        back_populates="execution",
        cascade="all, delete-orphan",
        order_by="StageAuditLog.order"
    )


class StageAuditLog(Base):
    """Records granular audit logs for each stage evaluation within an execution."""
    __tablename__ = "stage_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    execution_id: Mapped[int] = mapped_column(
        ForeignKey("workflow_executions.id", ondelete="CASCADE"),
        index=True,
        nullable=False
    )
    stage_id: Mapped[str] = mapped_column(String(100), nullable=False)
    stage_name: Mapped[str] = mapped_column(String(200), nullable=False)
    order: Mapped[int] = mapped_column(Integer, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    failed_checks: Mapped[List[str]] = mapped_column(JSON, default=list)
    failed_conditions: Mapped[List[str]] = mapped_column(JSON, default=list)
    details: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    execution: Mapped["WorkflowExecution"] = relationship(
        "WorkflowExecution",
        back_populates="stages"
    )


# ---------------------------------------------------------------------------
# Database Initialization & Session Dependency
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Initializes tables in the SQLite database if they do not exist."""
    Base.metadata.create_all(bind=engine)
    logger.info("Database schema initialized successfully.")


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a thread-local database session."""
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Idempotency & Repository Helper Functions
# ---------------------------------------------------------------------------

def get_execution_by_application_id(
    db: Session,
    application_id: str
) -> Optional[WorkflowExecution]:
    """Retrieves an existing workflow execution record by application_id."""
    stmt = select(WorkflowExecution).where(WorkflowExecution.application_id == application_id)
    return db.scalars(stmt).first()


def reconstruct_response_from_record(
    record: WorkflowExecution
) -> WorkflowExecutionResponse:
    """Converts a persisted WorkflowExecution ORM record into a WorkflowExecutionResponse DTO."""
    executed_stages: List[StageExecutionResult] = []
    for stage_log in record.stages:
        executed_stages.append(
            StageExecutionResult(
                stage_id=stage_log.stage_id,
                stage_name=stage_log.stage_name,
                order=stage_log.order,
                passed=stage_log.passed,
                failed_checks=stage_log.failed_checks or [],
                failed_conditions=stage_log.failed_conditions or [],
                details=stage_log.details or {}
            )
        )

    return WorkflowExecutionResponse(
        workflow_id=record.workflow_id,
        workflow_name=record.workflow_name,
        application_id=record.application_id,
        status=DecisionStatus(record.status),
        decision_summary=record.decision_summary or "",
        executed_stages=executed_stages,
        evaluated_at=record.updated_at or record.created_at,
        is_idempotent_replay=True,
        execution_id=record.id
    )
