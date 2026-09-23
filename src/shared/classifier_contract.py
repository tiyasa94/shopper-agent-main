"""Strict classifier wire contract for shopper routing."""

from pydantic import BaseModel, ConfigDict


class GuardrailDecision(BaseModel):
    """Classifier contract with every shared field required."""

    model_config = ConfigDict(extra="forbid")

    location_request: bool
    availability_request: bool
    greeting_only: bool
    live_agent: bool
    policy_override: bool
    medicaid_related: bool
    off_topic: bool
    instructional_bias: bool
    recommendation: bool
    provider_lookup: bool
    enrollment_action: bool
    enroll_now_effective_date_question: bool
    personalized_explanation: bool
