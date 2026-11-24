"""The harness never sends its API key in cleartext beyond this machine (review F1).

Every client here uses an injected ``httpx.MockTransport`` that records what would
have reached the wire, so no test opens a socket to anything but loopback and no
external host is contacted. The page tests also replace httpx's real transport
with a recorder, so a regression would be caught before any connection.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import httpx
import pytest
from ui.harness.client import (
    ApiError,
    SentinelFlowClient,
    is_loopback_url,
    require_safe_key_transport,
)

from tests.ui.conftest import LiveApiFactory, closed_port_url

KEY = "f1-probe-key-not-a-secret"
APP = str(Path(__file__).resolve().parents[2] / "ui" / "streamlit_app.py")

LOOPBACK = [
    "http://127.0.0.1:8000",
    "http://127.0.0.2:8000",  # all of 127.0.0.0/8 is loopback
    "http://127.255.255.254",
    "http://[::1]:8000",
    "http://localhost:8000",
    "HTTP://LOCALHOST:8000",
    "Http://127.0.0.1/",
]
NOT_LOOPBACK = [
    "http://127.evil.com:8000",  # the reproduced spoof
    "http://localhost.evil.com:8000",
    "http://127.0.0.1.evil.com",
    "http://127.0.0.1@evil.com/",  # userinfo: the host is evil.com
    "http://203.0.113.10:8000",
    "http://0.0.0.0:8000",
    "http://[::ffff:203.0.113.10]:8000",
    "http://10.0.0.5",
    "http://2130706433/",  # integer form is not accepted as an address
    "not a url",
    "",
]


class Recorder:
    """A transport that records requests and answers like a healthy API."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={"status": "ok", "service": "SentinelFlow"})

    @property
    def authorizations(self) -> list[str | None]:
        return [request.headers.get("authorization") for request in self.requests]


@pytest.mark.parametrize("url", LOOPBACK)
def test_loopback_urls_are_recognised(url: str) -> None:
    assert is_loopback_url(url)


@pytest.mark.parametrize("url", NOT_LOOPBACK)
def test_dns_names_and_remote_addresses_are_not_loopback(url: str) -> None:
    assert not is_loopback_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.evil.com:8000",
        "http://localhost.evil.com:8000",
        "http://127.0.0.1@evil.com/",
        "http://203.0.113.10:8000",
        "HTTP://203.0.113.10:8000",
        "hTTp://api.example.com",
        "http://0.0.0.0:8000",
        "http://[::ffff:203.0.113.10]:8000",
        "ws://203.0.113.10",
        "not a url",
    ],
)
def test_a_key_is_refused_for_plaintext_non_loopback_urls(url: str) -> None:
    wire = Recorder()
    with pytest.raises(ApiError) as caught:
        SentinelFlowClient(url, api_key=KEY, transport=httpx.MockTransport(wire))
    assert caught.value.code == "INSECURE_API_URL"
    assert KEY not in str(caught.value) and KEY not in caught.value.message
    assert wire.requests == []  # refused before anything was sent


@pytest.mark.parametrize(
    "url",
    [*LOOPBACK, "https://api.example.com", "HTTPS://203.0.113.10:8443", "https://[2001:db8::1]"],
)
def test_loopback_http_and_any_https_still_send_the_key(url: str) -> None:
    wire = Recorder()
    with SentinelFlowClient(url, api_key=KEY, transport=httpx.MockTransport(wire)) as client:
        assert client.health()["status"] == "ok"
    assert wire.authorizations == [f"Bearer {KEY}"]


def test_plain_http_without_a_key_is_allowed_and_sends_no_authorization() -> None:
    wire = Recorder()
    with SentinelFlowClient(
        "http://203.0.113.10:8000", transport=httpx.MockTransport(wire)
    ) as client:
        client.health()
    assert wire.authorizations == [None]
    require_safe_key_transport("http://203.0.113.10:8000", None)  # no key, nothing to guard
    require_safe_key_transport("http://203.0.113.10:8000", "")


def test_the_guard_also_runs_on_each_request_and_redirects_are_not_followed() -> None:
    wire = Recorder()
    with SentinelFlowClient(
        "https://api.example.com", api_key=KEY, transport=httpx.MockTransport(wire)
    ) as client:
        # A request whose final URL is plaintext and remote is stopped before the wire,
        # even on a client that was built for an https base URL.
        with pytest.raises(ApiError) as caught:
            client._http.get("http://203.0.113.10/health")
        assert caught.value.code == "INSECURE_API_URL"
    assert wire.requests == []

    hops: list[httpx.Request] = []

    def redirecting(request: httpx.Request) -> httpx.Response:
        hops.append(request)
        return httpx.Response(302, headers={"Location": "http://203.0.113.10/health"})

    with (
        SentinelFlowClient(
            "https://api.example.com", api_key=KEY, transport=httpx.MockTransport(redirecting)
        ) as client,
        pytest.raises(ApiError),
    ):
        client.health()
    assert [str(request.url) for request in hops] == ["https://api.example.com/health"]


# -- the page --------------------------------------------------------------------------


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """Replace httpx's real transport: any request the page makes is recorded, not sent."""
    recorder = Recorder()

    def handle(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        return recorder(request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    return recorder


def _page(monkeypatch: pytest.MonkeyPatch, url: str, env_key: str | None) -> Any:
    pytest.importorskip("streamlit", reason="needs the ui extra")
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("SENTINELFLOW_API_URL", url)
    if env_key is None:
        monkeypatch.delenv("SENTINELFLOW_API_KEY", raising=False)
    else:
        monkeypatch.setenv("SENTINELFLOW_API_KEY", env_key)
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    assert not at.exception, [item.value for item in at.exception]
    return at


def _visible(at: Any) -> str:
    parts = [item.value for item in at.markdown]
    for group in (at.error, at.warning, at.info, at.caption, at.success):
        parts += [str(item.value) for item in group]
    # Other inputs' values count as page text; the password field holds what the
    # user typed and is masked by the browser.
    parts += [str(item.value) for item in at.text_input if item.key != "api_key_input"]
    return "\n".join(parts)


@pytest.mark.parametrize("url", ["http://127.evil.com:8000", "HTTP://203.0.113.10:8000"])
def test_page_refuses_an_environment_key_for_an_unsafe_url(
    monkeypatch: pytest.MonkeyPatch, wire: Recorder, url: str
) -> None:
    at = _page(monkeypatch, url, env_key=KEY)
    text = _visible(at)
    assert "INSECURE_API_URL" in text and KEY not in text
    assert not any(button.key == "launch" for button in at.button)  # workspace not built
    at.button(key="check_connection").click().run()
    assert not at.exception
    assert wire.requests == []


def test_page_refuses_a_typed_key_then_recovers_on_a_loopback_url(
    monkeypatch: pytest.MonkeyPatch, wire: Recorder
) -> None:
    at = _page(monkeypatch, "http://localhost.evil.com:8000", env_key=None)
    assert any(button.key == "launch" for button in at.button)  # no key: nothing to guard
    at.text_input(key="api_key_input").input(KEY).run()
    assert not at.exception
    assert "INSECURE_API_URL" in _visible(at) and KEY not in _visible(at)
    assert not any(button.key == "launch" for button in at.button)
    assert wire.requests == []

    at.text_input(key="api_url").input(closed_port_url()).run()  # loopback: allowed again
    assert not at.exception
    assert "INSECURE_API_URL" not in _visible(at)
    assert any(button.key == "launch" for button in at.button)


# -- review L1: the decision card escapes the verdict -----------------------------------


def test_decision_card_escapes_the_verdict(
    live_api: LiveApiFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("streamlit", reason="needs the ui extra")
    from ui.harness import page

    with live_api() as api, SentinelFlowClient(api.base_url) as client:
        job_id = client.create_analysis(
            {"change_id": "CHG-H001", "domain_pack_id": "healthcare-claims"},
            idempotency_key="l1",
        ).job["job_id"]
        api.wait_for(job_id, {"completed"})
        job = client.get_analysis(job_id)
        decision = copy.deepcopy(client.get_decision(job_id))
    hostile = "<img src=x onerror=alert(1)>"
    decision["result"]["risk_assessment"]["decision"] = hostile
    rendered: list[str] = []
    monkeypatch.setattr(page.st, "markdown", lambda body, **_: rendered.append(str(body)))
    page._decision_panel(job, decision)
    card = next(body for body in rendered if "sf-decision" in body)
    assert hostile not in card
    assert "&lt;img src=x onerror=alert(1)&gt;" in card
    assert "background:#57606a" in card  # unknown verdict: neutral colour, layout kept
