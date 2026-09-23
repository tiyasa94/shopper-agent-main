"""Agent-facing plan-search evidence and response contracts."""

from typing import Any, Literal

from pydantic import BaseModel, Field

from shared.rag import SearchPlanMetadata

CANONICAL_NO_EVIDENCE_RESPONSE = "The available documents do not establish the requested fact."


class PlanPassage(BaseModel):
    """Plan-attributed evidence retained for answering and public observability."""

    rank: int = Field(ge=1)
    score: float = Field(ge=0.0, le=1.0)
    plan_name: str = Field(description="Canonical display name from the current plan allowlist")
    applicability: Literal["base_plan", "package_specific", "unspecified"] = Field(
        description=(
            "Scope stated by the passage itself. package_specific evidence does not replace a "
            "base_plan benefit; unspecified means the passage does not establish either scope."
        )
    )
    text: str
    metadata: SearchPlanMetadata


class PlanSearchResponse(BaseModel):
    """Candidate plan passages and bounded retrieval status for the agent and Shopper API."""

    outcome: Literal["candidates", "insufficient", "error"] = Field(
        description=(
            "candidates means retrieval returned possible evidence; it does not establish that "
            "the passages answer the question. insufficient and error contain no evidence and "
            "must not be supplemented from conversation history"
        )
    )
    business_intent: Literal["specific_plan", "broad_plans"] | None = None
    requested_fact: str = Field(
        default="",
        description="Trusted current-turn fact that candidate passages must directly establish",
    )
    evidence_status: Literal["unreviewed_candidates", "unavailable"] = "unavailable"
    table_interpretation: str | None = Field(
        default=None,
        description=(
            "Evidence-reading guidance included once when any retained passage contains a table"
        ),
        exclude_if=lambda value: value is None,
    )
    response_instructions: str | None = Field(
        default=None,
        description=(
            "Additional instructions for interpreting this tool response and drafting its "
            "supported answer"
        ),
        exclude_if=lambda value: value is None,
    )
    required_response_if_unsupported: str = Field(
        default=CANONICAL_NO_EVIDENCE_RESPONSE,
        description=(
            "For outcome=candidates only: use this response after reviewing the candidate "
            "passages in this same tool response and finding that they do not directly "
            "establish requested_fact. If another current tool result supports an independently "
            "requested part, use this response only for the unsupported part"
        ),
    )
    results: list[PlanPassage] = Field(default_factory=list)
    plans_found: list[str] = Field(default_factory=list)
    plans_searched_for: list[str] = Field(
        default_factory=list,
        description="Canonical display names corresponding to validated requested plan IDs",
    )
    coverage_complete: bool = Field(
        default=False,
        description=(
            "Whether at least one attributable candidate passage was retained for every selected "
            "plan. Support for each reported fact is determined separately from a passage for that "
            "same plan."
        ),
    )
    missing_plans: list[str] = Field(default_factory=list)
    insufficiency_reason: str | None = None
    error_message: str | None = None
    no_results_reason: str | None = None
    required_response: str | None = Field(
        default=None,
        description=(
            "When present, evidence for this tool request is unavailable. This fallback is scoped "
            "to this tool request and must never discard an independently requested part supported "
            "by another current tool result. When no such supported part exists, the final "
            "response must equal this text verbatim without headings or additions"
        ),
    )
    required_response_addendum: str | None = None
    retrieval_method: str | None = None
    retrieval_time_ms: int | None = None
    escalation: dict[str, Any] = Field(default_factory=lambda: {"type": False})
    audit_completed: bool = False
