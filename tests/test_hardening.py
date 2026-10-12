"""P7 failure paths use mocks only; no SIP or vendor requests."""
import asyncio
import logging
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from livekit import api

from app import agent
from app.pipeline import run_direction
from test_agent import context, leg, sip_call, sip_config


async def never(*args, **kwargs):
    await asyncio.Future()


def sip_error(code):
    return api.SipCallError(
        "unavailable", "private provider details", status=503,
        metadata={"sip_status_code": str(code)},
    )


@pytest.mark.parametrize("language", ["hi-IN", "od-IN"])
@pytest.mark.parametrize("kind", ["connected", "busy", "unanswered", "failed", "wait"])
async def test_announcements_use_phone_language_without_counting_utterances(monkeypatch, leg, language, kind):
    leg.language = language
    synthesize = AsyncMock(return_value=(bytes(960), 24000))
    monkeypatch.setattr(agent.vendors, "synthesize", synthesize)
    await leg.announce(kind)
    synthesize.assert_awaited_once_with(agent.PROMPTS[language][kind], language)
    script = (0x0900, 0x097F) if language == "hi-IN" else (0x0B00, 0x0B7F)
    assert any(script[0] <= ord(char) <= script[1] for char in synthesize.call_args.args[0])
    leg.source.capture_frame.assert_awaited_once()
    assert leg.latencies == []


@pytest.mark.parametrize("code,kind", [(486, "busy"), (600, "busy"), (408, "unanswered"), (480, "unanswered"), (503, "failed"), (603, "failed")])
@pytest.mark.parametrize("language", ["hi-IN", "od-IN"])
async def test_sip_failure_announces_to_caller_and_cleans_both_rooms(sip_call, code, kind, language, caplog):
    sip_call.route["caller_lang"] = language
    sip_call.ctx.api.sip.create_sip_participant.side_effect = sip_error(code)
    with pytest.raises(api.SipCallError):
        await agent.entrypoint(sip_call.ctx)
    assert sip_call.legs[0].language == language
    sip_call.legs[0].announce.assert_awaited_once_with(kind)
    assert sip_call.hold_stopped.is_set() and not sip_call.started.is_set()
    sip_call.legs[0].close.assert_awaited_once()
    assert sip_call.ctx.api.room.delete_room.await_count == 2
    assert "private provider details" not in caplog.text


async def test_connected_announcements_finish_before_translation(sip_call):
    task = asyncio.create_task(agent.entrypoint(sip_call.ctx))
    await asyncio.wait_for(sip_call.started.wait(), 1)
    for leg in sip_call.legs:
        leg.announce.assert_awaited_once_with("connected")
    sip_call.ctx.room.emit("participant_disconnected", sip_call.caller)
    await asyncio.wait_for(task, 1)


async def test_dial_deadline_cancels_hold_and_announces_no_answer(monkeypatch, sip_call):
    monkeypatch.setattr(agent, "DIAL_TIMEOUT", 0.01)
    dial = AsyncMock(side_effect=never)
    sip_call.ctx.api.sip.create_sip_participant = dial
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(agent.entrypoint(sip_call.ctx), 1)
    dial.assert_awaited_once()
    sip_call.legs[0].announce.assert_awaited_once_with("unanswered")
    assert sip_call.hold_stopped.is_set()
    assert sip_call.ctx.api.room.delete_room.await_count == 2


async def test_answer_without_receiver_has_bounded_wait(monkeypatch, sip_call):
    monkeypatch.setattr(agent, "PARTICIPANT_TIMEOUT", 0.01)
    monkeypatch.setattr(agent.utils, "wait_for_participant", never)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(agent.entrypoint(sip_call.ctx), 1)
    sip_call.legs[0].announce.assert_awaited_once_with("unanswered")
    assert not sip_call.started.is_set()
    assert sip_call.hold_stopped.is_set()


@pytest.mark.parametrize("language", ["hi-IN", "od-IN"])
async def test_translation_timeout_prompts_listener_once_then_retries(monkeypatch, leg, language):
    leg.language = language
    source_language = "od-IN" if language == "hi-IN" else "hi-IN"
    speaker = SimpleNamespace(language=source_language)
    translate = AsyncMock(side_effect=[httpx.ReadTimeout("private request URL"), "translated"])
    synthesize = AsyncMock(return_value=(bytes(960), 24000))
    monkeypatch.setattr(agent.vendors, "translate", translate)
    monkeypatch.setattr(agent.vendors, "synthesize", synthesize)
    assert await agent.Leg.translate_to(speaker, "source", leg) == "translated"
    assert translate.await_count == 2
    translate.assert_awaited_with("source", src=source_language, tgt=language)
    synthesize.assert_awaited_once_with(agent.PROMPTS[language]["wait"], language)
    assert leg.latencies == []


async def test_real_three_second_deadline_cancels_slow_vendor_once(leg):
    assert agent.VENDOR_TIMEOUT == 3
    leg.announce = AsyncMock()
    cancelled = asyncio.Event()

    async def slow():
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    operation = AsyncMock()
    async def attempt():
        if operation.await_count == 1:
            return await slow()
        return "retried"

    operation.side_effect = attempt
    start = asyncio.get_running_loop().time()
    assert await leg.vendor_call(operation, leg.generation) == "retried"
    assert 2.8 <= asyncio.get_running_loop().time() - start < 5
    assert cancelled.is_set()
    assert operation.await_count == 2
    leg.announce.assert_awaited_once_with("wait")


async def test_translation_twice_timing_out_skips_sentence_then_continues(monkeypatch, leg, caplog):
    monkeypatch.setattr(agent, "VENDOR_TIMEOUT", 0.01)
    leg.announce = AsyncMock()
    attempts = []

    async def translate(text, **kwargs):
        attempts.append(text)
        if text == "slow":
            await asyncio.Future()
        return "translated next"

    async def finals():
        yield "slow"
        yield "next"

    monkeypatch.setattr(agent.vendors, "translate", translate)
    speaker = SimpleNamespace(language="od-IN")
    speak = AsyncMock()
    await run_direction(finals(), partial(agent.Leg.translate_to, speaker, listener=leg), speak)
    assert attempts == ["slow", "slow", "next"]
    speak.assert_awaited_once_with("translated next")
    leg.announce.assert_awaited_once_with("wait")
    assert "vendor_sentence_skipped" in caplog.text


async def test_synthesis_twice_timing_out_skips_audio_then_next_sentence_plays(monkeypatch, leg):
    monkeypatch.setattr(agent, "VENDOR_TIMEOUT", 0.01)
    leg.announce = AsyncMock()
    attempts = []

    async def synthesize(text, language):
        attempts.append(text)
        if text == "slow":
            await asyncio.Future()
        return bytes(960), 24000

    monkeypatch.setattr(agent.vendors, "synthesize", synthesize)
    speaker = SimpleNamespace(utterance_end=None)
    await leg.speak("slow", speaker)
    leg.source.capture_frame.assert_not_awaited()
    await leg.speak("next", speaker)
    assert attempts == ["slow", "slow", "next"]
    leg.source.capture_frame.assert_awaited_once()
    leg.announce.assert_awaited_once_with("wait")


async def test_failed_wait_prompt_does_not_recurse_or_prevent_retry(monkeypatch, leg):
    monkeypatch.setattr(agent, "VENDOR_TIMEOUT", 0.01)
    synthesize = AsyncMock(side_effect=never)
    monkeypatch.setattr(agent.vendors, "synthesize", synthesize)
    operation = AsyncMock(side_effect=[TimeoutError(), "retried"])
    assert await asyncio.wait_for(leg.vendor_call(operation, leg.generation), 1) == "retried"
    synthesize.assert_awaited_once()
    assert operation.await_count == 2


async def test_non_timeout_vendor_error_is_not_retried(leg):
    leg.announce = AsyncMock()
    operation = AsyncMock(side_effect=ValueError("invalid vendor output"))
    with pytest.raises(ValueError):
        await leg.vendor_call(operation, leg.generation)
    operation.assert_awaited_once()
    leg.announce.assert_not_awaited()


async def test_announcement_playback_failure_clears_queued_prompt(monkeypatch, leg):
    monkeypatch.setattr(agent.vendors, "synthesize", AsyncMock(return_value=(bytes(960), 24000)))
    leg.play = AsyncMock(side_effect=TimeoutError("playout stuck"))
    await leg.announce("wait")
    leg.source.clear_queue.assert_called_once()


async def test_listener_interrupt_during_translation_discards_stale_result(monkeypatch, leg):
    async def translate(*args, **kwargs):
        leg.interrupt()
        return "stale translation"

    monkeypatch.setattr(agent.vendors, "translate", translate)
    with pytest.raises(InterruptedError):
        await agent.Leg.translate_to(SimpleNamespace(language="od-IN"), "source", leg)
    leg.source.capture_frame.assert_not_awaited()


async def test_listener_interrupt_during_wait_prompt_prevents_retry(leg):
    leg.announce = AsyncMock(side_effect=lambda kind: leg.interrupt())
    operation = AsyncMock(side_effect=TimeoutError())
    with pytest.raises(InterruptedError):
        await leg.vendor_call(operation, leg.generation)
    operation.assert_awaited_once()


async def test_cancelled_vendor_is_not_retried(leg):
    leg.announce = AsyncMock()
    started = asyncio.Event()

    async def operation():
        started.set()
        await asyncio.Future()

    task = asyncio.create_task(leg.vendor_call(operation, leg.generation))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    leg.announce.assert_not_awaited()


@pytest.mark.parametrize("phase", ["connect", "caller", "receiver"])
async def test_missing_participant_or_connection_times_out_and_cleans_rooms(monkeypatch, context, phase):
    ctx, room_b, *_ = context
    monkeypatch.setattr(agent, "PARTICIPANT_TIMEOUT", 0.01)
    monkeypatch.setattr(agent, "lookup", AsyncMock(return_value={"caller_lang": "od-IN", "other_lang": "hi-IN"}))
    if phase == "connect":
        ctx.connect.side_effect = never
    elif phase == "caller":
        ctx.wait_for_participant.side_effect = never
    else:
        monkeypatch.setattr(agent.utils, "wait_for_participant", never)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(agent.entrypoint(ctx), 1)
    assert ctx.api.room.delete_room.await_count == (2 if phase == "receiver" else 1)
    ctx.room.disconnect.assert_awaited_once()
    room_b.disconnect.assert_awaited_once()


@pytest.mark.parametrize("phase", ["before_identity", "lookup", "room_b_connect", "caller_publish", "receiver_publish"])
async def test_hangup_during_setup_never_starts_or_resumes_call(monkeypatch, sip_call, phase):
    call = sip_call
    if phase == "before_identity":
        async def caller():
            call.ctx.room.emit("participant_disconnected", call.caller)
            return call.caller
        call.ctx.wait_for_participant.side_effect = caller
    elif phase == "lookup":
        async def lookup(number):
            call.ctx.room.emit("participant_disconnected", call.caller)
            return call.route
        call.lookup.side_effect = lookup
    elif phase == "room_b_connect":
        call.room_b.connect.side_effect = lambda *args, **kwargs: call.room_b.emit("participant_disconnected", call.receiver)
    else:
        leg_class = agent.Leg
        def make_leg(*args):
            result = leg_class(*args)
            index = 1 if phase == "caller_publish" else 2
            if len(call.legs) == index:
                result.publish.side_effect = lambda room: room.emit("disconnected", "network lost")
            return result
        monkeypatch.setattr(agent, "Leg", make_leg)
    await asyncio.wait_for(agent.entrypoint(call.ctx), 1)
    assert not call.started.is_set()
    if phase != "receiver_publish":
        call.ctx.api.sip.create_sip_participant.assert_not_awaited()
    for leg in call.legs:
        leg.close.assert_awaited_once()
    assert all(not callbacks for callbacks in call.ctx.room.callbacks.values())
    assert all(not callbacks for callbacks in call.room_b.callbacks.values())


@pytest.mark.parametrize("error", [RuntimeError("transient"), api.TwirpError("unavailable", "transient", status=503)])
async def test_room_delete_retries_transient_failure_once(error):
    delete = AsyncMock(side_effect=[error, None])
    await agent.delete_room(SimpleNamespace(room=SimpleNamespace(delete_room=delete)), "room")
    assert delete.await_count == 2


async def test_already_deleted_room_is_success_without_retry(caplog):
    delete = AsyncMock(side_effect=api.TwirpError("not_found", "already deleted", status=404))
    await agent.delete_room(SimpleNamespace(room=SimpleNamespace(delete_room=delete)), "room")
    delete.assert_awaited_once()
    assert "room_cleanup_failed" not in caplog.text


@pytest.mark.parametrize("failure", ["timeout", "error"])
async def test_room_cleanup_failure_does_not_block_other_room_or_media(monkeypatch, sip_call, failure, caplog):
    monkeypatch.setattr(agent, "CLEANUP_TIMEOUT", 0.01)
    task = asyncio.create_task(agent.entrypoint(sip_call.ctx))
    await asyncio.wait_for(sip_call.started.wait(), 1)
    attempts = []

    async def delete(request):
        attempts.append(request.room)
        if request.room == "room-a":
            if failure == "timeout":
                await asyncio.Future()
            raise RuntimeError("private endpoint or credentials")

    sip_call.ctx.api.room.delete_room.side_effect = delete
    sip_call.legs[0].close.side_effect = RuntimeError("source close failed")
    sip_call.ctx.room.emit("participant_disconnected", sip_call.caller)
    await asyncio.wait_for(task, 1)
    assert attempts.count("room-a") == 2 and attempts.count("room-a-b") == 1
    for leg in sip_call.legs:
        leg.close.assert_awaited_once()
    sip_call.ctx.room.disconnect.assert_awaited_once()
    sip_call.room_b.disconnect.assert_awaited_once()
    assert "room_cleanup_failed" in caplog.text
    assert "media_cleanup_failed" in caplog.text
    assert "private endpoint or credentials" not in caplog.text


async def test_leg_close_is_idempotent_and_closed_leg_never_calls_vendor(monkeypatch, leg):
    synthesize = AsyncMock()
    monkeypatch.setattr(agent.vendors, "synthesize", synthesize)
    await asyncio.gather(leg.close(), leg.close())
    await leg.speak("late output", SimpleNamespace(utterance_end=None))
    await leg.announce("connected")
    leg.stream.aclose.assert_awaited_once()
    leg.source.aclose.assert_awaited_once()
    synthesize.assert_not_awaited()
    leg.source.capture_frame.assert_not_awaited()


async def test_media_close_timeout_still_closes_other_resource(monkeypatch, leg):
    monkeypatch.setattr(agent, "CLEANUP_TIMEOUT", 0.01)
    leg.stream.aclose.side_effect = never
    await asyncio.wait_for(agent.close_media(leg.close), 1)
    leg.source.aclose.assert_awaited_once()


async def test_cancelled_shutdown_waiter_does_not_cancel_shared_cleanup(monkeypatch, context, caplog):
    ctx, room_b, *_ = context
    monkeypatch.setattr(agent, "lookup", AsyncMock(return_value=None))
    deleting, release = asyncio.Event(), asyncio.Event()

    async def delete(request):
        deleting.set()
        await release.wait()

    ctx.api.room.delete_room.side_effect = delete
    with caplog.at_level(logging.INFO, logger="translator"):
        task = asyncio.create_task(agent.entrypoint(ctx))
        await asyncio.wait_for(deleting.wait(), 1)
        cleanup = ctx.add_shutdown_callback.call_args.args[0]
        waiter = asyncio.create_task(cleanup())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await asyncio.wait_for(cleanup(), 1)
        await cleanup()
    ctx.api.room.delete_room.assert_awaited_once()
    ctx.room.disconnect.assert_awaited_once()
    room_b.disconnect.assert_awaited_once()
    ctx.shutdown.assert_called_once()
    assert caplog.text.count("call_summary call_id=room-a ") == 1


async def test_shutdown_callback_cancels_active_call_once(sip_call, caplog):
    with caplog.at_level(logging.INFO, logger="translator"):
        task = asyncio.create_task(agent.entrypoint(sip_call.ctx))
        await asyncio.wait_for(sip_call.started.wait(), 1)
        cleanup = sip_call.ctx.add_shutdown_callback.call_args.args[0]
        await asyncio.gather(cleanup(), cleanup())
        try:
            await asyncio.wait_for(task, 1)
        except asyncio.CancelledError:
            pass
    assert sip_call.ctx.api.room.delete_room.await_count == 2
    assert caplog.text.count("call_summary call_id=room-a ") == 1
    for leg in sip_call.legs:
        leg.close.assert_awaited_once()


async def test_cancelled_entrypoint_closes_active_media_and_rooms(sip_call):
    task = asyncio.create_task(agent.entrypoint(sip_call.ctx))
    await asyncio.wait_for(sip_call.started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    for leg in sip_call.legs:
        leg.close.assert_awaited_once()
    assert sip_call.ctx.api.room.delete_room.await_count == 2
    sip_call.ctx.shutdown.assert_called_once()


async def test_stt_failure_stops_both_directions_and_cleans_rooms(monkeypatch, sip_call):
    async def transcribe(self):
        raise ConnectionError("private vendor URL")

    monkeypatch.setattr(agent.Leg, "transcribe", transcribe)
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(agent.entrypoint(sip_call.ctx), 1)
    for leg in sip_call.legs:
        leg.close.assert_awaited_once()
    assert sip_call.ctx.api.room.delete_room.await_count == 2


async def test_slow_task_cancellation_does_not_hold_room_cleanup(monkeypatch, sip_call, caplog):
    monkeypatch.setattr(agent, "CLEANUP_TIMEOUT", 0.01)
    started, cancelled, release, stopped = (asyncio.Event() for _ in range(4))

    async def pipeline(*args):
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        finally:
            stopped.set()

    monkeypatch.setattr(agent, "run_call", pipeline)
    task = asyncio.create_task(agent.entrypoint(sip_call.ctx))
    await asyncio.wait_for(started.wait(), 1)
    sip_call.ctx.room.emit("participant_disconnected", sip_call.caller)
    try:
        await asyncio.wait_for(task, 1)
        assert cancelled.is_set() and not stopped.is_set()
        assert sip_call.ctx.api.room.delete_room.await_count == 2
        for leg in sip_call.legs:
            leg.close.assert_awaited_once()
        assert "call_task_cleanup_timeout" in caplog.text
    finally:
        release.set()
        await asyncio.wait_for(stopped.wait(), 1)


@pytest.mark.parametrize("times_out", [False, True])
async def test_worker_drain_rejects_new_jobs_and_waits_for_active_job(monkeypatch, times_out):
    worker = agent.server
    monkeypatch.setattr(worker, "_draining", False)
    monkeypatch.setattr(worker, "_lock", asyncio.Lock())
    monkeypatch.setattr(worker, "_job_lifecycle_tasks", set(), raising=False)
    monkeypatch.setattr(worker, "_update_worker_status", AsyncMock())
    joined, release = asyncio.Event(), asyncio.Event()
    process = SimpleNamespace(running_job=object())

    async def join():
        joined.set()
        await release.wait()
        process.running_job = None

    process.join = join
    monkeypatch.setattr(worker, "_proc_pool", SimpleNamespace(processes=[process]), raising=False)
    assert worker._drain_timeout == 30 and worker._shutdown_process_timeout == 30
    task = asyncio.create_task(worker.drain(timeout=0.01 if times_out else 1))
    await asyncio.wait_for(joined.wait(), 1)
    assert worker.draining and not worker._is_available()
    assert not task.done()
    if times_out:
        with pytest.raises(TimeoutError):
            await task
        assert process.running_job is not None
    else:
        release.set()
        await task
        assert process.running_job is None
    worker._update_worker_status.assert_awaited_once()
