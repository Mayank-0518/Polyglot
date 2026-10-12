import json

import httpx

from vendor_timing import RequestTiming, TimedAsyncClient


async def test_trace_aggregates_stages_and_failed_attempts_without_logging_secrets(monkeypatch, capsys):
    timing = RequestTiming()
    clock = iter([1.0, 1.2, 1.3, 1.4, 1.5, 1.55, 1.6, 1.7])
    monkeypatch.setattr("vendor_timing.time.perf_counter", lambda: next(clock))
    for stage, end in [("connect_tcp", "failed"), ("retry", "complete"),
                       ("send_request_headers", "complete"), ("send_request_body", "complete")]:
        await timing.trace(f"connection.{stage}.started", {"secret": "keep-private"})
        await timing.trace(f"connection.{stage}.{end}", {"secret": "keep-private"})
    result = timing.report("hi-IN to od-IN", "test", 0.8)
    assert result["dns_tcp_ms"] == 200
    assert result["retry_wait_ms"] == 100
    assert result["send_ms"] == 150
    assert result["other_ms"] == 350
    assert (result["requests"], result["connection_attempts"], result["retries"]) == (0, 1, 1)
    assert "keep-private" not in capsys.readouterr().out
    timing.reset()
    assert not timing.started and not any(timing.seconds.values()) and timing.retries == 0


async def test_timed_client_preserves_request_response_and_cleanup():
    timing = RequestTiming()

    def handler(request):
        assert json.loads(request.content) == {"input": "gate 3"}
        assert request.headers["api-subscription-key"] == "unit-test-key"
        assert request.extensions["trace"] == timing.trace
        return httpx.Response(200, json={"translated_text": "अनुवाद"})

    async with TimedAsyncClient(
        timing, timeout=10, headers={"api-subscription-key": "unit-test-key"},
        transport=httpx.MockTransport(handler),
    ) as client:
        response = await client.post("https://api.sarvam.ai/translate", json={"input": "gate 3"})
        assert response.json() == {"translated_text": "अनुवाद"}
    assert client.is_closed
    assert timing.requests == 1
    assert timing.seconds["client_setup"] > 0
    assert timing.seconds["json_decode"] > 0
    assert timing.seconds["cleanup"] > 0
