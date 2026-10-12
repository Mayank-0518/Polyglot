"""Test-only HTTP timings. Trace metadata may contain secrets; never log it."""
import json
import time

import httpx


class RequestTiming:
    STAGES = {
        "connect_tcp": "dns_tcp",
        "start_tls": "tls",
        "send_request_headers": "send",
        "send_request_body": "send",
        "receive_response_headers": "response_wait",
        "receive_response_body": "body",
        "response_closed": "response_release",
        "retry": "retry_wait",
    }

    def __init__(self):
        self.reset()

    def reset(self):
        self.seconds = dict.fromkeys([
            "client_setup", "dns_tcp", "tls", "send", "response_wait", "body",
            "response_release", "json_decode", "cleanup", "retry_wait",
        ], 0.0)
        self.started = {}
        self.requests = self.connections = self.retries = 0

    async def trace(self, event, info):
        _, stage, status = event.rsplit(".", 2)
        if stage not in self.STAGES:
            return
        now = time.perf_counter()
        if status == "started":
            self.started[stage] = now
            self.connections += stage == "connect_tcp"
            self.retries += stage == "retry"
        elif status in {"complete", "failed"}:
            self.seconds[self.STAGES[stage]] += now - self.started.pop(stage)

    def report(self, direction, sample, overall, **metadata):
        measured = sum(self.seconds.values())
        result = {f"{name}_ms": round(seconds * 1000, 3) for name, seconds in self.seconds.items()}
        result.update(
            direction=direction, sample=sample, overall_ms=round(overall * 1000, 3),
            other_ms=round((overall - measured) * 1000, 3),
            requests=self.requests, connection_attempts=self.connections, retries=self.retries,
            transcription_calls=0, synthesis_calls=0, **metadata,
        )
        print("TRANSLATION_TIMING " + json.dumps(result, sort_keys=True))
        return result


class TimedAsyncClient(httpx.AsyncClient):
    def __init__(self, timing, **kwargs):
        self.timing = timing
        started = time.perf_counter()
        super().__init__(**kwargs)
        timing.seconds["client_setup"] += time.perf_counter() - started

    async def post(self, url, **kwargs):
        self.timing.requests += 1
        kwargs["extensions"] = {**kwargs.get("extensions", {}), "trace": self.timing.trace}
        response = await super().post(url, **kwargs)
        original_json = response.json

        def timed_json(**json_kwargs):
            started = time.perf_counter()
            try:
                return original_json(**json_kwargs)
            finally:
                self.timing.seconds["json_decode"] += time.perf_counter() - started

        response.json = timed_json
        return response

    async def __aexit__(self, *args):
        started = time.perf_counter()
        try:
            return await super().__aexit__(*args)
        finally:
            self.timing.seconds["cleanup"] += time.perf_counter() - started
