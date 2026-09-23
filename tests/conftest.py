"""Repository-wide pytest configuration."""

from pathlib import Path

import pytest

LIVE_E2E_MODULES = frozenset(
    {
        "test_local_shopper_api_workflows.py",
        "test_wxo_diagnostic.py",
    }
)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.getgroup("shopper E2E").addoption(
        "--live-e2e",
        action="store_true",
        help="collect tests that call running WXO or Shopper Assistant API services",
    )


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    if config.getoption("--live-e2e"):
        return None
    if collection_path.parent.name == "e2e" and collection_path.name in LIVE_E2E_MODULES:
        return True
    return None
