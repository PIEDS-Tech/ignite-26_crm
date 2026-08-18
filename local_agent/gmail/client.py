"""Per-member Gmail OAuth and sending.

Each team member authenticates once with their own institute Google account.
Mail therefore leaves a real human mailbox: replies land in their inbox and
deliverability is not that of a bulk sender.
"""

import base64
import json
import os
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from local_agent.config import settings

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]


class GmailAuthError(RuntimeError):
    pass


@dataclass
class SendResult:
    message_id: str
    thread_id: str


def _token_path(email: str) -> Path:
    safe = email.replace("@", "_at_").replace("/", "_")
    return settings.token_dir / f"token_{safe}.json"


def _load_credentials(email: str) -> Credentials:
    settings.token_dir.mkdir(parents=True, exist_ok=True)
    path = _token_path(email)

    creds = None
    if path.exists():
        creds = Credentials.from_authorized_user_info(json.loads(path.read_text()), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _write_token(path, creds)
            return creds
        except Exception:
            # Refresh token revoked or scopes changed -- fall through to a
            # fresh consent rather than dying.
            pass

    if not settings.client_secrets_path.exists():
        raise GmailAuthError(
            f"OAuth client secrets not found at {settings.client_secrets_path}. "
            "Download a Desktop-app OAuth client JSON from Google Cloud Console "
            "and set GOOGLE_CLIENT_SECRETS_PATH."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(settings.client_secrets_path), SCOPES)
    try:
        # port=0 asks the OS for any free port. A fixed port (this used to be
        # 8080) fails with "Address already in use" whenever anything else on
        # the laptop happens to hold it -- including a previous consent attempt
        # that was abandoned mid-flow. Desktop OAuth clients accept any
        # localhost port, so there is nothing to configure in Google Cloud.
        creds = flow.run_local_server(port=0, prompt="consent")
    except OSError as exc:
        # Otherwise this escapes as a bare OSError and the UI shows a 500 with
        # no indication that the problem is a socket, not Google.
        raise GmailAuthError(
            f"Could not start the local browser handshake: {exc}. "
            "Close whatever is holding the port, or retry."
        ) from exc
    _write_token(path, creds)
    return creds


def _write_token(path: Path, creds: Credentials) -> None:
    path.write_text(creds.to_json())
    os.chmod(path, 0o600)   # tokens are live credentials; keep them owner-only


class GmailClient:
    def __init__(self, member_email: str):
        self.member_email = member_email
        self._service = None
        self._authenticated_email: str | None = None

    @property
    def service(self):
        if self._service is None:
            creds = _load_credentials(self.member_email)
            self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._service

    def authenticated_email(self) -> str:
        """The address Google says we are actually signed in as."""
        if self._authenticated_email is None:
            profile = self.service.users().getProfile(userId="me").execute()
            self._authenticated_email = profile["emailAddress"].lower()
        return self._authenticated_email

    def verify_identity(self) -> str:
        """Refuse to run if the Gmail account isn't the configured member.

        `campaign_mailings.sent_by` is only trustworthy because of this check --
        without it the agent could record mail as sent by anyone.
        """
        actual = self.authenticated_email()
        expected = self.member_email.lower()
        if actual != expected:
            raise GmailAuthError(
                f"Authenticated Gmail account is {actual!r} but AGENT_MEMBER_EMAIL "
                f"is {expected!r}. Sign in with the correct account, or delete "
                f"{_token_path(self.member_email)} and re-authenticate."
            )
        return actual

    def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        body_html: str = "",
        from_name: str = "",
        cc: str = "",
        bcc: str = "",
    ) -> SendResult:
        message = EmailMessage()
        message["To"] = to

        # `formataddr`, not an f-string: it quotes a name containing a comma or
        # a period and RFC 2047-encodes a non-ASCII one. Hand-built From headers
        # are how mail ends up displaying as `"Rao"" <addr>` in some clients.
        # An empty name yields the bare address, unchanged from before.
        message["From"] = formataddr((from_name, self.member_email)) if from_name \
            else self.member_email
        message["Subject"] = subject

        # Both are plain headers. Gmail strips Bcc before delivery, so the
        # recipients never learn about each other.
        if cc:
            message["Cc"] = cc
        if bcc:
            message["Bcc"] = bcc

        # Plain text first, then the HTML alternative. That ordering is what
        # multipart/alternative means -- last part wins in clients that render
        # HTML, and the text part is the fallback for those that do not. The
        # server built both from the same body (see services/richtext.py), so
        # they always say the same thing.
        message.set_content(body)
        if body_html:
            message.add_alternative(body_html, subtype="html")

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        sent = self.service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return SendResult(message_id=sent["id"], thread_id=sent["threadId"])

    def thread_has_reply_from(self, thread_id: str, address: str) -> bool:
        """Did `address` write back in this thread?

        Evidence, not inference: we look for a message in the thread whose From
        is the prospect. That rules out our own follow-ups, anyone we CC'd, and
        Gmail's own "message blocked" notices, all of which land in the same
        thread and none of which mean the prospect replied.

        A thread that has vanished (deleted, or the id is stale) reads as "no
        reply" rather than raising: the caller is a background scan, and one
        bad row must not stop it looking at the rest.
        """
        try:
            thread = (
                self.service.users().threads()
                .get(userId="me", id=thread_id, format="metadata",
                     metadataHeaders=["From"])
                .execute()
            )
        except Exception:                              # noqa: BLE001
            return False

        wanted = (address or "").strip().lower()
        if not wanted:
            return False

        for message in thread.get("messages", []):
            headers = message.get("payload", {}).get("headers", [])
            sender = next(
                (h.get("value", "") for h in headers if h.get("name", "").lower() == "from"),
                "",
            )
            # The header is "Name <addr>", so a substring test on the bare
            # address is the reliable read.
            if wanted in sender.lower():
                return True
        return False

    def find_message_to(self, address: str, subject: str) -> SendResult | None:
        """Used by /reconcile to resolve a stranded DRAFT row.

        Answers "did this mail actually go out before we crashed?"
        """
        query = f'in:sent to:{address} subject:"{subject}"'
        resp = (
            self.service.users()
            .messages()
            .list(userId="me", q=query, maxResults=1)
            .execute()
        )
        messages = resp.get("messages", [])
        if not messages:
            return None
        return SendResult(message_id=messages[0]["id"], thread_id=messages[0]["threadId"])
