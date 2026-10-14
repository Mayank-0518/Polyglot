import asyncio


async def run_direction(finals, translate, speak, on_error=None):
    async for text in finals:
        if not text.strip():
            continue
        try:
            translated = await translate(text)
        except Exception as exc:
            if on_error is not None:
                on_error(exc)
            continue
        await speak(translated)


async def run_call(a_finals, b_finals, translate_ab, translate_ba, speak_to_a, speak_to_b, on_error=None):
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(run_direction(a_finals, translate_ab, speak_to_b, on_error))
        tasks.create_task(run_direction(b_finals, translate_ba, speak_to_a, on_error))
