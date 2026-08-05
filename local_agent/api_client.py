"""HTTP client for the hosted Django CRM.

Replaces the agent's old direct-Postgres access entirely. Database credentials
never reach a team member's laptop; the only secret here is a revocable token.
"""

import httpx

from local_agent.config import settings


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
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            raise ApiError(f"Cannot reach the CRM at {self.base_url}: {exc}") from exc

        if response.status_code >= 400:
            try:
                detail = response.json().get("error", response.text)
            except ValueError:
                detail = response.text[:300]
            raise ApiError(detail, status=response.status_code)

        return response.json()

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

    def preflight(self, campaign_id: str, contact_ids: list[str]):
        return self._request(
            "POST", "/mailings/preflight",
            json={"campaign_id": campaign_id, "contact_ids": contact_ids},
        )

    def claim(self, campaign_id: str, contact_ids: list[str]):
        """Reserve mailings. Each returned item already has a durable DRAFT row."""
        return self._request(
            "POST", "/mailings/claim",
            json={"campaign_id": campaign_id, "contact_ids": contact_ids},
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
