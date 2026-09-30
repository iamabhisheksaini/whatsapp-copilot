# WhatsApp Revenue Copilot

A WhatsApp copilot that answers questions grounded in your Google Drive
documents and captures and advances sales leads — two LangGraph agents
orchestrated by n8n.

Users never type slash commands. Intent is detected from natural language and
attachments.

---

## Architecture

```
WhatsApp ──▶ n8n ──┬──▶ Agent A (Knowledge)  ──▶ Chroma
                   └──▶ Agent B (Dealflow)
                   │
                   └──▶ Google Drive / Sheets / Calendar
```

**The agents never call Google or WhatsApp.** They take JSON in and return typed
JSON out; n8n performs every side effect. That keeps the agents pure enough to
unit test without mocking half of Google, and means a credential change touches
one place.

| Service | Port | Role |
|---|---|---|
| `agentA` | 8001 | Ingest, retrieve, answer with citations, parse follow-ups |
| `agentB` | 8002 | Lead parsing, proposal copy, scheduling, status — plus the shared intent classifier |
| `chroma` | 8003 | Persistent vector store |
| `n8n` | 5678 | Orchestration and all integrations |

### Agent A — Knowledge

```
Ingest ──▶ Embed ──▶ Persist                        (ingest)

Retrieve ──▶ Answer ──┬─(confident)────────────────▶ LogIntent
                      └─▶ SelfReflect ─┬─(ok)──────▶ LogIntent
                                       └─▶ Revise ─▶ LogIntent

ScheduleIntent ──▶ Validate                         (follow-up)
```

The self-reflection pass only runs when it can change the outcome: skipped when
nothing was retrieved, or when the first answer already scored ≥ 0.9. That keeps
the common case to one LLM call.

### Agent B — Dealflow

```
Parse ──▶ ValidateEnrich ──▶ Score ──▶ LogIntent   (lead capture)
ProposalCopy ──▶ ValidateCopy                      (proposal)
ScheduleIntent ──▶ ValidateTime                    (next step)
StatusClassify ──▶ ValidateStatus                  (status)
```

Node names match §3 of the brief, so the graphs read against the spec's node
map directly. Full diagrams: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

### Shared intent classifier

`Normalize ──▶ Classify ──▶ Validate`, exposed at `POST /classify`. A single
mini-graph rather than duplicated logic in both agents. If the model returns
something off-schema, a keyword fallback still routes the message rather than
failing the request — `fallbackUsed` in the response says which path ran.

---

## Quickstart

```bash
cp env.sample .env      # then fill in OPENAI_API_KEY
docker compose up -d --build
```

Verify:

```bash
curl -s localhost:8001/health && curl -s localhost:8002/health
```

That gets the agents running. WhatsApp and Google need credentials — see
[docs/GOOGLE_SETUP.md](docs/GOOGLE_SETUP.md) and [WhatsApp](#whatsapp) below.

### Import the workflows

n8n UI (<http://localhost:5678>) → **Workflows → Import from File**, once per
file in `n8n/workflows/`. Then attach credentials on each Google node.

The JSON is generated — edit `n8n/build_workflows.py` and re-run it rather than
hand-editing 49 nodes:

```bash
python3 n8n/build_workflows.py
```

No ids or tokens are baked into the committed JSON. Workflows read them at run
time via `{{ $env.NAME }}`, passed in by `docker-compose.yml`.

### WhatsApp

1. Meta for Developers → create an app → add **WhatsApp**.
2. Expose n8n publicly (`ngrok http 5678`) and set `WEBHOOK_URL` in `.env` to
   the tunnel URL.
3. Webhook URL: `<WEBHOOK_URL>/webhook/whatsapp-router`, verify token: whatever
   you set as `WHATSAPP_VERIFY_TOKEN`.
4. Subscribe to the **messages** field.
5. Set `WHATSAPP_PHONE_ID` to your phone number id.

The router's `Meta Verify` node handles the GET handshake; the POST path handles
inbound messages.

---

## Testing

```bash
infra/.venv/bin/python -m pytest        # 99 tests
infra/.venv/bin/ruff check .            # lint
```

99 tests, no network access — the LLM and the vector store are both faked, so
the suite runs in about a second.

Covered: chunking and document identity, idempotent re-ingest, citation
fallback, the reflection/revision branch, unparseable model output, budget
normalisation, domain guessing, lead scoring, proposal validation, weekday
correction, and status coercion.

`tests/test_intent_golden.py` holds the golden tests for intent classification
and entity extraction: a table of messages with their expected route asserted
exactly against the keyword layer (no LLM, fully deterministic), plus a
schema contract checked for every one of the seven intents.

### Metrics

Both agents expose `GET /metrics` — the four measures the brief names:

```bash
curl -s localhost:8001/metrics   # files ingested, Q&A latency, retrieval hit rate
curl -s localhost:8002/metrics   # lead funnel counts
```

Process-local counters returned as JSON, reset on restart. Deliberately not
Prometheus: the call sites would be identical, and this keeps the demo
dependency-free.

---

## Design notes

### The vector store really does persist

Chroma writes to `/data` inside the container. An earlier version of this
compose file mounted the host volume at `/chroma/data`, so the index lived only
in the container's writable layer and vanished on `docker compose down`. It now
mounts `/data`. To confirm:

```bash
docker compose down && docker compose up -d
curl -s "http://localhost:8003/api/v2/tenants/default_tenant/databases/default_database/collections"
```

The `knowledge` collection should still be there.

### Weekday names beat the model's arithmetic

Asked for "next Wed" on a Tuesday, gpt-4o-mini returned a date that was a
*Tuesday* — even when given today's date and weekday in the prompt. Booking a
meeting on the wrong day is a failure a user only notices afterwards, so when a
message names a weekday, `shared/timeparse.py` treats that name as authoritative
and snaps the model's date onto the nearest matching day. The response sets
`weekdayCorrected: true` when this fires.

The model still reads the time of day and the intent — only the date component
is corrected.

### Idempotency

Retries are expected: Meta redelivers webhooks, and the nightly re-index walks
every file again.

- **Vector store** — chunk ids derive from the Drive file id, and ingest purges
  a document's existing chunks before writing. Re-indexing replaces rather than
  duplicates, and a file renamed in Drive keeps its identity.
- **Sheets** — writes use `appendOrUpdate` matched on `MessageId`
  (Conversations) or `LeadId` (CRM), so a redelivered webhook updates the row it
  already wrote.

### Errors

Agent calls retry three times with backoff and `neverError`, so a failing agent
still reaches the reply node and the user gets a message rather than silence.
Unhandled failures go to the **Error Channel** workflow, which logs to the
Conversations sheet and alerts the operator on WhatsApp.

Every response carries a `requestId`, forwarded from n8n as `X-Request-Id` and
attached to every structured log line, so one message can be traced end to end.

### Configuration

Chat and embedding providers are configured separately — not every
OpenAI-compatible gateway serves `/embeddings`, and this project points chat at
OpenRouter.

> **Embedding model.** The existing index was built with
> `text-embedding-ada-002`. It and `text-embedding-3-small` are both 1536
> dimensions, so mixing them fails *silently* with poor retrieval rather than an
> error. `.env` pins ada-002; to switch, change `EMBEDDINGS_MODEL` and re-ingest
> every document.

---

## API

### Agent A

| Endpoint | In | Out |
|---|---|---|
| `POST /agentA/ingest` | `{filename, text, driveFileId?, metadata?}` | `{chunks, tokens, docKey, requestId}` |
| `POST /agentA/ask` | `{userId, text}` | `{answer, citations[], confidence, lowConfidence, revised, requestId}` |
| `POST /agentA/followup-parse` | `{text}` | `{title, startISO, endISO, attendees[], weekdayCorrected, requestId}` |

### Agent B

| Endpoint | In | Out |
|---|---|---|
| `POST /classify` | `{text, recentContext?}` | `{intent, context, entities, confidence, fallbackUsed, requestId}` |
| `POST /agentB/newlead` | `{raw}` | `{name, company, intent, budget, budgetAmount, normalizedCompanyDomain, qualityScore, missingFields[], notes, requestId}` |
| `POST /agentB/proposal-copy` | `{lead}` | `{title, summaryBlurb, bulletPoints[], wordCount, requestId}` |
| `POST /agentB/nextstep-parse` | `{text}` | `{title, startISO, endISO, weekdayCorrected, requestId}` |
| `POST /agentB/status-classify` | `{label, reasonText?}` | `{label, reasonCategory, reasonSummary, requestId}` |

Both expose `GET /health`. Errors return `{error, where, requestId}` with a 500.

Try one:

```bash
curl -s -X POST localhost:8002/agentB/newlead \
  -H 'content-type: application/json' \
  -d '{"raw":"John from Acme wants a PoC in September, budget around 10k."}'
```

---

## Demo script

1. **"What's our refund policy?"** → grounded answer with citations.
2. **Send `Refunds_2025.pdf`** → confirms it was added to the knowledge base.
3. **"And what about digital goods refunds?"** → answers from the new document.
4. **"Schedule a call next Tue 10:00 with Dana about refunds."** → Calendar
   event plus a confirmation link.
5. **"John from Acme wants a PoC in September, budget ~10k."** → CRM row.
6. **"Draft a proposal for Acme."** → Drive PDF link.
7. **"Let's set a demo next Wed at 11."** → Calendar event, `NextStepDate`
   updated.
8. **"We lost Acme — budget cut."** → Stage `Lost`, reason categorised `budget`.

Finish by showing the Conversations and CRM sheets, the Drive folders and the
Calendar.

---

## Layout

```
agents/
  shared/           llm.py, intent.py (classifier mini-graph), timeparse.py
  agentA_knowledge/ app.py, graph.py, tools.py
  agentB_dealflow/  app.py, graph.py, tools.py
  requirements.txt
n8n/
  build_workflows.py
  workflows/        whatsapp_router, drive_watch, nightly_reindex, error_channel
tests/              99 tests, incl. golden intent tests
docs/               ARCHITECTURE.md (diagrams), GOOGLE_SETUP.md
                    sequence-diagram.mmd, agentA-graph.mmd, agentB-graph.mmd
ruff.toml
data/chroma/        persisted vector store (gitignored)
docker-compose.yml
env.sample
```

## Status

Working and verified: both agents on LangGraph, all endpoints against a live
LLM, persistence across restarts, the full test suite, and all four workflows
importing into n8n.

Needs your credentials before an end-to-end demo: Google OAuth and the Sheets,
Drive and Calendar ids ([docs/GOOGLE_SETUP.md](docs/GOOGLE_SETUP.md)), plus the
WhatsApp webhook. Until those are attached, the Google nodes are wired but
inert.
