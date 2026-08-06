# Ignite CRM — PIEDS Mass Mailing System

Two apps, one database, one visual language:

- **`core_django/`** — the CRM, hosted on DigitalOcean. Owns the schema, the
  contact pool, campaigns, assignment, and every safety rule. **Never sends mail.**
- **`local_agent/`** — a small FastAPI app each team member runs on their laptop.
  Talks to the CRM over HTTPS and sends through *their own* Gmail. Holds no
  database credentials.

```
   laptop                            DigitalOcean
┌───────────────┐                ┌──────────────────┐
│ FastAPI agent │──HTTPS/Token──→ │ Django CRM       │──→ Postgres
│  + Gmail OAuth│                │  claim / report  │
└───────┬───────┘                └──────────────────┘
        └──→ Gmail API (the member's own credentials, local only)
```

## The one guarantee

`campaign_mailings` has `UNIQUE(campaign_id, contact_id)`. A prospect cannot be
mailed twice for the same campaign — no matter how many times someone presses
send, how many members race, or where a crash lands. Everything else is
convenience.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # then edit it

docker compose up -d          # Postgres on :5432
cd core_django
../.venv/bin/python manage.py migrate
../.venv/bin/python manage.py seed_dev      # dev data; password: devpassword
../.venv/bin/python manage.py createsuperuser
```

Tests (needs Postgres up):

```bash
.venv/bin/python -m pytest
```

## Running the CRM

```bash
cd core_django && ../.venv/bin/python manage.py runserver
```

# 3. frontend agent — separate terminal, from the repo root
.venv/bin/uvicorn local_agent.main:app --port 8111

| Screen | Who |
|---|---|
| `/` dashboard — pool size, per-campaign funnel, recent failures | any member |
| `/contacts/` searchable list · `/contacts/<id>/` history + notes | any member |
| `/campaigns/` list · detail with live preview and funnel | any member |
| `/assign/` bulk-assign contacts to members | **leads (batch 2024)** |
| `/contacts/import/` CSV upload → preview → commit | **leads** |
| `/campaigns/new` template editor with placeholder validation | **leads** |
| `/members/` team load + issue/revoke API tokens | **leads** |

Who counts as a lead is defined once, in `shared/enums.py::LEAD_BATCH`.

## Running the local agent

One-time setup per member:

1. Google Cloud Console → enable the **Gmail API** → Credentials → Create OAuth
   client ID → **Desktop app** → download the JSON as `client_secret.json` at
   the repo root (gitignored).
2. Ask a lead to issue you a token on `/members/`. It is shown **once**.
3. Fill in `.env`: `AGENT_API_BASE_URL`, `AGENT_API_TOKEN`, `AGENT_MEMBER_EMAIL`.

```bash
.venv/bin/uvicorn local_agent.main:app --port 8111
```

Open <http://localhost:8111>. Pick a campaign → **Verify Gmail** (opens the
consent screen once) → select contacts → **Preflight** → **Send**.

Preflight writes nothing. It labels each contact `OK`, `ALREADY_MAILED`,
`NOT_ASSIGNED` or `MISSING_VARS` and shows the fully rendered first mail.

**Two identity proofs before any mail is attributed.** The API token says who
the CRM thinks you are; the Gmail session says which mailbox you can send from.
If they disagree the agent refuses to start — that is what makes `sent_by`
trustworthy.

## How a send is ordered, and why

Server side, `crm/services/mailing.py::claim_batch`, one transaction per contact:

1. `select_for_update()` the contact, check it is assigned to the caller.
2. Render the template. A blank required field skips *that* contact — we never
   mail `Hi ,`.
3. `INSERT` the mailing as `DRAFT` and **commit before the agent is told.** The
   unique constraint fires here for a duplicate.
4. The agent sends via Gmail with **no locks held** — a lock across a
   multi-second round trip would serialize the whole team.
5. `POST /mailings/<id>/result` records `SENT` + thread id, or `FAILED` + error.

A crash between 3 and 5 leaves a `DRAFT` — visible, and resolvable with **Resolve
stranded drafts**, which asks Gmail whether the mail actually went out. The
reverse ordering (send first, record after) could lose the record entirely,
which is unrecoverable.

Retry **updates** the existing row. Inserting a second one is impossible, which
is precisely why retrying is safe.

## API

All endpoints take `Authorization: Token <key>` and live under `/api/v1/`.

| Endpoint | Purpose |
|---|---|
| `GET /me` | identity handshake |
| `GET /campaigns` · `GET /contacts?campaign_id=` | what can be sent, to whom |
| `POST /mailings/preflight` | dry run, writes nothing |
| `POST /mailings/claim` | reserve DRAFTs, returns rendered mail |
| `POST /mailings/<id>/result` | record sent/failed |
| `GET /mailings/drafts` | stranded DRAFTs to reconcile |

Only token hashes are stored. A leaked database yields no working credentials.

## UI

Both apps share one vendored copy of [Basecoat](https://basecoatui.com) — a
Tailwind port of shadcn/ui that works in plain HTML, so Django templates and the
agent's Jinja2 templates render identically. No npm, no build step, no node on
the server.

`shared/static/basecoat/` holds `basecoat.css` (components), `basecoat.js`
(dialog, select, dropdown, toast, tabs) and `app.css` — a small hand-written
layout layer, because Basecoat ships components but deliberately no Tailwind
utilities. See `VENDORED.md` there for provenance.

## Deploying to DigitalOcean

```bash
docker build -f core_django/Dockerfile -t ignite-crm .   # build from the repo root
```

Set as app-level env vars: `DATABASE_URL` (managed Postgres), `DJANGO_SECRET_KEY`,
`DJANGO_ALLOWED_HOSTS`, and `DJANGO_DEBUG=False`. With `DEBUG=False` the app
turns on SSL redirect, HSTS, secure cookies and manifest static storage
automatically. Run `manage.py migrate` as the release step.

Then point each member's `AGENT_API_BASE_URL` at the public HTTPS host.

## Schema changes

Django owns the schema — it is the only thing that migrates.

```bash
cd core_django && ../.venv/bin/python manage.py makemigrations && ../.venv/bin/python manage.py migrate
```

The agent has no models to keep in sync; it only speaks JSON.

## Layout

```
core_django/crm/
  models.py                  canonical schema — the unique constraint lives here
  services/mailing.py        claim/report — the safety core
  services/render.py         {{ var }} substitution, refuses on blanks
  services/permissions.py    the single definition of "is a lead"
  services/campaigns.py      status transitions + template validation
  services/assignment.py     bulk assign, with the reassign guard
  services/importer.py       CSV parse → validate → commit
  api/                       token auth + the seven JSON endpoints
  templates/crm/             every screen
  tests/                     32 tests, incl. the idempotency proof
local_agent/
  api_client.py              httpx wrapper — the agent's only link to the CRM
  services/send.py           claim → send → report
  gmail/client.py            OAuth, token storage, identity binding
shared/
  enums.py                   status strings + LEAD_BATCH
  static/basecoat/           the shared UI kit
```
