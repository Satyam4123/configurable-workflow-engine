"""Workflow execution engine orchestrating stages, evaluations, persistence, and audit logging."""

from datetime import datetime
import logging
from typing import Any, Dict, List, Optional
from sqlalchemy.orm import Session

from core.evaluator import ConditionEvaluator
from core.external_service import (
    MockCreditBureauClient,
    default_external_client,
)
from models.schemas import (
    ApplicationIntakeRequest,
    DecisionStatus,
    StageExecutionResult,
    WorkflowConfig,
    WorkflowExecutionResponse,
)
from storage.database import (
    DuplicateActiveRequestError,
    StageAuditLog,
    WorkflowExecution,
    get_execution_by_application_id,
    reconstruct_response_from_record,
)

logger = logging.getLogger("workflow-engine")


class WorkflowEngine:
    """Orchestrates multi-stage workflow pipeline execution with idempotency, persistence, and audit trails."""

    def __init__(
        self,
        condition_evaluator: type[ConditionEvaluator] = ConditionEvaluator,
        external_service: Optional[MockCreditBureauClient] = None
    ) -> None:
        self.evaluator = condition_evaluator
        self.external_service = external_service or default_external_client

    def _extract_payload_dict(self, request: ApplicationIntakeRequest) -> Dict[str, Any]:
        """Flattens request fields and metadata into an evaluable lookup dictionary."""
        data = request.model_dump()
        metadata = data.get("metadata", {}) or {}
        # Merge metadata at root so conditions can reference either top-level or metadata fields
        combined = {**data, **metadata}
        return combined

    def execute(
        self,
        request: ApplicationIntakeRequest,
        config: WorkflowConfig,
        db: Optional[Session] = None,
        fail_fast: bool = False
    ) -> WorkflowExecutionResponse:
        """Executes the workflow pipeline against the provided application request.

        Args:
            request: Validated incoming intake application.
            config: Target workflow configuration specifying stages, checks, and rules.
            db: Optional active SQLAlchemy database session for idempotency and audit persistence.
            fail_fast: When True, halts further stage evaluation immediately upon first failure.

        Returns:
            WorkflowExecutionResponse containing final status and granular stage results.

        Raises:
            DuplicateActiveRequestError: If the application_id is currently being evaluated.
        """
        execution_record: Optional[WorkflowExecution] = None

        # -------------------------------------------------------------------
        # 1. Idempotency Check & Active State Locking
        # -------------------------------------------------------------------
        if db is not None:
            existing = get_execution_by_application_id(db, request.application_id)
            if existing is not None:
                if existing.is_terminal:
                    logger.info(
                        "Idempotency hit: Application '%s' already executed with terminal status '%s'. "
                        "Returning persisted decision and audit trail.",
                        request.application_id,
                        existing.status,
                    )
                    return reconstruct_response_from_record(existing)
                else:
                    logger.warning(
                        "Duplicate active request detected for application '%s' with status '%s'.",
                        request.application_id,
                        existing.status,
                    )
                    raise DuplicateActiveRequestError(
                        f"Application '{request.application_id}' is currently processing. "
                        "Concurrent duplicate evaluation requests are rejected."
                    )

            # Register initial active execution record in database
            execution_record = WorkflowExecution(
                application_id=request.application_id,
                workflow_id=config.workflow_id,
                workflow_name=config.workflow_name,
                applicant_name=request.applicant_name,
                credit_score=request.credit_score,
                income=request.income,
                amount_requested=request.amount_requested,
                status=DecisionStatus.PROCESSING.value,
                is_terminal=False,
                request_payload=request.model_dump(),
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            db.add(execution_record)
            db.commit()
            db.refresh(execution_record)

        # -------------------------------------------------------------------
        # 2. Automated External Service Enrichment (Blacklist Screening)
        # -------------------------------------------------------------------
        metadata = dict(request.metadata or {})
        requires_blacklist = any(
            "blacklist_check_cleared" in stage.mandatory_checks
            for stage in config.stages
        )

        external_audit_details: Dict[str, Any] = {}
        if requires_blacklist and "blacklist_check_cleared" not in metadata:
            try:
                screening_result = self.external_service.check_blacklist(
                    application_id=request.application_id,
                    applicant_name=request.applicant_name
                )
                metadata["blacklist_check_cleared"] = screening_result.get("blacklist_check_cleared", False)
                external_audit_details["blacklist_screening"] = screening_result
            except Exception as exc:
                logger.error(
                    "External blacklist screening failed for '%s' after retries: %s",
                    request.application_id,
                    exc,
                )
                metadata["blacklist_check_cleared"] = False
                external_audit_details["blacklist_screening_error"] = str(exc)

        # Build combined evaluation payload
        payload_dict = {**request.model_dump(), **metadata}

        # -------------------------------------------------------------------
        # 3. Stage Evaluation and Granular Rule Trigger Audit Logging
        # -------------------------------------------------------------------
        executed_stages: List[StageExecutionResult] = []
        overall_passed = True
        sorted_stages = sorted(config.stages, key=lambda s: s.order)

        for stage in sorted_stages:
            failed_checks = self.evaluator.evaluate_mandatory_checks(
                mandatory_checks=stage.mandatory_checks,
                metadata=metadata
            )

            failed_conditions: List[str] = []
            rules_audit: List[Dict[str, Any]] = []

            for condition in stage.conditions:
                passed, err = self.evaluator.evaluate_condition(
                    condition=condition,
                    payload_data=payload_dict
                )
                if not passed:
                    failed_conditions.append(err)

                rules_audit.append({
                    "field": condition.field,
                    "operator": condition.operator.value if hasattr(condition.operator, "value") else str(condition.operator),
                    "threshold_value": condition.value,
                    "actual_value": payload_dict.get(condition.field),
                    "passed": passed,
                    "error_message": err if not passed else None,
                })

            stage_passed = (len(failed_checks) == 0 and len(failed_conditions) == 0)
            if not stage_passed:
                overall_passed = False

            stage_details: Dict[str, Any] = {
                "mandatory_checks_evaluated": stage.mandatory_checks,
                "rules_evaluated": rules_audit,
            }
            if external_audit_details:
                stage_details["external_integrations"] = external_audit_details

            stage_result = StageExecutionResult(
                stage_id=stage.stage_id,
                stage_name=stage.stage_name,
                order=stage.order,
                passed=stage_passed,
                failed_checks=failed_checks,
                failed_conditions=failed_conditions,
                details=stage_details,
            )
            executed_stages.append(stage_result)

            # Persist stage audit log if database session is active
            if db is not None and execution_record is not None:
                stage_log = StageAuditLog(
                    execution_id=execution_record.id,
                    stage_id=stage.stage_id,
                    stage_name=stage.stage_name,
                    order=stage.order,
                    passed=stage_passed,
                    failed_checks=failed_checks,
                    failed_conditions=failed_conditions,
                    details=stage_details,
                    evaluated_at=datetime.utcnow(),
                )
                db.add(stage_log)

            if fail_fast and not stage_passed:
                break

        # -------------------------------------------------------------------
        # 4. Final Decision Determination & Persistence
        # -------------------------------------------------------------------
        if overall_passed:
            status = DecisionStatus.APPROVED
            summary = "Application satisfies all mandatory checks and threshold conditions across all stages."
        else:
            status = DecisionStatus.REJECTED
            summary = "Application failed one or more mandatory verification checks or threshold conditions."

        execution_id: Optional[int] = None

        if db is not None and execution_record is not None:
            execution_record.status = status.value
            execution_record.decision_summary = summary
            execution_record.is_terminal = True
            execution_record.updated_at = datetime.utcnow()
            db.commit()
            db.refresh(execution_record)
            execution_id = execution_record.id

        return WorkflowExecutionResponse(
            workflow_id=config.workflow_id,
            workflow_name=config.workflow_name,
            application_id=request.application_id,
            status=status,
            decision_summary=summary,
            executed_stages=executed_stages,
            evaluated_at=datetime.utcnow(),
            is_idempotent_replay=False,
            execution_id=execution_id,
        )
