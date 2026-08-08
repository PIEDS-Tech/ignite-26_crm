"""Ignite CRM local sending agent.

Runs on an individual team member's laptop. Talks to the hosted Django CRM over
HTTPS and sends through that member's own Gmail. It holds no database
credentials -- only a revocable API token and a Gmail OAuth token.
"""

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.requests import Request

from local_agent.api_client import ApiError, CrmClient
from local_agent.config import settings
from local_agent.gmail.client import GmailAuthError, GmailClient
from local_agent.services import send as send_svc

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

state: dict = {"api": None, "gmail": None, "profile": None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Two independent identity proofs before a single mail can be attributed.

    The API token says who the CRM thinks we are. The Gmail OAuth session says
    which mailbox we can actually send from. If those disagree, `sent_by` would
    be a lie, so we refuse to start.
    """
    api = CrmClient()
    profile = api.me()

    configured = settings.member_email.lower()
    if configured and profile["bits_email"].lower() != configured:
        raise RuntimeError(
            f"API token belongs to {profile['bits_email']!r} but "
            f"AGENT_MEMBER_EMAIL is {configured!r}. Fix .env before sending."
        )

    state["api"] = api
    state["profile"] = profile
    state["gmail"] = GmailClient(profile["bits_email"])
    try:
        yield
    finally:
        api.close()


app = FastAPI(title="Ignite CRM — Local Agent", lifespan=lifespan)

# Same vendored Basecoat files the Django CRM serves, so both look identical.
app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")


def api() -> CrmClient:
    return state["api"]


def gmail() -> GmailClient:
    return state["gmail"]


def _guard(fn, *args, **kwargs):
    """Translate client/auth errors into clean HTTP responses."""
    try:
        return fn(*args, **kwargs)
    except GmailAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except ApiError as exc:
        raise HTTPException(status_code=exc.status or 502, detail=str(exc))


# --------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok", "crm": settings.api_base_url, "member": state["profile"]}


@app.post("/auth/verify")
def auth_verify():
    """Trigger the Gmail consent flow if needed and confirm the account matches."""
    return {"authenticated_as": _guard(gmail().verify_identity)}


@app.get("/api/campaigns")
def campaigns():
    return _guard(api().campaigns)


@app.get("/api/contacts")
def contacts(campaign_id: str | None = None):
    return _guard(api().contacts, campaign_id)


@app.get("/api/me")
def me():
    """Live profile, including today's remaining quota.

    Polled by the UI rather than read from `state['profile']`: that snapshot is
    taken once at startup and would otherwise show a stale send count all day.
    """
    return _guard(api().me)


@app.post("/api/contacts")
def create_contact(payload: dict):
    """Add a contact. The server assigns it to us -- we cannot claim someone else's."""
    return _guard(api().create_contact, payload)


@app.patch("/api/contacts/{contact_id}")
def update_contact(contact_id: str, payload: dict):
    """Fix a detail before it gets rendered into a mail.

    The server re-checks that this contact is ours; a 403 here means the contact
    was reassigned while the page was open.
    """
    return _guard(api().update_contact, contact_id, payload)


class SendRequest(BaseModel):
    campaign_id: str
    contact_ids: list[str]


@app.post("/api/preflight")
def preflight(payload: SendRequest):
    return _guard(api().preflight, payload.campaign_id, payload.contact_ids)


@app.post("/api/send")
def send(payload: SendRequest):
    _guard(gmail().verify_identity)

    def stream():
        try:
            for outcome in send_svc.send_batch(
                api(), gmail(), payload.campaign_id, payload.contact_ids
            ):
                yield json.dumps(outcome.dict()) + "\n"
        except ApiError as exc:
            yield json.dumps({"contact_id": "", "email": "", "name": "",
                              "status": "FAILED", "detail": str(exc)}) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@app.get("/api/drafts")
def drafts():
    return _guard(api().drafts)


@app.post("/api/reconcile")
def reconcile():
    _guard(gmail().verify_identity)
    return [o.dict() for o in _guard(send_svc.reconcile, api(), gmail())]


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request, "index.html",
        {"profile": state["profile"], "crm_url": settings.api_base_url},
    )
