"""
Pytest configuration and fixtures for rag_retrieval_tool tests
"""

import sys
from pathlib import Path

import pytest

# Add src and the deploy-time sibling-tool directory to path.
src_dir = Path(__file__).parent.parent.parent.parent / "src"
sys.path.insert(0, str(src_dir))
sys.path.insert(0, str(src_dir / "tools"))


@pytest.fixture
def mock_agent_context():
    """Fixture providing a mock AgentRun context with all required fields"""
    from ibm_watsonx_orchestrate.run.context import AgentRun, RequestContext

    request_context = RequestContext(
        session_id="test-session-12345",
        user_brand="Anthem",
        user_zip_code="90001",
        user_state_code="CA",
        user_county_code="037",
        user_applicant_count=1,
        user_language="en",
        user_requested_eff_date="2026-01-01",
        user_current_plan="Anthem Silver 70",
        user_dsnp_eligibility="",
        application_market_segment="IND",
        application_exchange_indicator="Off",
        application_current_page="view-all-plans",
        application_available_plans='[{"plan_id": "8XWE", "plan_name": "Anthem Silver 70"}]',
        application_recommended_plans='[{"plan_id": "8XWE", "plan_name": "Anthem Silver 70"}]',
        prospect_type="prospect",
    )

    context = AgentRun(request_context=request_context)
    return context


@pytest.fixture
def mock_agent_context_medicare():
    """Fixture providing a mock AgentRun context for Medicare segment"""
    from ibm_watsonx_orchestrate.run.context import AgentRun, RequestContext

    request_context = RequestContext(
        session_id="test-session-medicare",
        user_brand="Anthem",
        user_zip_code="90001",
        user_state_code="CA",
        user_county_code="037",
        user_applicant_count=1,
        user_language="en",
        user_requested_eff_date="2026-01-01",
        user_current_plan="",
        user_dsnp_eligibility="Yes",
        application_market_segment="Medicare",
        application_exchange_indicator="",
        application_current_page="view-all-plans",
        application_available_plans=(
            '[{"plan_id": "MED123", "plan_name": "Medicare Advantage Plan"}]'
        ),
        application_recommended_plans=(
            '[{"plan_id": "MED123", "plan_name": "Medicare Advantage Plan"}]'
        ),
        prospect_type="prospect",
    )

    context = AgentRun(request_context=request_context)
    return context


@pytest.fixture
def mock_successful_api_response():
    """Fixture providing a mock successful RAG API response"""
    return {
        "status": "success",
        "results": [
            {
                "content": "The deductible for Anthem Silver 70 is $2,500 per individual.",
                "metadata": {
                    "plan_id": "8XWE",
                    "plan_name": "Anthem Silver 70",
                    "document_type": "SBC",
                    "page": 1,
                },
                "score": 0.95,
            },
            {
                "content": "After meeting the deductible, you pay 30% coinsurance.",
                "metadata": {
                    "plan_id": "8XWE",
                    "plan_name": "Anthem Silver 70",
                    "document_type": "SBC",
                    "page": 2,
                },
                "score": 0.88,
            },
        ],
        "plans_found": ["Anthem Silver 70"],
        "plans_searched_for": ["Anthem Silver 70"],
    }


@pytest.fixture
def mock_connection_credentials():
    """Fixture providing mock connection credentials"""
    return {
        "RAG_API_BASE_URL": "https://test-rag-api.example.com",
        "RAG_API_KEY": "test-rag-api-key",
        "RAG_IOLS_ENDPOINT": "/retrieve/iols",
        "RAG_MOLS_ENDPOINT": "/retrieve/mols",
    }


@pytest.fixture
def mock_empty_api_response():
    """Fixture providing a mock empty RAG API response"""
    return {
        "status": "success",
        "results": [],
        "plans_found": [],
        "plans_searched_for": ["Anthem Silver 70"],
    }
