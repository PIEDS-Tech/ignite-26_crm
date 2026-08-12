# Ignite CRM — PIEDS Mass Mailing System

A shared contact pool and mass-mailing system for PIEDS, BITS Pilani's technology
business incubator. One master database, a hosted CRM the 2024 batch runs, and a
small app each 2025-batch member runs on their own laptop to send from their own
Gmail.

---

## Table of contents

1. [What this is](#1-what-this-is)
2. [Architecture](#2-architecture)
3. [The guarantees](#3-the-guarantees)
4. [Who can do what](#4-who-can-do-what)
5. [Data model](#5-data-model)
6. [Lifecycle and tags](#6-lifecycle-and-tags)
7. [The send protocol](#7-the-send-protocol)
8. [Staying in sync](#8-staying-in-sync)
9. [Screens](#9-screens)
10. [HTTP API](#10-http-api)
11. [Services — where the rules live](#11-services--where-the-rules-live)
12. [Setup](#12-setup)
13. [Running it](#13-running-it)
14. [Supabase — the master database](#14-supabase--the-master-database)
15. [Google setup](#15-google-setup)
16. [Deploying](#16-deploying)
17. [Tests](#17-tests)
18. [Environment variables](#18-environment-variables)
19. [Management commands](#19-management-commands)
20. [File layout](#20-file-layout)
21. [Operational playbook](#21-operational-playbook)
22. [Change log](#22-change-log)
23. [Known gaps](#23-known-gaps)

---

## 1. What this is

Two apps, one database, one visual language:

- **`core_django/`** — the CRM. Owns the schema, the contact pool, campaigns,
  assignment, and **every safety rule**. It is the only process that connects to
  the database. **It never sends mail.**
- **`local_agent/`** — a small FastAPI app each team member runs on their laptop.
  Talks to the CRM over HTTPS and sends through *their own* Gmail. Holds no
  database credentials.

The split exists because of one asymmetry: **neither side can send mail alone.**
The server knows who to mail and enforces every rule, but has no mailbox. The
agent has a mailbox but doesn't know who to mail until the server hands it a
claim. That is the whole security model.

---

## 2. Architecture

```
   member's laptop                          hosted
┌────────────────────────┐        ┌──────────────────────────┐
│ local_agent (FastAPI)  │        │ core_django (Django)     │
│ :8111                  │──────► │ :8000                    │──► Supabase
│                        │ HTTPS  │                          │    Postgres
│ holds: Gmail OAuth     │ +Token │ holds: DB credentials    │    (the master)
│ holds: NO db creds     │        │ holds: NO Gmail creds    │
└───────────┬────────────┘        └──────────────────────────┘
            │
            └──► Gmail API — the member's own mailbox, never the server's
```

**Why the agent is local at all.** Mail sent from a central server would come
from one address and land in spam at volume. Each member sending from their own
BITS address is both more deliverable and more honest — the person whose name is
on the mail is the person who will get the reply.

**Why the agent has no database access.** It used to. Moving the send logic
server-side means the row lock, the unique constraint, and the DRAFT-before-send
ordering all live next to the data they protect. Laptops stopped holding
production credentials, and port 5432 never faces the internet.

---

## 3. The guarantees

### 3.1 A prospect is never mailed twice for the same campaign

```sql
UNIQUE (campaign_id, contact_id)  -- on campaign_mailings
```

Not application logic — a **database constraint**. A double-click, a retry, two
agents racing, or a crash mid-batch all collide there rather than putting a
second copy in someone's inbox. Everything else in this system is convenience.

Verify it exists on any host with `manage.py check_db`.

### 3.2 A sent mail always has a record

The DRAFT row is committed **before** the agent is told to send. A crash can
therefore leave an ambiguous DRAFT — visible and resolvable — but never a sent
mail with no record, which would be unrecoverable.

### 3.3 `sent_by` is trustworthy

The agent refuses to start unless two independent identities agree: the API
token's owner (what the CRM thinks) and the Gmail session (which mailbox you can
actually send from). Nobody can send from their own account and have it
attributed to someone else.

### 3.4 We never mail a half-rendered template

A contact missing a variable the template needs is **skipped**, not mailed with a
gap. Nobody receives "Hi , we loved what you're building at ."

### 3.5 Nobody edits data they don't own

2025-batch members can only change contacts assigned to them. Enforced per-object
in `services/permissions.py` and again in `services/contacts.py` — twice, because
the API accepts JSON, and JSON does not respect a form's field list.

---

## 4. Who can do what

Defined once, in `shared/enums.py::LEAD_BATCH` (currently `"2024"`) and
`services/permissions.py`. Changing which batch leads next year is a one-line edit.

| | batch 2024 (lead) | batch 2025 |
|---|---|---|
| **Signs in by** | **picking their name** | **Google, BITS domain only** |
| See the whole pool | ✅ | ✅ |
| Edit a contact | anyone's | **only their own assigned** |
| Archive / restore | anyone's | only their own |
| Add a contact | assigns to anyone | assigns to themselves, forced |
| Bulk edit | anyone's | only their own |
| Delete permanently | ✅ (never-mailed only) | ❌ |
| Set `lifecycle` by hand | ✅ | ❌ — the server moves it |
| Assign contacts to members | ✅ | ❌ |
| Create / edit campaigns | ✅ | ❌ (read-only) |
| Issue and revoke API tokens | ✅ | ❌ |
| Import CSV | ✅ | ❌ |
| Send mail | ✅ | ✅ |

Both batches edit from **either surface**: full forms in the Django CRM, and an
inline row editor in the local agent for fixing a detail just before sending.

### 4.1 Two doors, one per batch

There is no password anywhere in the CRM. `services/auth.py` holds both doors,
and identity is a session key holding a `TeamMember` id — Django's `User` model
is consulted only by `/admin/`.

**Batch 2024 picks their name from a list.** Every lead runs the CRM on their own
laptop against the shared database, so the only person who can reach that form is
the person holding the machine. A password there protects nothing the laptop's
lock screen doesn't.

**Batch 2025 signs in with Google**, restricted to the BITS hosted domain and
matched against `TeamMember.bits_email`. They must prove that identity anyway —
the sending agent refuses to run unless the Gmail session matches the member — so
this reuses a proof they already have to give rather than inventing a second one.

**The 2025 half is a refusal, not a filter.** The name list only contains leads,
but posting a 2025 member's id directly to `/login/name/` is rejected too. If
anyone could claim a lead identity from a dropdown, every rule in the table above
would be advisory. That is what `test_login.py` pins.

> **This assumes the CRM is reachable only by people you trust** — on localhost,
> or on a network only the team can reach. Do not put the name door on a public
> hostname: it is, deliberately, a list of names and a Continue button.

**Why 2025 members can edit at all.** They are the ones actually in conversation
with their prospects, so they are the first to learn that a designation changed
or a name was misspelt. Making them file a request to a lead guarantees the pool
stays wrong. Scoping it to their own list means a stale row in someone else's
list is still not theirs to touch.

---

## 5. Data model

Everything inherits `TimeStampedModel`: UUID primary key, `created_at`,
`updated_at`. UUIDs rather than sequential integers so a contact id in a URL
leaks nothing about pool size.

### `team_members`
| Field | Type | Notes |
|---|---|---|
| `name` | Char(120) | |
| `bits_email` | Email | unique, validated as a BITS address |
| `phone` | Char(10) | regex-validated, optional |
| `linkedin` | URL | optional |
| `batch` | Char(4) | **indexed — drives all permissions** |
| `is_active` | Bool | |
| `user` | OneToOne → Django `User` | `SET_NULL`; null for members who only run the agent |

### `api_tokens`
| Field | Type | Notes |
|---|---|---|
| `member` | FK → TeamMember | `CASCADE` |
| `label` | Char(80) | e.g. "Aarav's MacBook" |
| `key_hash` | Char(64) | unique, indexed — **SHA-256 only** |
| `key_prefix` | Char(8) | first chars, shown in the UI for identification |
| `last_used_at` | DateTime | stamped on every request, so leads can spot stale tokens |
| `revoked_at` | DateTime | |

The plaintext key is shown **once** at creation and is unrecoverable. A dumped
database yields zero working credentials.

### `contacts`
| Field | Type | Notes |
|---|---|---|
| `first_name` / `last_name` | Char(80) | |
| `email` | Email | **unique — the dedupe key for CSV import** |
| `phone_no` | Char(10) | regex-validated |
| `linkedin` | URL | |
| `company` | Char(160) | indexed |
| `designation` | Char(160) | |
| `assigned_to` | FK → TeamMember | `SET_NULL`, indexed |
| `assigned_at` | DateTime | |
| `last_contacted_by` / `last_contacted_at` | FK / DateTime | written on every send |
| **`lifecycle`** | Char(16) | indexed, **server-owned** — see §6 |
| **`tags`** | Postgres array of Char(40) | GIN-indexed, free-form |
| **`is_archived`** | Bool | indexed |
| **`archived_at` / `archived_by`** | DateTime / FK | |
| **`created_by`** | FK → TeamMember | who added it |

Indexes: `(assigned_to, company)`, `(is_archived, lifecycle)`, GIN on `tags`.

### `contact_notes`
Append-only free text: `contact` (CASCADE), `author`, `body`. A separate table
rather than a column so we keep who wrote what and when.

### `contact_audits`
| Field | Type |
|---|---|
| `contact` | FK, `CASCADE` |
| `actor` | FK → TeamMember, `SET_NULL` |
| `field` / `old_value` / `new_value` | Char(40) / Text / Text |

One row per field changed. Written **only** by `services/contacts.py`, so a new
view cannot mutate a contact without leaving a trace.

### `campaigns`
| Field | Type | Notes |
|---|---|---|
| `title` | Char(200) | |
| `mail_sub` / `mail_body` | Char(300) / Text | support `{{ variable }}` |
| `var_list` | JSON | declared variables, cross-checked against the template |
| `status` | Char(16) | indexed — `draft → active → paused → completed → archived` |
| `created_by` | FK | |

**Only `active` campaigns can be mailed.** Flipping one to `paused` in the CRM
stops every agent on every laptop mid-batch. That is the emergency brake.

### `campaign_mailings` — the unit of idempotency
| Field | Type | Notes |
|---|---|---|
| `campaign` / `contact` / `sent_by` | FK | **all `PROTECT`** |
| `mail_thread_id` / `mail_message_id` | Char(120) | from Gmail |
| `status` | Char(8) | `draft` / `sent` / `failed` |
| `rendered_subject` / `rendered_body` | Text | **snapshot of what actually went out** |
| `error_detail` | Text | |
| `sent_at` | DateTime | |

```
constraints: UNIQUE(campaign, contact)  name="uniq_campaign_contact"
indexes:     (campaign, status), (sent_by, sent_at)
```

Two deliberate choices here:

- **`rendered_*` snapshots.** Campaign templates change. Without these we could
  never answer "what did we actually send this person?"
- **`PROTECT` on `contact`.** A contact that has ever been mailed cannot be
  deleted — that would destroy the record. This is why archiving exists.

---

## 6. Lifecycle and tags

Two separate things, deliberately kept apart.

### `lifecycle` — server-owned funnel state

```
new ──(first confirmed send)──► contacted
                                    │
                       (set by hand by a lead)
                                    ▼
                    replied · bounced · do_not_contact
```

**The only automatic transition is `new → contacted`**, applied in
`services/mailing.py::record_result()` the instant a send is confirmed. This is
what "changes the moment they mail" means, and it happens inside the same
transaction that records the send.

It is scoped to contacts sitting at `new`:

```python
Contact.objects.filter(
    id=mailing.contact_id, lifecycle=ContactLifecycle.NEW.value
).update(lifecycle=ContactLifecycle.CONTACTED.value, updated_at=now)
```

**Why the filter matters.** Without it, a second campaign would silently
overwrite a `replied` that someone set by hand after an actual conversation —
destroying the one piece of information a human added. A later campaign must
never drag a contact backwards.

`replied`, `bounced` and `do_not_contact` stay manual. Inferring "bounced" from
an SMTP error string is guesswork we would later have to un-guess.

### `tags` — free-form labels

`fintech`, `priority`, `iit-b`, `warm-intro`. Lowercased and de-duplicated on the
way in, so `Fintech` and `FINTECH` cannot become two separate filter facets for
the same idea. Editable by a lead or by the assigned owner. Filterable on
`/contacts/` and `/assign/`, and stored as a real Postgres array so
`tags__contains=["fintech"]` uses the GIN index.

### Blocked states actually block

`is_archived`, `do_not_contact` and `bounced` are **refused by `claim_batch`**
under the row lock — not merely hidden from a list:

```python
def unmailable_reason(contact) -> tuple[str, str] | None:
    if contact.is_archived:
        return ARCHIVED, "archived"
    if contact.lifecycle in BLOCKED_LIFECYCLES:
        return BLOCKED, f"marked {contact.get_lifecycle_display().lower()}"
    return None
```

`preflight()` calls the **same helper**, so the dry run cannot disagree with the
real thing about who is sendable. The check runs under the lock rather than
trusting the agent's list, because someone may have archived the contact between
the page loading and Send being pressed.

---

## 7. The send protocol

### The claim → send → report loop

```
1. agent: POST /api/v1/mailings/claim {campaign_id, contact_ids[]}

2. server, ONE TRANSACTION PER CONTACT:
     SELECT ... FOR UPDATE the contact          ← row lock acquired
     assigned to the caller?          no → skip NOT_ASSIGNED
     archived / blocked lifecycle?   yes → skip ARCHIVED | BLOCKED
     render(campaign, contact)      fail → skip MISSING_VARS
     INSERT CampaignMailing(DRAFT)         ← unique constraint fires on a dupe
     COMMIT                                 ← lock released, DRAFT durable
   ────────────────────────────────────────────────────────────────────
3. agent sends via its own Gmail             ← NO locks held anywhere

4. agent: POST /api/v1/mailings/<id>/result
     {status: "sent",   message_id, thread_id}  → SENT  + lifecycle flip
     {status: "failed", error}                  → FAILED + error_detail
```

### Why this exact ordering

- **DRAFT commits before the mail leaves.** The reverse — send first, record
  after — could lose the record of a mail that actually went out. That is
  unrecoverable; an orphaned DRAFT is merely annoying.
- **The lock is released at step 2's commit**, before the multi-second Gmail
  round trip. Holding it across the network would serialize the entire team
  behind one slow send.
- **Retry UPDATEs the existing row.** Inserting a second one is physically
  impossible. That is precisely what makes pressing Send twice safe.
- **One transaction per contact**, not one per batch. A single bad contact
  doesn't roll back the other 199.

### Stranded drafts

If the agent dies between steps 2 and 4, DRAFTs are left behind.
`GET /mailings/drafts` lists your own; **Resolve stranded drafts** uses
`GmailClient.find_message_to()` to ask **Gmail itself** whether that mail went
out, and settles the row to `SENT` or `FAILED` accordingly. It never re-sends
blindly.

### Daily cap

`DAILY_SEND_CAP = 400`, enforced inside `claim_batch` by counting
`sent_last_24h(member)`. Server-side deliberately — it counts across every device
a member uses, so nobody evades it by opening the agent on a second laptop.
Gmail's real per-account quota, once tripped, throttles the whole mailbox for
hours.

### Outcome codes

Shared by the API and both UIs (`services/mailing.py`):

| Code | Meaning |
|---|---|
| `OK` | sendable |
| `ALREADY_MAILED` | a mailing already exists for this (campaign, contact) |
| `NOT_ASSIGNED` | not assigned to the caller |
| `MISSING_VARS` | template needs a field this contact leaves blank |
| `ARCHIVED` | contact is archived |
| `BLOCKED` | lifecycle is `do_not_contact` or `bounced` |
| `CAP_REACHED` | daily cap exhausted |
| `SENT` / `FAILED` | result of the actual send |

---

## 8. Staying in sync

One master database now has the whole team writing to it from two apps, so an
open tab goes stale within seconds of someone else's edit.

Both apps poll every **20 seconds** and on tab focus
(`shared/static/basecoat/poll.js`). Deliberately not websockets: that would mean
a second auth system and a Supabase key in every browser, to save nineteen
seconds.

Behaviours that make it usable rather than annoying:

- **Pauses during a send.** The NDJSON result stream is writing per-row statuses;
  a refresh mid-stream would rebuild the table underneath it.
- **Pauses while an edit dialog is open.** Otherwise a refresh wipes fields
  someone is halfway through typing.
- **Preserves checkbox selections** across a refresh. Losing a 40-contact
  selection to a background poll would make the feature worse than not having it.
- **Stops when the tab is hidden**, and refreshes immediately on focus.
- **Shows "updated 12s ago"**, so a frozen tab looks frozen.
- **Fails silently.** A failed poll logs to console and waits for the next one;
  it never interrupts anyone with an alert.

Event listeners on polled tables are **delegated**, not bound per row — a refresh
replaces the rows and would otherwise take the listeners with them.

---

## 9. Screens

### Django CRM — `http://localhost:8000`

| Route | Screen | Who |
|---|---|---|
| `/` | Dashboard — pool size, per-campaign funnel, recent failures | any member |
| `/contacts/` | Searchable list; filter by company, assignee, **tag, stage, archived**; bulk edit | any member |
| `/contacts/new/` | Add one contact by hand | any member |
| `/contacts/<id>/` | Detail — mail history, notes, **change log** | any member |
| `/contacts/<id>/edit/` | Edit | owner or lead |
| `/contacts/<id>/archive/` | Archive / restore | owner or lead |
| `/contacts/<id>/delete/` | Permanent delete | **lead**, never-mailed only |
| `/contacts/bulk-edit/` | Apply one change to many | any member (scoped) |
| `/contacts/import/` | CSV upload → preview → commit | **lead** |
| `/assign/` | Bulk-assign contacts to members | **lead** |
| `/campaigns/` | List with sent/failed counts | any member |
| `/campaigns/<id>/` | Funnel, live preview, status transitions | any member |
| `/campaigns/new/` · `/campaigns/<id>/edit/` | Template editor with placeholder validation | **lead** |
| `/members/` | Team load, issue/revoke API tokens | **lead** |
| `/login/` | Sign in — name list, or Google | anyone |
| `/admin/` | Django admin | superuser (password auth, separate) |

### Local agent — `http://localhost:8111`

Single page: campaign picker → Verify Gmail → contact table (with tags, stage
badge and a ✎ inline editor per row) → Preflight → Send, plus **Resolve stranded
drafts**.

---

## 10. HTTP API

All endpoints take `Authorization: Token <key>` and live under `/api/v1/`.

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/me` | identity handshake, live quota |
| `GET` | `/campaigns` | active campaigns only |
| `GET` | `/contacts?campaign_id=` | the caller's assigned contacts, archived excluded |
| `POST` | `/contacts/new` | add a contact, force-assigned to the caller |
| `PATCH` | `/contacts/<id>` | edit, guarded by `can_edit_contact` |
| `POST` | `/mailings/preflight` | dry run, **writes nothing** |
| `POST` | `/mailings/claim` | reserve DRAFTs, returns rendered mail |
| `POST` | `/mailings/<id>/result` | record sent/failed |
| `GET` | `/mailings/drafts` | stranded DRAFTs to reconcile |

Contact payload:

```json
{
  "id": "…", "name": "Rohan Iyer", "first_name": "Rohan", "last_name": "Iyer",
  "email": "rohan@example.com", "phone_no": "", "linkedin": "",
  "company": "Zerodha", "designation": "CTO",
  "tags": ["fintech", "priority"],
  "lifecycle": "new", "lifecycle_label": "New",
  "mailable": true, "already_mailed": false, "last_contacted_at": null
}
```

CORS is not configured and is not needed — the agent is a server-side HTTP
client, not a browser.

### Agent's own routes (`:8111`)

`GET /` (the UI), `/health`, `/auth/verify`, `/api/me`, `/api/campaigns`,
`/api/contacts`, `POST /api/contacts`, `PATCH /api/contacts/{id}`,
`/api/preflight`, `/api/send` (NDJSON stream), `/api/drafts`, `/api/reconcile`.
All thin proxies to the CRM except `/auth/verify` and `/api/send`, which touch
Gmail.

---

## 11. Services — where the rules live

Every rule lives in `core_django/crm/services/`. The agent is a dumb pipe with a
mailbox. Change behaviour here and it takes effect for everyone immediately, with
no laptop to update.

| Module | Contents |
|---|---|
| `mailing.py` | `claim_batch`, `record_result`, `preflight`, `unmailable_reason`, `stranded_drafts`, `reset_for_retry`, `sent_last_24h`, `load_sendable_campaign`, `DAILY_SEND_CAP` |
| `contacts.py` | `create`, `update`, `set_archived`, `hard_delete`, `bulk_edit`, `clean_tags`, `lifecycle_counts`, `all_tags`, `EDITABLE_FIELDS`, `LEAD_ONLY_FIELDS` |
| `permissions.py` | `is_lead`, `lead_required`, `member_required`, `can_edit_contact`, `can_set_lifecycle`, `can_hard_delete`, `editable_contacts` |
| `assignment.py` | `bulk_assign` (with the reassign guard), `bulk_unassign` |
| `campaigns.py` | `validate_template`, `transition`, `extract_placeholders`, `ALLOWED_VARIABLES` |
| `importer.py` | `parse` → `ImportPreview`, `commit` |
| `render.py` | `render`, `contact_context`, `MissingVariables` |
| `auth.py` | `login_member`, `current_member`, `name_login_allowed`, `member_from_google_callback`, `SESSION_KEY` |

Two guards worth knowing about:

**The reassign guard** (`bulk_assign`) refuses to move a contact that already has
mailings under another member, unless `force=True`. The existing owner may be
mid-conversation, and silently moving the contact would strand that thread with
nobody watching for the reply. Skipped contacts surface in the UI with a
"reassign anyway" option.

**The delete guard** (`hard_delete`) refuses a contact with mail history. `PROTECT`
would reject it at the database anyway — but as a 500, not as an explanation.

Template variables available: `first_name`, `last_name`, `full_name`, `email`,
`company`, `designation`. `validate_template()` cross-checks every `{{ … }}`
against that set **and** against the campaign's declared `var_list`, so
`{{ compnay }}` fails at save rather than at send.

---

## 12. Setup

### 12.1 Docker — the whole CRM in one command

```bash
git clone <repo> && cd ignite_crm
cp .env.example .env               # leave the agent section blank for now

docker compose up --build
```

`.env.example` ships with `COMPOSE_PROFILES=localdb`, which is what starts the
local Postgres container. Keep that line for local development; drop it when you
move to a hosted database (§14).

That is everything: Postgres 16, migrations, `check_db`, `seed_dev`, and
gunicorn on <http://localhost:8000>. Nothing is installed on the host — no venv,
no Python version to match. See §13.1 for what it actually does and how to add
the agent.

Requires only Docker. If another project already owns port 5432, run
`PG_HOST_PORT=5442 docker compose up --build` — that changes the *host* port
only; nothing inside the stack notices.

### 12.2 Native — for working on the code

```bash
git clone <repo> && cd ignite_crm

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env               # then edit it

docker compose up -d               # Postgres 16 on :5432

cd core_django
../.venv/bin/python manage.py migrate
../.venv/bin/python manage.py check_db          # verify the constraint landed
../.venv/bin/python manage.py seed_dev          # dev data
../.venv/bin/python manage.py createsuperuser   # optional, for /admin/ only
```

`seed_dev` creates four members — `aarav`, `diya` (batch 2024, leads) and
`kabir`, `ishita` (batch 2025) — plus ~50 contacts with assorted tags and
lifecycles, and one active campaign. The two leads can sign in immediately by
picking their name; the 2025 members need Google configured (§15.1).

---

## 13. Running it

### 13.1 Docker

```bash
docker compose up --build          # postgres + CRM   → http://localhost:8000
```

One image (`./Dockerfile`) contains both apps, because they share `shared/` and
the same dependency set. Compose decides which one a container runs by choosing
an entrypoint. **The split is enforced by environment, not by files:** the agent
container is simply never given `DATABASE_URL`.

`docker/entrypoint-crm.sh` runs before gunicorn and, in order: prints the
database it is about to use, blocks on `pg_isready`, migrates *if the database is
local*, then **`check_db` — and refuses to boot if it fails.** A container that
came up without `uniq_campaign_contact` could double-mail a prospect, so not
starting is the correct outcome. `seed_dev` runs last, under the same
local-only rule. Both rules are spelt out just below.

| Variable | Default in compose | |
|---|---|---|
| `COMPOSE_PROFILES` | `localdb` in `.env.example` | runs the local Postgres container; drop it once `DATABASE_URL_DOCKER` points at Supabase |
| `PG_HOST_PORT` | `5432` | host-side Postgres port; change it on a collision |
| `RUN_MIGRATIONS` | *auto* | `true` only if the database is local — see below |
| `SEED_DEV` | *auto* | same rule |
| `DJANGO_DEBUG` | `True` | `False` turns on `SECURE_SSL_REDIRECT`, which bounces plain-http localhost to https |
| `DATABASE_URL_DOCKER` | `…@postgres:5432/…` | point it at Supabase to skip the local Postgres |

#### Migrating and seeding decide themselves

Both default to **whether `DATABASE_URL` names a local database** (`localhost`,
`127.0.0.1`, `::1`, or the `postgres` service), rather than to a flag somebody
has to remember to flip:

| | local Postgres | shared hosted database |
|---|---|---|
| `migrate` | every boot | **skipped** — set `RUN_MIGRATIONS=true` on the one machine that owns the schema |
| `seed_dev` | every boot | **skipped** — and `seed_dev` itself refuses a non-local host without `--force` |
| `check_db` | **always** | **always** |

Skipping migrations is safe precisely because `check_db` is not skipped: a
laptop pointed at a database whose schema was never built exits non-zero with
`MISSING campaign_mailings.uniq_campaign_contact` rather than serving a CRM that
can double-mail. The guard is in `seed_dev.py` as well as the entrypoint,
because someone typing the command by hand deserves the same protection.

`DJANGO_ALLOWED_HOSTS` gets `crm` **appended**, not defaulted — the agent
container reaches the CRM by that hostname and Django validates `Host`, so a
`.env` listing only `localhost` must not be able to drop it.

#### Adding the agent

The agent is behind a profile because it cannot start unprepared: it needs an
API token that only exists once the CRM is up, and a Gmail consent that has to
happen in a real browser.

```bash
# 1. token (the /members/ page is the normal path; this is for bootstrapping)
docker compose exec crm python core_django/manage.py issue_token \
    aarav@pilani.bits-pilani.ac.in --label docker

# 2. paste AGENT_API_TOKEN and AGENT_MEMBER_EMAIL into .env

# 3. Gmail consent — ONCE, on the host, because the OAuth flow opens a browser
#    and binds 127.0.0.1:8080 inside whatever runs it
mkdir -p ~/.ignite_crm && cp client_secret.json ~/.ignite_crm/
.venv/bin/uvicorn local_agent.main:app --port 8111    # press "Verify Gmail", then Ctrl-C

# 4. now the container reuses that cached token
docker compose --profile agent up --build             # → http://localhost:8111
```

`~/.ignite_crm` is mounted at `/tokens`, holding both `client_secret.json` and
the cached `token_*.json`. Step 3 is a one-time cost per member; after it, the
agent is `docker compose --profile agent up` forever.

The agent container's own entrypoint fails fast with an explanation when
`AGENT_API_TOKEN` is empty, rather than letting uvicorn crash-loop on the
lifespan identity check.

### 13.2 Native

Three terminals, from the repo root:

```bash
# 1. database
docker compose up -d postgres

# 2. backend — the CRM                          → http://localhost:8000
cd core_django && ../.venv/bin/python manage.py runserver 8000

# 3. local agent                                → http://localhost:8111
.venv/bin/uvicorn local_agent.main:app --port 8111
```

Log in at <http://localhost:8000/login/>:

| User | Batch | Sees |
|---|---|---|
| Aarav (pick the name) | 2024 lead | everything |
| Kabir (Google sign-in) | 2025 | 403 on `/assign/`, `/contacts/import/`, `/campaigns/new/`, `/members/` |

The agent runs as whichever member `AGENT_API_TOKEN` belongs to.

---

## 14. Supabase — the master database

Supabase replaces the Docker Postgres. It changes nothing about the
architecture: Django remains the only process that connects to it, and laptops
still hold no database credentials.

### 14.1 Use the session pooler

From **Project Settings → Database → Connection string → Session pooler**:

```
postgres://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require
```

Three ways to get this wrong, worst first:

| Don't | Why |
|---|---|
| Port **6543** (transaction pooler) | `mailing.py` and `assignment.py` depend on `SELECT … FOR UPDATE`; psycopg3's prepared statements break there. If you must, also set `DB_TRANSACTION_POOLER=True`, which sets `prepare_threshold=None` and disables server-side cursors. |
| `db.<ref>.supabase.co` direct | IPv6-only without the paid IPv4 add-on. |
| Omitting `sslmode=require` | Every contact and token travels the public internet in the clear. |

`DB_CONN_MAX_AGE` defaults to `0`. Persistent connections eat pooler slots on the
free tier faster than traffic does.

### 14.2 What changes in `.env` on every laptop

```diff
- COMPOSE_PROFILES=localdb                    # stop running a database nobody uses
+ DATABASE_URL_DOCKER=postgres://postgres.<ref>:<pw>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require
```

Nothing else. `RUN_MIGRATIONS` and `SEED_DEV` notice the host is not local and
switch themselves off (§13.1) — on the one machine that owns the schema, set
`RUN_MIGRATIONS=true`.

### 14.3 Cutover

```bash
cd core_django

# 1. build the schema on the far side
DATABASE_URL="<session-pooler-url>" ../.venv/bin/python manage.py migrate

# 2. VERIFY — migrations "succeeding" is not proof the constraint landed
DATABASE_URL="<session-pooler-url>" ../.venv/bin/python manage.py check_db

# 3. move existing data
pg_dump --data-only --no-owner "postgres://ignite:ignite@localhost:5432/ignite_crm" \
  | psql "<session-pooler-url>"

# 4. re-verify after the data load
DATABASE_URL="<session-pooler-url>" ../.venv/bin/python manage.py check_db
```

**Step 2 is not optional.** The entire no-double-mail guarantee is one index.

### 14.4 RLS

Django connects as `postgres`, which **bypasses RLS**. That is deliberate: Django
is the only client, and permissions are enforced in `services/permissions.py`.

Two consequences to respect:
- Never expose the Supabase `anon` or `service_role` keys to any frontend.
- Do not add a second direct-to-Postgres client without revisiting this decision.
  If a browser ever talks to Supabase directly, every rule in this repo is bypassed.

### 14.5 Tests never touch it

`config/settings.py` forces local Docker Postgres whenever pytest is running,
regardless of `DATABASE_URL`:

```python
RUNNING_TESTS = "pytest" in sys.modules or "test" in sys.argv
```

Django's test runner CREATEs and DROPs its database. Pointing that at production
would be unrecoverable. The guard lives in settings rather than `conftest.py`
because pytest-django calls `django.setup()` before root conftest files are
imported — an override there would be too late.

The pytest header prints which host it chose:

```
ignite: tests pinned to localhost:5432/ignite_crm (never the hosted database)
```

Verified by running the suite with `DATABASE_URL` pointed at a fake Supabase
host: all 76 tests still pass against localhost.

---

## 15. Google setup

**Two OAuth clients, one Google Cloud project.** They are not interchangeable and
mixing them up is the most likely thing to go wrong here:

| | client type | used by | for |
|---|---|---|---|
| `client_secret.json` | **Desktop app** | `local_agent` | sending mail as the member |
| `GOOGLE_OAUTH_CLIENT_ID/SECRET` | **Web application** | `core_django` | batch-2025 sign-in |

### 15.1 Web client — sign-in for batch 2025

1. **Credentials → Create OAuth client ID → Web application**.
2. Authorised redirect URI, exactly:
   `http://localhost:8000/login/google/callback/`
   (add your public URL too if you host the CRM).
3. Put the id and secret in `.env` as `GOOGLE_OAUTH_CLIENT_ID` /
   `GOOGLE_OAUTH_CLIENT_SECRET`.

Leave them blank and the Google door is disabled with a message on the login
page rather than a stack trace — but **no batch-2025 member can sign in to the
CRM until they are set**. They can still send: the agent authenticates with an
API token, not a browser session.

`GOOGLE_OAUTH_HOSTED_DOMAIN` defaults to `pilani.bits-pilani.ac.in` and is
checked against the signed `hd` claim, so a personal Gmail is refused even if
somebody put one in the pool. Sign-in never creates a member — an address with
no active `TeamMember` is turned away.

### 15.2 Desktop client — sending

One-time, per member:

1. **Google Cloud Console** → same project → enable the **Gmail API**.
2. **OAuth consent screen** → External → add each team member as a test user.
3. **Credentials → Create OAuth client ID → Desktop app** → download the JSON as
   `client_secret.json` at the repo root (gitignored).
4. Ask a lead to issue you a token on `/members/`. **It is shown once.**
5. Fill in `.env`: `AGENT_API_BASE_URL`, `AGENT_API_TOKEN`, `AGENT_MEMBER_EMAIL`.
6. Start the agent and press **Verify Gmail** — the consent screen appears once.

Scopes requested: `gmail.send` and `gmail.readonly`. Readonly is needed for
stranded-draft reconciliation — asking Gmail whether a mail actually went out.
Cached tokens live in `~/.ignite_crm`, `chmod 600`.

Each person needs **their own** token. Never share one — the token is what makes
`sent_by` meaningful.

---

## 16. Deploying

```bash
docker build -t ignite-crm .        # from the repo root; `shared/` needs it
```

The image's default command is the CRM. Run it with the entrypoint below so a
deploy migrates and verifies before it serves:

```bash
docker run -p 8000:8000 --env-file prod.env ignite-crm
```

App-level environment variables:

| Variable | Value |
|---|---|
| `DATABASE_URL` | the Supabase session-pooler URL |
| `DJANGO_SECRET_KEY` | `python -c "import secrets; print(secrets.token_urlsafe(50))"` |
| `DJANGO_ALLOWED_HOSTS` | your public hostname |
| `DJANGO_DEBUG` | `False` |

With `DEBUG=False`, `settings.py` automatically enables `SECURE_SSL_REDIRECT`,
`SECURE_PROXY_SSL_HEADER`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`,
one-year HSTS with subdomains, `X_FRAME_OPTIONS=DENY`, `CSRF_TRUSTED_ORIGINS`
derived from `ALLOWED_HOSTS`, and `CompressedManifestStaticFilesStorage`.

Release step: `manage.py migrate && manage.py check_db`. Static files are built
into the image via `collectstatic` and served by whitenoise — no nginx needed.

Then point each member's `AGENT_API_BASE_URL` at the public HTTPS host.

---

## 17. Tests

```bash
.venv/bin/python -m pytest          # needs docker compose up
```

**76 tests**, all passing:

| File | Count | Covers |
|---|---|---|
| `test_constraints.py` | 13 | the unique constraint, permissions, status transitions |
| `test_mailing_api.py` | 19 | token auth, claim/report, preflight, drafts, retry |
| `test_contacts_crud.py` | 33 | edit scoping, lifecycle rules, archive/delete, audit, HTTP layer |
| `test_login.py` | 11 | the two doors — see §4.1 |

The single most important test:

```python
def test_claiming_twice_yields_no_second_mailing(...):
    first  = claim(client, auth, campaign, [contact.id]).json()
    second = claim(client, auth, campaign, [contact.id]).json()
    assert len(first["claimed"]) == 1
    assert second["claimed"] == []
    assert "already has a mailing" in second["skipped"][0]["reason"]
    assert CampaignMailing.objects.filter(contact=contact).count() == 1
```

If that ever fails, the system can put two copies of the same mail in a
prospect's inbox. Everything else is negotiable.

Other guarantees pinned by tests: a 2025 member gets `PermissionDenied` editing
someone else's contact; a posted `lifecycle` is dropped for non-leads while the
rest of the edit still applies; `bulk_edit` silently skips contacts outside the
caller's list; a mailed contact refuses hard delete with a message rather than a
500; archived and `do_not_contact` contacts are skipped by `claim_batch` with
`ARCHIVED`/`BLOCKED`; `record_result(sent)` moves `new → contacted` but leaves
`replied` alone; a failed send moves nothing; every mutation writes an audit row
naming its actor.

---

## 18. Environment variables

### CRM (`core_django`)
| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | local Docker Postgres | Supabase session pooler in production |
| `IGNITE_TEST_DATABASE_URL` | local Docker Postgres | only used under pytest |
| `DJANGO_SECRET_KEY` | insecure dev key | **must be set in production** |
| `DJANGO_DEBUG` | `False` | |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` | |
| `DB_CONN_MAX_AGE` | `0` | persistent connections; keep 0 behind a pooler |
| `GOOGLE_OAUTH_CLIENT_ID` | — | **Web** client; blank disables batch-2025 sign-in |
| `GOOGLE_OAUTH_CLIENT_SECRET` | — | pairs with the above |
| `GOOGLE_OAUTH_HOSTED_DOMAIN` | `pilani.bits-pilani.ac.in` | checked against the signed `hd` claim |
| `DB_TRANSACTION_POOLER` | `False` | only for port 6543 |

### Agent (`local_agent`) — no `DATABASE_URL`, by design
| Variable | Notes |
|---|---|
| `AGENT_API_BASE_URL` | the CRM's URL; HTTPS in production |
| `AGENT_API_TOKEN` | issued on `/members/`, shown once |
| `AGENT_MEMBER_EMAIL` | must match both the token owner and the Gmail account |
| `GOOGLE_CLIENT_SECRETS_PATH` | default `./client_secret.json` |
| `AGENT_TOKEN_DIR` | default `~/.ignite_crm` |
| `AGENT_SEND_DELAY_SECONDS` | default `2`; the hard cap is server-side |

Gitignored and never committed: `.env`, `client_secret.json`, `token_*.json`,
`.ignite_crm/`.

---

## 19. Management commands

```bash
cd core_django

../.venv/bin/python manage.py check_db     # verify the live DB is safe to send from
../.venv/bin/python manage.py seed_dev     # dev fixtures
../.venv/bin/python manage.py issue_token <email> --label <name>   # shown once
../.venv/bin/python manage.py migrate
../.venv/bin/python manage.py makemigrations
```

`check_db` asserts that `uniq_campaign_contact` and `contacts_tags_gin` exist,
that `SELECT … FOR UPDATE` actually works over this connection, and that you are
not on the transaction pooler. It exits non-zero on failure, so it can be a
release gate. Sample output:

```
database : localhost:5432/ignite_crm
server   : PostgreSQL 16.14
  ok     campaign_mailings.uniq_campaign_contact
  ok     contacts.contacts_tags_gin
  ok     SELECT ... FOR UPDATE

All checks passed.
```

---

## 20. File layout

```
ignite_crm/
├── conftest.py                    reports which DB tests chose
├── Dockerfile                     ONE image, both apps — see §13.1
├── docker-compose.yml             postgres + crm, and agent behind a profile
├── docker/
│   ├── entrypoint-crm.sh          wait → migrate → check_db → seed → serve
│   └── entrypoint-agent.sh        fail fast on a missing token
├── pytest.ini
├── requirements.txt
├── .env.example
│
├── shared/                        imported by BOTH apps
│   ├── enums.py                   CampaignStatus, MailingStatus, ContactLifecycle,
│   │                              BLOCKED_LIFECYCLES, LEAD_BATCH
│   └── static/basecoat/
│       ├── basecoat.css  (213 KB) components, from basecoat-css@1.0.2
│       ├── basecoat.js   (43 KB)  dialog, select, dropdown, toast, tabs
│       ├── app.css                hand-written layout + lifecycle badge colours
│       ├── poll.js                20s refresh, shared by both apps
│       └── VENDORED.md            provenance
│
├── core_django/
│   ├── config/settings.py         DB config, Supabase notes, the test guard
│   └── crm/
│       ├── auth_views.py          the login page and the two doors
│       ├── models.py              canonical schema — the constraint lives here
│       ├── validators.py          BITS email, phone, batch
│       ├── forms.py               ContactForm, BulkEditForm, CampaignForm, …
│       ├── views.py  urls.py      every screen
│       ├── admin.py
│       ├── services/              ← every rule (see §11), incl. auth.py
│       ├── api/                   auth.py, views.py, urls.py
│       ├── management/commands/   check_db.py, seed_dev.py, issue_token.py
│       ├── migrations/            0001_initial, 0002_apitoken,
│       │                          0003_contact_lifecycle_tags_archive
│       ├── templates/crm/         14 templates
│       └── tests/                 76 tests
│
└── local_agent/
    ├── main.py                    lifespan identity checks + 12 routes
    ├── api_client.py              httpx wrapper — the only link to the CRM
    ├── config.py
    ├── services/send.py           claim → send → report, + reconcile
    ├── gmail/client.py            OAuth, token storage, identity binding
    └── templates/index.html       the send UI + inline editor
```

---

## 21. Operational playbook

**Someone is mailing the wrong people — stop everything.**
Set the campaign to `paused` on `/campaigns/<id>/`. Every agent stops at the next
claim; no restart needed.

**A prospect asks never to be contacted again.**
Set their lifecycle to `do_not_contact` (lead). `claim_batch` refuses them
permanently, across all campaigns.

**A member left the team.**
Revoke their tokens on `/members/`, set `is_active = False`. Their agent stops
working immediately; their mail history is preserved.

**A token leaked.**
Revoke it on `/members/` and issue a new one. Only the hash is stored, so nothing
else is exposed. `last_used_at` shows whether it was used after the leak.

**Someone deleted the wrong thing.**
Contacts that have been mailed cannot be deleted at all. For everything else,
`/contacts/<id>/` shows the full change log with actor and timestamp.

**An agent crashed mid-batch.**
Restart it and press **Resolve stranded drafts**. It asks Gmail what actually
went out and settles each row. It never re-sends blindly.

**The CRM is down but mail must go out.**
It can't, and that's the design. The agent cannot claim, so it cannot send. This
is preferable to sending without a record.

---

## 22. Change log

### Phase 3 — Docker, and passwordless sign-in

- One root `Dockerfile` holding both apps; `core_django/Dockerfile` removed.
  `docker/entrypoint-crm.sh` migrates, runs `check_db`, and **refuses to serve if
  the constraint is missing**. See §13.1.
- `manage.py issue_token` for bootstrapping an agent before anyone can log in.
- `whitenoise` and `gunicorn` added to `requirements.txt` — `whitenoise` was
  already in `MIDDLEWARE` and missing from the file.
- **Passwords removed from the CRM.** New `services/auth.py` and `auth_views.py`:
  batch 2024 picks a name, batch 2025 signs in with Google on the BITS domain.
  Identity is now a session key holding a `TeamMember` id; `TeamMember.user` and
  Django's `User` survive only for `/admin/`.
- `member_required` / `lead_required` now redirect a stranger to `/login/` and
  keep raising `PermissionDenied` for a real refusal.
- 11 new tests in `test_login.py`; 76 total.
- `migrate` and `seed_dev` now key off whether the database is local, so a
  laptop pointed at the shared pool cannot reseed it or race a migration.
  `check_db` still runs unconditionally and still fails the boot.
- The local `postgres` container moved behind the `localdb` profile, and `crm`
  dropped its `depends_on` in favour of the entrypoint's own wait — which works
  for a hosted database too, where there is no container to depend on.

### Phase 2 — shared editing, lifecycle/tags, Supabase (commit `1aaf2df`)

**Schema — migration `0003`**
- `Contact` gains `lifecycle`, `tags` (Postgres array + GIN index), `is_archived`,
  `archived_at`, `archived_by`, `created_by`
- New `ContactAudit` model: one row per field change, naming the actor
- New index `(is_archived, lifecycle)`

**Rules**
- `record_result()` flips `new → contacted` on a confirmed send, scoped so a
  later campaign never drags `replied` backwards
- `claim_batch` and `preflight` refuse archived and `do_not_contact`/`bounced`
  contacts via a shared `unmailable_reason()` helper, checked under the row lock
- New outcome codes `ARCHIVED` and `BLOCKED`
- `can_edit_contact`, `can_set_lifecycle`, `can_hard_delete`, `editable_contacts`
  in `permissions.py`
- New `services/contacts.py`: every mutation funnels through it, writing audit rows
- `hard_delete` refuses mailed contacts with an explanation instead of a 500
- Tags lowercased and de-duplicated on the way in

**Surfaces**
- Django: `/contacts/new`, `/contacts/<id>/edit`, `/archive`, `/delete`,
  `/bulk-edit`; tag/stage/archived filters; change log on the detail page
- Agent: inline row editor, richer contact payload, live quota
- API: `POST /contacts/new`, `PATCH /contacts/<id>`
- `poll.js` shared by both apps
- Lifecycle badge colours in `app.css`, light and dark
- CSV import accepts a semicolon-separated `tags` column

**Infrastructure**
- `settings.py` documents the session-pooler requirement and pins tests to local
  Postgres regardless of `DATABASE_URL`
- New `manage.py check_db`
- `conftest.py` reports the chosen database in the pytest header

**Tests** — 33 new, 65 total.

**Fixed along the way**
- The agent's quota badge was captured once at FastAPI startup and stale for the
  whole process lifetime; now polled
- Checkbox listeners on `/assign/` were bound per row and would have been lost on
  a polled refresh; now delegated
- A mangled fragment in this README's "Running the CRM" section

### Phase 1 — the base system (commit `6a5b206`)

Four-table schema with the unique constraint; claim/report protocol moved
server-side; token auth; the 15 Django screens; the local agent as a thin client;
Basecoat UI vendored for both apps; 32 tests.

---

## 23. Known gaps

**A real Gmail send has never been tested.** The Gmail path has only ever run
against `FakeGmail` mocks. Every other guarantee in this document is proven by a
test; this one is proven by nothing. **Test it with a batch of one to your own
address before pointing it at real prospects.**

**Dev credentials are in the repo.** `docker-compose.yml` uses `ignite:ignite`.
Harmless on localhost, but visible in a public org repo.

**The name door trusts the network.** Batch-2024 sign-in is a dropdown and a
button (§4.1). That is a deliberate trade for a tool every lead runs on their own
laptop, and it is wrong the moment the CRM gets a public hostname.

**`bounced` is never set automatically.** Nothing reads bounce notifications;
a lead sets it by hand. Inferring it from SMTP error strings was judged worse
than leaving it manual.

**Polling is not instant.** Up to 20 seconds of staleness by design. The database
is always correct immediately; only the screens lag.

**No rate limit on `claim` beyond the daily cap.** A member could claim 400 in
one burst. Gmail's own throttling is the backstop.

**The dashboard shows no lifecycle funnel yet.** `services/contacts.py`
provides `lifecycle_counts()`, but no screen calls it.
