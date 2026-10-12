import asyncio

import pytest

from app.pipeline import run_call, run_direction


async def agen(items):
    for i in items:
        yield i


def recorder():
    out = []

    async def speak(text):
        out.append(text)

    return out, speak


async def tr(text):
    return f"T({text})"


async def test_translates_in_order_and_skips_blank():
    spoken, speak = recorder()
    await run_direction(agen(["one", "  ", "", "\t\n", "two"]), tr, speak)
    assert spoken == ["T(one)", "T(two)"]  # never the original text


async def test_translate_failure_skips_sentence_and_continues():
    spoken, speak = recorder()
    errors = []

    async def flaky(text):
        if text == "bad":
            raise RuntimeError("vendor down")
        return f"T({text})"

    await run_direction(agen(["a", "bad", "b"]), flaky, speak, on_error=errors.append)
    assert spoken == ["T(a)", "T(b)"]
    assert len(errors) == 1 and isinstance(errors[0], RuntimeError)


async def test_slow_translation_does_not_reorder():
    spoken, speak = recorder()
    delays = {"first": 0.05, "second": 0.0}

    async def slow(text):
        await asyncio.sleep(delays[text])
        return text

    await run_direction(agen(["first", "second"]), slow, speak)
    assert spoken == ["first", "second"]


async def test_each_side_reaches_only_the_other():
    to_a, say_a = recorder()
    to_b, say_b = recorder()

    async def ab(t):
        return f"hi:{t}"

    async def ba(t):
        return f"od:{t}"

    await run_call(agen(["a1", "a2"]), agen(["b1"]), ab, ba, say_a, say_b)
    assert to_b == ["hi:a1", "hi:a2"]
    assert to_a == ["od:b1"]


async def test_directions_run_concurrently():
    to_a, say_a = recorder()
    to_b, say_b = recorder()
    gate = asyncio.Event()

    async def a_silent_until_gate():
        await gate.wait()
        yield "late"

    task = asyncio.create_task(run_call(a_silent_until_gate(), agen(["b1"]), tr, tr, say_a, say_b))
    await asyncio.sleep(0.05)
    assert to_a == ["T(b1)"]  # B got through while A's direction was idle
    gate.set()
    await asyncio.wait_for(task, 1)
    assert to_b == ["T(late)"]


async def test_translate_failure_without_handler_still_continues():
    spoken, speak = recorder()

    async def flaky(text):
        if text == "bad":
            raise RuntimeError("vendor down")
        return f"T({text})"

    await run_direction(agen(["bad", "good"]), flaky, speak)
    assert spoken == ["T(good)"]


async def test_speaker_failure_is_not_swallowed_as_translation_failure():
    errors = []
    failure = RuntimeError("audio output unavailable")

    async def broken_speaker(text):
        raise failure

    with pytest.raises(RuntimeError) as caught:
        await run_direction(agen(["one"]), tr, broken_speaker, on_error=errors.append)
    assert caught.value is failure
    assert errors == []


async def test_call_failure_cancels_the_other_direction():
    translating = asyncio.Event()
    stopped = asyncio.Event()
    failure = RuntimeError("audio output unavailable")
    spoken, speak = recorder()

    async def blocked_translation(text):
        translating.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    async def broken_speaker(text):
        await translating.wait()
        raise failure

    with pytest.raises(ExceptionGroup) as caught:
        await asyncio.wait_for(
            run_call(agen(["a"]), agen(["b"]), tr, blocked_translation, speak, broken_speaker),
            timeout=1,
        )
    assert caught.value.exceptions == (failure,)
    assert stopped.is_set()
    assert spoken == []
