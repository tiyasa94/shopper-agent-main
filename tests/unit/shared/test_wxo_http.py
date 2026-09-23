import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import pytest

from shared import wxo_http

IAM = "https://iam.example.test/token"
INSTANCE = "https://wxo.example.test/instances/test"


def _response(token="token", **fields):
    response = Mock()
    response.json.return_value = {"access_token": token, "expires_in": 3600, **fields}
    return response


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch):
    monkeypatch.setattr(wxo_http, "_TOKENS", {})
    monkeypatch.setattr(wxo_http, "_THREAD", threading.local())


def test_reuses_token_then_refreshes_before_expiration():
    with (
        patch.object(wxo_http.time, "monotonic", return_value=1000) as clock,
        patch.object(
            wxo_http, "post", side_effect=[_response("first"), _response("second")]
        ) as post,
    ):
        assert wxo_http.iam_token("key", IAM) == "first"
        clock.return_value = 4539
        assert wxo_http.iam_token("key", IAM) == "first"
        clock.return_value = 4540
        assert wxo_http.iam_token("key", IAM) == "second"
    assert post.call_count == 2


@pytest.mark.parametrize("change", ["key", "iam", "instance"])
def test_cache_is_scoped_to_credential_issuer_and_instance(change):
    with patch.object(wxo_http, "post", side_effect=[_response("first"), _response("second")]):
        assert wxo_http.iam_token("key", IAM, instance_url=INSTANCE) == "first"
        assert (
            wxo_http.iam_token(
                "other-key" if change == "key" else "key",
                IAM + "/other" if change == "iam" else IAM,
                instance_url=INSTANCE + "other" if change == "instance" else INSTANCE,
            )
            == "second"
        )
        assert wxo_http.iam_token("key", IAM, instance_url=INSTANCE) == "first"


def test_concurrent_expiry_requests_share_one_iam_exchange():
    barrier = threading.Barrier(8)

    def get_token(_):
        barrier.wait(timeout=5)
        return wxo_http.iam_token("key", IAM)

    with (
        patch.object(wxo_http, "post", return_value=_response()) as post,
        ThreadPoolExecutor(max_workers=8) as executor,
    ):
        assert list(executor.map(get_token, range(8))) == ["token"] * 8
    post.assert_called_once()


def test_late_401_reuses_token_refreshed_by_another_request():
    with patch.object(
        wxo_http, "post", side_effect=[_response("first"), _response("second")]
    ) as post:
        assert wxo_http.iam_token("key", IAM) == "first"
        assert wxo_http.iam_token("key", IAM, rejected_token="first") == "second"
        assert wxo_http.iam_token("key", IAM, rejected_token="first") == "second"
    assert post.call_count == 2


def test_failed_refresh_does_not_leave_rejected_token_cached():
    with patch.object(wxo_http, "post", return_value=_response("rejected")):
        wxo_http.iam_token("key", IAM)
    with (
        patch.object(wxo_http, "post", side_effect=ValueError("IAM unavailable")),
        pytest.raises(ValueError),
    ):
        wxo_http.iam_token("key", IAM, rejected_token="rejected")
    assert not wxo_http._TOKENS


@pytest.mark.parametrize("lifetime", [None, True, 0, -1, "3600", float("inf"), float("nan")])
def test_invalid_lifetime_is_not_cached(lifetime):
    with (
        patch.object(wxo_http, "post", return_value=_response(expires_in=lifetime)),
        pytest.raises(ValueError, match="lifetime"),
    ):
        wxo_http.iam_token("key", IAM)
    assert not wxo_http._TOKENS


def test_expiration_only_response_and_short_lived_tokens():
    with (
        patch.object(wxo_http.time, "time", return_value=1000),
        patch.object(wxo_http.time, "monotonic", return_value=2000) as clock,
        patch.object(
            wxo_http, "post", return_value=_response(expires_in=None, expiration=1030)
        ) as post,
    ):
        assert wxo_http.iam_token("key", IAM) == "token"
        clock.return_value = 2026
        assert wxo_http.iam_token("key", IAM) == "token"
    post.assert_called_once()


def test_missing_access_token_is_not_cached():
    response = Mock()
    response.json.return_value = []
    with (
        patch.object(wxo_http, "post", return_value=response),
        pytest.raises(ValueError, match="access_token"),
    ):
        wxo_http.iam_token("key", IAM)
    assert not wxo_http._TOKENS


def test_http_session_reused_per_thread_with_request_scoped_auth():
    with patch.object(wxo_http.requests, "Session") as factory:
        wxo_http.post(INSTANCE, headers={"Authorization": "Bearer first"})
        wxo_http.post(INSTANCE, headers={"Authorization": "Bearer second"})
        factory.assert_called_once()
        assert factory.return_value.post.call_args.kwargs == {
            "headers": {"Authorization": "Bearer second"},
            "allow_redirects": False,
        }
        assert factory.return_value.cookies.clear.call_count == 2
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(wxo_http.post, INSTANCE).result()
        assert factory.call_count == 2


def test_cache_is_bounded_when_credentials_rotate():
    with patch.object(wxo_http, "post", return_value=_response()):
        for i in range(wxo_http._MAX_TOKEN_ENTRIES + 5):
            wxo_http.iam_token(f"key-{i}", IAM)
    assert len(wxo_http._TOKENS) == wxo_http._MAX_TOKEN_ENTRIES
