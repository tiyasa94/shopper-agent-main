"""Tests for the WXO Draft/Live deployment contract."""

import importlib.util
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/wxo_environment_contract.py"
SPEC = importlib.util.spec_from_file_location("wxo_environment_contract", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
PREFLIGHT_SCRIPT = ROOT / "deployments/preflight.py"
PREFLIGHT_SPEC = importlib.util.spec_from_file_location("deployment_preflight", PREFLIGHT_SCRIPT)
assert PREFLIGHT_SPEC and PREFLIGHT_SPEC.loader
PREFLIGHT = importlib.util.module_from_spec(PREFLIGHT_SPEC)
PREFLIGHT_SPEC.loader.exec_module(PREFLIGHT)


class FakeClient:
    def __init__(self, *, agent_id: str = "agent-id") -> None:
        self.agent_id = agent_id

    def get_draft_by_name(self, name: str) -> list[dict]:
        return [{"id": self.agent_id, "name": name}]

    def get_environments_for_agent(self, agent_id: str) -> dict:
        assert agent_id == self.agent_id
        return {
            "environments": [
                {"id": "draft-id", "current_version": None},
                {"id": "live-id", "current_version": 6},
            ]
        }


class FakeConfig:
    def __init__(self, profile: dict | None = None) -> None:
        self.profile = profile

    def get(self, section: str, name: str) -> dict:
        assert section == "environments"
        assert name == "shopperbroker"
        if self.profile is None:
            raise KeyError(name)
        return self.profile


def snapshot(live_version: int) -> dict:
    return {
        "agent_name": "agent",
        "agent_id": "agent-id",
        "environments": {
            "draft": {"id": "draft-id", "current_version": None},
            "live": {"id": "live-id", "current_version": live_version},
        },
    }


def test_inspect_contract_resolves_configured_environment_ids() -> None:
    result = MODULE.inspect_contract(
        agent_name="agent",
        expected_agent_id="agent-id",
        expected_draft_environment_id="draft-id",
        expected_live_environment_id="live-id",
        client=FakeClient(),
    )

    assert result["environments"]["draft"]["current_version"] is None
    assert result["environments"]["live"]["current_version"] == 6


def test_inspect_contract_rejects_agent_id_drift() -> None:
    with pytest.raises(MODULE.ContractError, match="Agent ID mismatch"):
        MODULE.inspect_contract(
            agent_name="agent",
            expected_agent_id="wrong-id",
            expected_draft_environment_id="draft-id",
            expected_live_environment_id="live-id",
            client=FakeClient(),
        )


def test_cli_profile_must_match_tracked_tenant_identity() -> None:
    expected = {
        "wxo_url": "https://api.us-south.watson-orchestrate.cloud.ibm.com/instances/tenant-id",
    }

    assert MODULE.verify_cli_profile(
        profile_name="shopperbroker",
        expected_url=expected["wxo_url"],
        config=FakeConfig(expected),
    ) == {
        "profile_name": "shopperbroker",
        "url": expected["wxo_url"],
    }

    with pytest.raises(MODULE.ContractError, match="URL does not match"):
        MODULE.verify_cli_profile(
            profile_name="shopperbroker",
            expected_url=expected["wxo_url"],
            config=FakeConfig({**expected, "wxo_url": "https://wrong.example.test"}),
        )
    with pytest.raises(MODULE.ContractError, match="is not configured"):
        MODULE.verify_cli_profile(
            profile_name="shopperbroker",
            expected_url=expected["wxo_url"],
            config=FakeConfig(),
        )


def test_draft_import_must_leave_live_version_unchanged() -> None:
    MODULE.assert_transition(snapshot(6), snapshot(6), operation="draft_import")

    with pytest.raises(MODULE.ContractError, match="Draft import changed Live version"):
        MODULE.assert_transition(snapshot(6), snapshot(7), operation="draft_import")


def test_live_promotion_must_advance_live_version() -> None:
    MODULE.assert_transition(snapshot(6), snapshot(7), operation="live_promotion")

    with pytest.raises(MODULE.ContractError, match="did not advance"):
        MODULE.assert_transition(snapshot(6), snapshot(6), operation="live_promotion")


@pytest.mark.parametrize(
    ("target", "rag_environment", "connection_environment"),
    (("draft", "dev", "draft"), ("live", "stage", "live")),
)
def test_cloud_manifests_pair_rag_and_wxo_environments(
    target: str, rag_environment: str, connection_environment: str
) -> None:
    path = ROOT / "deployments" / target / "manifest.toml"
    with path.open("rb") as handle:
        manifest = tomllib.load(handle)

    assert manifest["config_map"]["RAG_API_ENVIRONMENT"] == rag_environment
    assert manifest["connections"]["credentials"]["elevance-rag-tool-anthem"]["envs"] == [
        connection_environment
    ]
    assert manifest["connections"]["credentials"]["elevance-rag-tool-anthem"]["secret_vars"] == [
        "RAG_API_KEY"
    ]


def test_cloud_identity_is_shared_once_outside_target_manifests() -> None:
    with (ROOT / "deployments/cloud.toml").open("rb") as handle:
        cloud_manifest = tomllib.load(handle)
    cloud_config = cloud_manifest["config_map"]
    tenant = cloud_manifest["wxo_tenant"]

    shared_keys = {
        "WXO_AGENT_ID",
        "WXO_DRAFT_ENVIRONMENT_ID",
        "WXO_LIVE_ENVIRONMENT_ID",
    }
    assert shared_keys <= cloud_config.keys()
    assert tenant["resource_name"] == "shopper-wxo-nonprod"
    assert tenant["cli_alias"] == "shopperbroker"
    assert tenant["instance_id"] in tenant["api_url"]
    assert tenant["region"] == "us-south"
    assert tenant["auth_type"] == "ibm_iam"

    for target in ("draft", "live"):
        with (ROOT / f"deployments/{target}/manifest.toml").open("rb") as handle:
            target_config = tomllib.load(handle)["config_map"]
        assert shared_keys.isdisjoint(target_config)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("cli_alias", "shopper broker", "unsupported characters"),
        ("instance_id", "not-a-uuid", "instance_id must be a UUID"),
        ("api_url", "https://wrong.example.test", "api_url must match"),
        ("auth_type", "other", "auth_type must be ibm_iam"),
    ),
)
def test_preflight_rejects_wxo_tenant_identity_drift(field: str, value: str, message: str) -> None:
    with (ROOT / "deployments/cloud.toml").open("rb") as handle:
        tenant = tomllib.load(handle)["wxo_tenant"]

    PREFLIGHT.validate_wxo_tenant(tenant)
    with pytest.raises(ValueError, match=message):
        PREFLIGHT.validate_wxo_tenant({**tenant, field: value})


@pytest.mark.parametrize(
    "authority",
    (
        "operator:secret@api.us-south.watson-orchestrate.cloud.ibm.com",
        "api.us-south.watson-orchestrate.cloud.ibm.com:443",
    ),
)
def test_preflight_rejects_tenant_url_userinfo_and_ports(authority: str) -> None:
    with (ROOT / "deployments/cloud.toml").open("rb") as handle:
        tenant = tomllib.load(handle)["wxo_tenant"]

    tenant_url = tenant["api_url"]
    _, path = tenant_url.split(".com", maxsplit=1)
    with pytest.raises(ValueError, match="api_url must match"):
        PREFLIGHT.validate_wxo_tenant({**tenant, "api_url": f"https://{authority}{path}"})
