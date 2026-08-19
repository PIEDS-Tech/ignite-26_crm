"""HTTP client for the hosted Django CRM.

Replaces the agent's old direct-Postgres access entirely. Database credentials
never reach a team member's laptop; the only secret here is a revocable token.
"""

import time

import httpx

from local_agent.config import settings

#: A dropped connection is the normal condition of a laptop on campus wifi, not
#: an exceptional one. Retrying transient failures here is what stops a blip
#: mid-batch turning into stranded DRAFT rows -- see CLAIM_CHUNK in
#: services/send.py for the other half of that story.
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.5


class ApiError(RuntimeError):
    def __init__(self, message, status=None):
        self.status = status
        super().__init__(message)


class CrmClient:
    def __init__(self, base_url: str | None = None, token: str | None = None):
        self.base_url = (base_url or settings.api_base_url or "").rstrip("/")
        self.token = token or settings.api_token

        if not self.base_url:
            raise ApiError("AGENT_API_BASE_URL is not set. Copy .env.example to .env.")
        if not self.token:
            raise ApiError(
                "AGENT_API_TOKEN is not set. Ask a lead to issue one on the "
                "Team & Tokens page of the CRM."
            )

        self._client = httpx.Client(
            base_url=f"{self.base_url}/api/v1",
            headers={"Authorization": f"Token {self.token}"},
            # Generous read timeout: claim locks and renders a whole batch
            # server-side before responding.
            timeout=httpx.Timeout(10.0, read=60.0),
            follow_redirects=False,
        )

    def close(self):
        self._client.close()

    def _request(self, method: str, path: str, **kwargs):
        """One call to the CRM, retried through a flaky connection.

        Only transport errors and 5xx are retried. A 4xx is the server's
        considered answer -- "not assigned to you", "already has a mailing" --
        and repeating the request would neither change it nor be safe: several
        of these endpoints write.

        Reporting a result is the one call that MUST get through. If it does
        not, a mail that has already left sits as a DRAFT and someone has to
        reconcile it against Gmail by hand, so it is worth three attempts.
        """
        last_error = None

        for attempt in range(MAX_RETRIES):
            try:
                response = self._client.request(method, path, **kwargs)
            except httpx.RequestError as exc:
                # Connection reset, DNS blip, laptop lid, a hotel captive portal.
                # WinError 10053 arrives here.
                last_error = exc
            else:
                if response.status_code < 500:
                    if response.status_code >= 400:
                        try:
                            detail = response.json().get("error", response.text)
                        except ValueError:
                            detail = response.text[:300]
                        raise ApiError(detail, status=response.status_code)
                    return response.json()

                last_error = ApiError(response.text[:300], status=response.status_code)

            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))

        raise ApiError(
            f"Cannot reach the CRM at {self.base_url} after {MAX_RETRIES} attempts: "
            f"{last_error}"
        ) from (last_error if isinstance(last_error, Exception) else None)

    # ---- read ----------------------------------------------------------

    def me(self):
        return self._request("GET", "/me")

    def campaigns(self):
        return self._request("GET", "/campaigns")

    def contacts(self, campaign_id: str | None = None):
        params = {"campaign_id": campaign_id} if campaign_id else None
        return self._request("GET", "/contacts", params=params)

    def drafts(self):
        return self._request("GET", "/mailings/drafts")

    # ---- write ---------------------------------------------------------

    def preflight(self, campaign_id: str, contact_ids: list[str], cc: str = "", bcc: str = ""):
        return self._request(
            "POST", "/mailings/preflight",
            json={"campaign_id": campaign_id, "contact_ids": contact_ids,
                  "cc": cc, "bcc": bcc},
        )

    def claim(self, campaign_id: str, contact_ids: list[str], cc: str = "", bcc: str = ""):
        """Reserve mailings. Each returned item already has a durable DRAFT row.

        CC/BCC are sent for the server to validate and record, not applied here:
        the laptop must not be able to copy an address the CRM has no note of.
        """
        return self._request(
            "POST", "/mailings/claim",
            json={"campaign_id": campaign_id, "contact_ids": contact_ids,
                  "cc": cc, "bcc": bcc},
        )

    def report_sent(self, mailing_id: str, message_id: str, thread_id: str):
        return self._request(
            "POST", f"/mailings/{mailing_id}/result",
            json={"status": "sent", "message_id": message_id, "thread_id": thread_id},
        )

    def report_failed(self, mailing_id: str, error: str):
        return self._request(
            "POST", f"/mailings/{mailing_id}/result",
            json={"status": "failed", "error": error[:2000]},
        )

    # ---- scheduled sends -------------------------------------------------
    # The agent proposes; the server disposes. It validates the time and the
    # addresses, owns the queue, and hands work back only when it is due.

    def schedules(self, status="open"):
        return self._request("GET", "/schedules", params={"status": status})

    def create_schedule(self, campaign_id, contact_ids, scheduled_at, cc="", bcc="",
                        batch_size=0, interval_minutes=0):
        """`scheduled_at` must be ISO 8601 WITH an offset -- the server stores
        UTC and the UI speaks IST, so a naive string would be a guess."""
        return self._request("POST", "/schedules", json={
            "campaign_id": campaign_id,
            "contact_ids": contact_ids,
            "scheduled_at": scheduled_at,
            "cc": cc,
            "bcc": bcc,
            "batch_size": batch_size,
            "interval_minutes": interval_minutes,
        })

    def claim_schedules(self, agent_id="", limit=5):
        """Lease whatever is due for us. Also sweeps stale leases server-side."""
        return self._request("POST", "/schedules/claim",
                             json={"agent_id": agent_id, "limit": limit})

    def report_schedule_progress(self, schedule_id, *, attempted, sent, skipped, error=""):
        return self._request("POST", f"/schedules/{schedule_id}/progress", json={
            "attempted": attempted, "sent": sent, "skipped": skipped, "error": error[:2000],
        })

    def report_schedule_failed(self, schedule_id, error):
        return self._request("POST", f"/schedules/{schedule_id}/progress",
                             json={"failed": True, "error": str(error)[:2000]})

    def cancel_schedule(self, schedule_id):
        return self._request("POST", f"/schedules/{schedule_id}/cancel", json={})

    def reschedule(self, schedule_id, scheduled_at):
        return self._request("POST", f"/schedules/{schedule_id}/reschedule",
                             json={"scheduled_at": scheduled_at})

    # ---- follow-ups ------------------------------------------------------

    def reply_scan(self):
        """Threads the server wants re-read for a reply."""
        return self._request("GET", "/replies/scan")

    def report_reply(self, mailing_id, replied: bool):
        return self._request("POST", f"/replies/{mailing_id}", json={"replied": replied})

    # ---- contact editing -----------------------------------------------
    # The server applies the same permission rule as the web CRM: a member may
    # only change contacts assigned to them. We do not check it here as well --
    # a check on the laptop protects nobody.

    def create_contact(self, data: dict):
        return self._request("POST", "/contacts/new", json=data)

    def update_contact(self, contact_id: str, data: dict):
        return self._request("PATCH", f"/contacts/{contact_id}", json=data)
