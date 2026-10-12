def lookup(caller: str, parties: dict[str, str]) -> dict | None:
    if len(parties) != 2:
        raise ValueError("Exactly two distinct phone numbers are required")
    if caller not in parties:
        return None
    other_no = next(number for number in parties if number != caller)
    return {
        "caller_lang": parties[caller],
        "other_no": other_no,
        "other_lang": parties[other_no],
    }
