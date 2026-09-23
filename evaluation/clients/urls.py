"""Endpoint allowlists shared by networked evaluation and E2E runners."""

from urllib.parse import urlsplit


def assert_draft_rag_url(url: str) -> None:
    """Allow only loopback or clearly labelled non-production RAG endpoints."""
    parsed = urlsplit(url)
    if parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        return
    hostname = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not any(label in hostname for label in ("dev", "nonprod")):
        raise ValueError(f"RAG API E2E refuses a non-draft URL: {url!r}")
