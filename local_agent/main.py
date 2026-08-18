"""Ignite CRM local sending agent.

Runs on an individual team member's laptop. Talks to the hosted Django CRM over
HTTPS and sends through that member's own Gmail. It holds no database
credentials -- only a revocable API token and a Gmail OAuth token.
"""

import asyncio
import json
import logging
import threading
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
from local_agent.services import schedule_runner
from local_agent.services import send as send_svc

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

state: dict = {"api": None, "gmail": None, "profile": None}

#: Held for the length of a batch. The scheduler and a human pressing Send both
#: pace themselves between mails; interleaving the two would break that pacing
#: and walk the mailbox into Gmail's rate limit, which throttles it for hours.
#:
#: threading, not asyncio: /api/send is a plain `def` streaming response and so
#: runs in Starlette's threadpool, where an asyncio lock cannot be acquired.
send_lock = threading.Lock()

#: Set on shutdown so the poller stops between ticks instead of being killed
#: mid-send.
stop_polling = asyncio.Event()


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

    # The scheduler. Gmail has no sendAt, so a scheduled mail only goes out
    # because this loop is awake to send it -- which is why an always-on agent
    # is the difference between "9am" meaning 9am and meaning "whenever someone
    # next opens their laptop". See docs/MAIL_SCHEDULING.md.
    stop_polling.clear()
    poller = asyncio.create_task(
        schedule_runner.loop(api_ref, gmail_ref, send_lock, stop_polling)
    )

    try:
        yield
    finally:
        stop_polling.set()
        poller.cancel()
        try:
            await poller
        except (asyncio.CancelledError, Exception):   # noqa: BLE001
            pass
        api.close()


app = FastAPI(title="Ignite CRM — Local Agent", lifespan=lifespan)

# Same vendored Basecoat files the Django CRM serves, so both look identical.
app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")


def api() -> CrmClient:
    return state["api"]


def gmail() -> GmailClient:
    return state["gmail"]


# Passed to the poller so it reads `state` at tick time rather than capturing a
# client that a re-authentication could have replaced underneath it.
def api_ref() -> CrmClient:
    return state["api"]


def gmail_ref() -> GmailClient:
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
    #: Comma-separated, applied to every mail in the batch. Validated and
    #: recorded by the server, which is the only thing that makes them real.
    cc: str = ""
    bcc: str = ""


@app.post("/api/preflight")
def preflight(payload: SendRequest):
    return _guard(
        api().preflight, payload.campaign_id, payload.contact_ids,
        payload.cc, payload.bcc,
    )


@app.post("/api/send")
def send(payload: SendRequest):
    _guard(gmail().verify_identity)

    def stream():
        # Held for the whole batch so a scheduled job cannot start sending
        # through the same mailbox halfway down this list. try/finally rather
        # than `with` around the yields alone: a browser that navigates away
        # closes the generator, and the lock must not go with it.
        acquired = send_lock.acquire(timeout=120)
        if not acquired:
            yield json.dumps({"contact_id": "", "email": "", "name": "", "status": "FAILED",
                              "detail": "a scheduled send is using the mailbox; try again"}) + "\n"
            return
        try:
            for outcome in send_svc.send_batch(
                api(), gmail(), payload.campaign_id, payload.contact_ids,
                cc=payload.cc, bcc=payload.bcc,
            ):
                yield json.dumps(outcome.dict()) + "\n"
        except ApiError as exc:
            yield json.dumps({"contact_id": "", "email": "", "name": "",
                              "status": "FAILED", "detail": str(exc)}) + "\n"
        finally:
            send_lock.release()

    return StreamingResponse(stream(), media_type="application/x-ndjson")


# ------------------------------------------------------------------ schedules

class ScheduleRequest(SendRequest):
    #: ISO 8601 WITH an offset. The browser builds it from a local date+time,
    #: so the offset is what stops "09:00" meaning 09:00 UTC on the server.
    scheduled_at: str


@app.get("/api/schedules")
def list_schedules(status: str = "open"):
    return _guard(api().schedules, status)


@app.post("/api/schedules")
def create_schedule(payload: ScheduleRequest):
    """Queue a send. The server validates the time and the addresses."""
    return _guard(
        api().create_schedule, payload.campaign_id, payload.contact_ids,
        payload.scheduled_at, payload.cc, payload.bcc,
    )


@app.post("/api/schedules/{schedule_id}/cancel")
def cancel_schedule(schedule_id: str):
    return _guard(api().cancel_schedule, schedule_id)


class RescheduleRequest(BaseModel):
    scheduled_at: str


@app.post("/api/schedules/{schedule_id}/reschedule")
def move_schedule(schedule_id: str, payload: RescheduleRequest):
    return _guard(api().reschedule, schedule_id, payload.scheduled_at)


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
