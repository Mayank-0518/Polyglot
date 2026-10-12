import os
import re
import secrets

from fastapi import Depends, FastAPI, Header, HTTPException

from app.routing import lookup

SUPPORTED_LANGUAGES = {
    "hi-IN", "kn-IN", "ta-IN", "te-IN", "ml-IN", "mr-IN",
    "bn-IN", "gu-IN", "pa-IN", "od-IN", "en-IN",
}


def create_app(parties: dict[str, str], api_key: str) -> FastAPI:
    if len(parties) != 2:
        raise ValueError("Exactly two distinct phone numbers are required")
    if any(re.fullmatch(r"\+\d{10,15}", number, re.ASCII) is None for number in parties):
        raise ValueError("Phone numbers must use E.164 format")
    if any(language not in SUPPORTED_LANGUAGES for language in parties.values()):
        raise ValueError("Unsupported language")
    if len(set(parties.values())) != 2:
        raise ValueError("The two phones must use different languages")
    if not api_key.strip():
        raise ValueError("API_KEY must not be empty")

    parties = parties.copy()
    expected_key = api_key.encode("utf-8")
    app = FastAPI()

    def authenticate(x_api_key: str | None = Header(default=None)) -> None:
        if x_api_key is None or not secrets.compare_digest(x_api_key.encode("utf-8"), expected_key):
            raise HTTPException(status_code=401, detail="Invalid API key")

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/lookup", dependencies=[Depends(authenticate)])
    def lookup_caller(caller: str):
        result = lookup(caller, parties)
        if result is None:
            raise HTTPException(status_code=404, detail="Unknown caller")
        return result

    return app


app = create_app(
    parties={
        os.getenv("PARTY_A_NO", ""): os.getenv("PARTY_A_LANG", ""),
        os.getenv("PARTY_B_NO", ""): os.getenv("PARTY_B_LANG", ""),
    },
    api_key=os.getenv("API_KEY", ""),
)
