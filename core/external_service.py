"""Mock External Dependency Service with Exponential Backoff Retries.

Simulates external credit bureau lookups and AML/fraud blacklist verification APIs.
Handles transient failures (timeouts, 500s, 503s) with exponential backoff retries.
"""

from functools import wraps
import logging
import random
import time
from typing import Any, Callable, Dict, Optional, Tuple, TypeVar

# Attempt to import tenacity; provide robust fallback if not yet installed
try:
    from tenacity import (
        retry as tenacity_retry,
        stop_after_attempt,
        wait_exponential,
        retry_if_exception_type,
        before_sleep_log,
    )
    TENACITY_AVAILABLE = True
except ImportError:
    TENACITY_AVAILABLE = False

logger = logging.getLogger("external-service")

F = TypeVar("F", bound=Callable[..., Any])


# ---------------------------------------------------------------------------
# Exception Hierarchy
# ---------------------------------------------------------------------------

class ExternalServiceError(Exception):
    """Base exception for all external API integration failures."""
    pass


class ExternalServiceTransientError(ExternalServiceError):
    """Transient network timeouts, rate limits, or HTTP 500/503 errors eligible for retry."""
    pass


class ExternalServiceTimeoutError(ExternalServiceTransientError):
    """Simulated network connection or read timeout."""
    pass


class ExternalServicePermanentError(ExternalServiceError):
    """Non-retriable failure (e.g., malformed payload, 400 Bad Request, unauthorized 401)."""
    pass


# ---------------------------------------------------------------------------
# Retry Decorator (Tenacity or Fallback Exponential Backoff)
# ---------------------------------------------------------------------------

def retry_with_exponential_backoff(
    max_attempts: int = 3,
    initial_delay: float = 0.1,
    multiplier: float = 2.0,
    max_delay: float = 2.0,
    retry_exceptions: Tuple[type[Exception], ...] = (ExternalServiceTransientError,)
) -> Callable[[F], F]:
    """Retry decorator with exponential backoff.

    Uses `tenacity` when available; falls back to an internal exponential backoff
    implementation if `tenacity` is not installed in the current environment.
    """
    if TENACITY_AVAILABLE:
        return tenacity_retry(
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=initial_delay, min=initial_delay, max=max_delay),
            retry=retry_if_exception_type(retry_exceptions),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,
        )

    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            attempt = 0
            current_delay = initial_delay
            while True:
                attempt += 1
                try:
                    return func(*args, **kwargs)
                except retry_exceptions as exc:
                    if attempt >= max_attempts:
                        logger.error(
                            "External call %s failed after %d attempts. Giving up: %s",
                            func.__name__,
                            attempt,
                            exc,
                        )
                        raise
                    logger.warning(
                        "External call %s failed on attempt %d/%d (%s). Retrying in %.2fs...",
                        func.__name__,
                        attempt,
                        max_attempts,
                        exc,
                        current_delay,
                    )
                    time.sleep(current_delay)
                    current_delay = min(current_delay * multiplier, max_delay)

        return wrapper  # type: ignore[return-value]

    return decorator


# ---------------------------------------------------------------------------
# Mock External Credit Bureau & Blacklist Service Client
# ---------------------------------------------------------------------------

class MockCreditBureauClient:
    """Simulates external bureau and fraud blacklist verification endpoints.

    Features controllable transient error simulation for reliable unit testing
    and realistic chaos engineering.
    """

    def __init__(
        self,
        failure_rate: float = 0.0,
        transient_failures_before_success: int = 0,
        force_timeout: bool = False
    ) -> None:
        """
        Args:
            failure_rate: Probability (0.0 to 1.0) of random transient failure.
            transient_failures_before_success: Deterministic counter; fails N times then succeeds.
            force_timeout: If True, always raises ExternalServiceTimeoutError.
        """
        self.failure_rate = failure_rate
        self.transient_failures_before_success = transient_failures_before_success
        self.force_timeout = force_timeout
        self.total_attempts: int = 0
        self.successful_calls: int = 0

    def reset_counters(self) -> None:
        """Resets simulation attempt counters."""
        self.total_attempts = 0
        self.successful_calls = 0

    def _simulate_network_conditions(self, service_name: str) -> None:
        """Simulates latency and transient disruptions."""
        self.total_attempts += 1

        if self.force_timeout:
            logger.warning("[%s] Forcing connection timeout simulation", service_name)
            raise ExternalServiceTimeoutError(f"Connection to {service_name} timed out after 5000ms")

        if self.transient_failures_before_success > 0:
            self.transient_failures_before_success -= 1
            logger.warning(
                "[%s] Simulating transient HTTP 503 Service Unavailable (%d failures remaining)",
                service_name,
                self.transient_failures_before_success,
            )
            raise ExternalServiceTransientError(
                f"Transient 503 Service Unavailable from {service_name}"
            )

        if self.failure_rate > 0.0 and random.random() < self.failure_rate:
            logger.warning("[%s] Random transient failure triggered", service_name)
            raise ExternalServiceTransientError(
                f"Transient HTTP 500 internal gateway error from {service_name}"
            )

    @retry_with_exponential_backoff(max_attempts=3, initial_delay=0.05, multiplier=2.0)
    def fetch_credit_score(
        self,
        application_id: str,
        applicant_name: str,
        default_score: int = 700
    ) -> Dict[str, Any]:
        """Fetches credit bureau data with automated retries on transient errors.

        Args:
            application_id: Application identifier.
            applicant_name: Applicant's legal name.
            default_score: Baseline credit score returned when successful.

        Returns:
            Dictionary containing bureau verification details.
        """
        self._simulate_network_conditions("CreditBureauAPI")
        self.successful_calls += 1
        logger.info(
            "[CreditBureauAPI] Successfully fetched credit report for application '%s'",
            application_id,
        )
        return {
            "application_id": application_id,
            "applicant_name": applicant_name,
            "bureau_credit_score": default_score,
            "bureau_reference_id": f"BUR-REF-{abs(hash(application_id)) % 1000000:06d}",
            "bureau_status": "VERIFIED",
        }

    @retry_with_exponential_backoff(max_attempts=3, initial_delay=0.05, multiplier=2.0)
    def check_blacklist(
        self,
        application_id: str,
        applicant_name: str
    ) -> Dict[str, Any]:
        """Performs AML/fraud blacklist screening with automated retries.

        Args:
            application_id: Application identifier.
            applicant_name: Applicant's legal name.

        Returns:
            Dictionary with blacklist check result and screening reference.
        """
        self._simulate_network_conditions("BlacklistScreeningAPI")

        # Deterministic simulation: names containing 'fraud' or 'blocked' are blacklisted
        is_blocked = "blocked" in applicant_name.lower() or "fraud" in applicant_name.lower()
        self.successful_calls += 1
        logger.info(
            "[BlacklistScreeningAPI] Screened '%s' for application '%s': cleared=%s",
            applicant_name,
            application_id,
            not is_blocked,
        )
        return {
            "application_id": application_id,
            "applicant_name": applicant_name,
            "blacklist_check_cleared": not is_blocked,
            "screening_reference": f"AML-SCREEN-{abs(hash(applicant_name)) % 1000000:06d}",
            "match_found": is_blocked,
        }


# Global default client instance for the application runtime
default_external_client = MockCreditBureauClient()

