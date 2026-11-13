"""Rule and condition evaluation engine for workflow stages.

Evaluates mandatory checks and dynamic threshold conditions against incoming request payloads.
"""

import operator
from typing import Any, Callable, Dict, List, Tuple
from models.schemas import ComparisonOperator, RuleCondition


class ConditionEvaluator:
    """Evaluates rule conditions and mandatory checks against an intake payload."""

    _OPERATOR_MAP: Dict[str, Callable[[Any, Any], bool]] = {
        ComparisonOperator.EQUALS.value: operator.eq,
        ComparisonOperator.NOT_EQUALS.value: operator.ne,
        ComparisonOperator.GREATER_THAN.value: operator.gt,
        ComparisonOperator.GREATER_THAN_OR_EQUAL.value: operator.ge,
        ComparisonOperator.LESS_THAN.value: operator.lt,
        ComparisonOperator.LESS_THAN_OR_EQUAL.value: operator.le,
        ComparisonOperator.IN.value: lambda a, b: a in b if b is not None else False,
        ComparisonOperator.NOT_IN.value: lambda a, b: a not in b if b is not None else True,
    }

    @classmethod
    def evaluate_mandatory_checks(
        cls,
        mandatory_checks: List[str],
        metadata: Dict[str, Any]
    ) -> List[str]:
        """Validates that all mandatory check flags exist and evaluate to True in metadata.

        Args:
            mandatory_checks: List of required check names (e.g. ['kyc_verified']).
            metadata: Request metadata dictionary containing verification flags.

        Returns:
            List of failed check names (empty if all passed).
        """
        failed_checks: List[str] = []
        for check in mandatory_checks:
            check_val = metadata.get(check)
            if check_val is not True:
                failed_checks.append(check)
        return failed_checks

    @classmethod
    def evaluate_condition(
        cls,
        condition: RuleCondition,
        payload_data: Dict[str, Any]
    ) -> Tuple[bool, str]:
        """Evaluates a single threshold condition against a payload dictionary.

        Args:
            condition: The RuleCondition definition containing field, operator, and threshold value.
            payload_data: Unified key-value dictionary representing request data and metadata.

        Returns:
            Tuple of (passed: bool, error_message: str)
        """
        field_name = condition.field
        op_val = condition.operator.value if hasattr(condition.operator, "value") else str(condition.operator)
        target_val = condition.value
        error_msg = condition.error_message or f"Condition failed for field '{field_name}' ({op_val} {target_val})"

        if field_name not in payload_data:
            return False, f"Field '{field_name}' not found in request payload"

        actual_val = payload_data[field_name]
        comparator = cls._OPERATOR_MAP.get(op_val)

        if not comparator:
            return False, f"Unsupported operator '{op_val}' for condition evaluation"

        try:
            passed = bool(comparator(actual_val, target_val))
            return passed, "" if passed else error_msg
        except (TypeError, ValueError) as err:
            return False, f"Type error evaluating '{field_name}' with value '{actual_val}': {err}"

