"""Tests for the health check endpoint."""

from fastapi.testclient import TestClient
from main import app

client = TestClient(app)


def test_health_check_returns_200() -> None:
    """Verifies that GET /health responds with 200 OK and healthy status."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "configurable-workflow-engine"
    assert "version" in data
    assert "timestamp" in data

