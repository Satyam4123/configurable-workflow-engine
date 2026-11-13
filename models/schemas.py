"""Pydantic schemas and data transfer objects for the Configurable Workflow Decision Platform.

Includes models for:
- Workflow configuration parsing (stages, conditions, operators)
- Incoming request intake validation
- Execution responses, audit logs, and health check payloads
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ComparisonOperator(str, Enum):
    """Supported comparison operators for workflow threshold conditions."""
    EQUALS = "=="
    NOT_EQUALS = "!="
    GREATER_THAN = ">"
    GREATER_THAN_OR_EQUAL = ">="
    LESS_THAN = "<"
    LESS_THAN_OR_EQUAL = "<="
    IN = "in"
    NOT_IN = "not_in"


class DecisionStatus(str, Enum):
    """Decision outcomes for workflow stage and overall pipeline execution."""
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    IN_REVIEW = "IN_REVIEW"
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"


# ---------------------------------------------------------------------------
# Workflow Configuration Models
# ---------------------------------------------------------------------------

class RuleCondition(BaseModel):
    """Defines a threshold condition against a field in the incoming request payload."""
    field: str = Field(
        ...,
        description="Target attribute name in the intake request (e.g., credit_score, income)."
    )
    operator: ComparisonOperator = Field(
        ...,
        description="Logical comparison operator to evaluate."
    )
    value: Union[int, float, str, bool, List[Union[int, float, str]]] = Field(
        ...,
        description="Threshold value or list of accepted values to evaluate against."
    )
    error_message: Optional[str] = Field(
        default=None,
        description="Descriptive failure message returned if condition evaluates to false."
    )
    on_failure_status: Optional[DecisionStatus] = Field(
        default=None,
        description="Failure status override. Set to IN_REVIEW to flag for manual review rather than hard rejection."
    )

    model_config = ConfigDict(
        use_enum_values=True,
        json_schema_extra={
            "example": {
                "field": "credit_score",
                "operator": ">=",
                "value": 650,
                "error_message": "Credit score must be at least 650"
            }
        }
    )


class WorkflowStage(BaseModel):
    """Defines an individual stage in a multi-stage decision pipeline."""
    stage_id: str = Field(..., description="Unique alphanumeric identifier for the stage.")
    stage_name: str = Field(..., description="Human-readable stage title.")
    order: int = Field(..., ge=1, description="Sequential execution index of the stage.")
    description: Optional[str] = Field(default=None, description="Stage scope and responsibility.")
    mandatory_checks: List[str] = Field(
        default_factory=list,
        description="List of required boolean flags that must evaluate to True in request metadata."
    )
    conditions: List[RuleCondition] = Field(
        default_factory=list,
        description="List of threshold rules that must all pass for the stage to succeed."
    )
    on_failure_status: DecisionStatus = Field(
        default=DecisionStatus.REJECTED,
        description="Default decision status assigned if this stage fails (REJECTED or IN_REVIEW)."
    )
    is_manual_review_stage: bool = Field(
        default=False,
        description="Flag indicating if this stage functions as a manual underwriting / review stage."
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "stage_id": "stage_1_pre_qualification",
                "stage_name": "Pre-Qualification & KYC",
                "order": 1,
                "description": "Validates KYC and baseline financial thresholds",
                "mandatory_checks": ["kyc_verified", "blacklist_check_cleared"],
                "conditions": [
                    {
                        "field": "credit_score",
                        "operator": ">=",
                        "value": 650,
                        "error_message": "Credit score must be at least 650"
                    }
                ]
            }
        }
    )


class WorkflowConfig(BaseModel):
    """Root model representing a full declarative workflow configuration."""
    workflow_id: str = Field(..., description="Unique identifier for this workflow specification.")
    workflow_name: str = Field(..., description="Display title for the workflow.")
    version: str = Field(default="1.0.0", description="Semantic version of the configuration.")
    description: Optional[str] = Field(default=None, description="Detailed overview of workflow logic.")
    stages: List[WorkflowStage] = Field(
        ...,
        min_length=1,
        description="Ordered sequence of decision stages to execute."
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "workflow_id": "loan_approval_workflow",
                "workflow_name": "Retail Loan Decision Pipeline",
                "version": "1.0.0",
                "stages": []
            }
        }
    )


# ---------------------------------------------------------------------------
# Intake Request Models
# ---------------------------------------------------------------------------

class ApplicationIntakeRequest(BaseModel):
    """Standardized intake payload for loan, claim, or transaction evaluation requests."""
    application_id: str = Field(
        ...,
        min_length=1,
        description="Unique business identifier for the incoming application."
    )
    applicant_name: str = Field(
        ...,
        min_length=2,
        max_length=150,
        description="Full name of applicant or business entity."
    )
    credit_score: int = Field(
        ...,
        ge=300,
        le=850,
        description="Credit bureau score on standard 300-850 scale."
    )
    income: float = Field(
        ...,
        ge=0.0,
        description="Annual gross verified income in currency units."
    )
    amount_requested: float = Field(
        ...,
        gt=0.0,
        description="Requested monetary amount (e.g. loan principal or claim amount)."
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Extensible key-value metadata holding mandatory verification flags and custom attributes."
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "application_id": "APP-2026-90412",
                "applicant_name": "Jane Doe",
                "credit_score": 720,
                "income": 85000.0,
                "amount_requested": 250000.0,
                "metadata": {
                    "kyc_verified": True,
                    "citizenship_verified": True,
                    "blacklist_check_cleared": True,
                    "employment_status_active": True,
                    "compliance_signoff": True
                }
            }
        }
    )


# ---------------------------------------------------------------------------
# Execution Result & Audit Models
# ---------------------------------------------------------------------------

class StageExecutionResult(BaseModel):
    """Detailed outcome of an individual workflow stage execution and rule evaluation."""
    stage_id: str = Field(..., description="Identifier of evaluated stage.")
    stage_name: str = Field(..., description="Name of evaluated stage.")
    order: int = Field(..., description="Execution sequence index.")
    passed: bool = Field(..., description="Whether all checks and conditions for this stage succeeded.")
    failed_checks: List[str] = Field(
        default_factory=list,
        description="Mandatory checks from request metadata that failed or were missing."
    )
    failed_conditions: List[str] = Field(
        default_factory=list,
        description="Threshold conditions that failed during evaluation."
    )
    details: Dict[str, Any] = Field(
        default_factory=dict,
        description="Diagnostic information, rule evaluation audit trails, and field values."
    )


class WorkflowExecutionResponse(BaseModel):
    """Response returned upon workflow pipeline execution completion or idempotent replay."""
    workflow_id: str = Field(..., description="Identifier of executed workflow.")
    workflow_name: str = Field(..., description="Title of executed workflow.")
    application_id: str = Field(..., description="Target application identifier.")
    status: DecisionStatus = Field(..., description="Final aggregate decision status.")
    decision_summary: str = Field(..., description="High-level explanation of decision outcome.")
    executed_stages: List[StageExecutionResult] = Field(
        default_factory=list,
        description="Granular result breakdown per workflow stage with audit trails."
    )
    evaluated_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="Timestamp indicating when decision evaluation concluded."
    )
    is_idempotent_replay: bool = Field(
        default=False,
        description="Indicates whether this response was served from idempotent database cache."
    )
    execution_id: Optional[int] = Field(
        default=None,
        description="Internal database primary key identifying the persisted execution record."
    )


# ---------------------------------------------------------------------------
# Health & Status Models
# ---------------------------------------------------------------------------

class HealthCheckResponse(BaseModel):
    """System health and heartbeat response payload."""
    status: str = Field(default="healthy", description="Current service health status.")
    service: str = Field(default="configurable-workflow-engine", description="Service name.")
    version: str = Field(default="1.0.0", description="API version.")
    timestamp: datetime = Field(
        default_factory=datetime.utcnow,
        description="Current server timestamp."
    )
