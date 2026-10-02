# Demo guide, request blueprint and user manual

Three parts:

1. [Screen recording script](#part-1--screen-recording-script) — what to film, in what order
2. [Request blueprint](#part-2--request-blueprint) — one message traced through every component
3. [User manual](#part-3--user-manual) — what it does and which problem each feature solves

---

# Part 1 — Screen recording script

Target length **6–7 minutes**. Record the real system. A reviewer who has built
on the WhatsApp Cloud API can tell a genuine run from an edited one, and the
genuine run is more convincing than any animation.

## Before you press record

```bash
ngrok http 5678          # leave running in its own tab
caffeinate -d            # stop the Mac sleeping, its own tab
docker compose ps        # all four services up
```

Checks that save a retake:

- `curl -s localhost:8001/health && curl -s localhost:8002/health` → both `ok`
- Meta token is the **System User** one (never expires) in *both* n8n credentials
- CRM sheet has **no** `acme` row — step 5 must create it live
- Conversations sheet cleared of test and error rows
- Drive `KnowledgeBase` contains only `refund-policy.txt`; keep
  `Refunds_2025.pdf` on your desktop, you will send it during the demo
- Silence notifications on both phone and Mac

## Window layout

| Where | What |
|---|---|
| Left third | WhatsApp — phone mirrored, or WhatsApp Web |
| Right two-thirds | Browser with tabs in this order: n8n executions, Conversations sheet, CRM sheet, Drive `Proposals`, Google Calendar |

Record the full screen, not a window — you will switch tabs constantly.

## Shot list

### 0:00 — Opening (20s)

Show the n8n canvas with **WhatsApp Router** open, zoomed so the branches are
visible.

> "This is a WhatsApp copilot. It answers questions from company documents and
> captures sales leads. Two LangGraph agents, orchestrated by n8n. The user
> never types a command — intent is detected from what they write."

Trace the branches with the cursor while saying it.

### 0:20 — Step 1: grounded question (40s)

Send from WhatsApp:

> What is our refund policy?

While it runs, switch to the n8n executions tab and open the live run. Let the
nodes tick green on camera — that is the moment that proves it is real.

> "Classified as knowledge_qa, retrieved from the vector store, answered with a
> citation."

Show the reply on the phone, pointing at `Sources: refund-policy.txt`.

Then the Conversations sheet: the new row with intent, confidence and citation.

### 1:00 — Step 2: send a document (40s)

Send `Refunds_2025.pdf` as a WhatsApp attachment.

> "The file goes to Drive, the text is extracted, chunked, embedded and stored.
> The agents never touch Google — n8n does every side effect."

Show: the confirmation reply, the file now in Drive `KnowledgeBase`, and the
Conversations row reading `N chunks indexed`.

### 1:40 — Step 3: ask about the new document (40s)

> And what about digital goods refunds?

> "Same question type, but the citation is now the document I just sent. The
> knowledge base updated live."

Point at the citation changing to `Refunds_2025.pdf`. **This is the strongest
moment in the demo** — hold on it.

### 2:20 — Step 4: scheduling from natural language (50s)

> Schedule a call next Tue 10:00 with Dana about refunds.

Show the Calendar event appearing at the right date and time.

> "No date was given — it resolved 'next Tuesday' against today. And it checks
> itself: the model is unreliable at calendar arithmetic, so when a message
> names a weekday, that name wins and the date gets snapped onto it."

If `weekdayCorrected` is true in the execution output, show it. It is a detail
reviewers notice.

### 3:10 — Step 5: lead capture (50s)

> John from Acme wants a PoC in September, budget around 10k.

Show the CRM row appearing.

> "Name, company and timeline parsed. Budget normalised from 'around 10k' to
> 10000. The domain is guessed, and the lead is scored on completeness so the
> copilot knows what is still missing."

Point at `Domain`, `QualityScore`.

### 4:00 — Step 6: proposal generation (50s)

> Draft a proposal for Acme.

Open the Drive `Proposals` folder, open the generated document.

> "A Drive template, copied and merged with LLM-written copy — title, summary
> and bullets. The link goes back on WhatsApp and into the CRM."

Show the CRM `Links` column now populated.

### 4:50 — Steps 7 and 8: next step and status (50s)

> Let us set a demo next Wed at 11.

Calendar event plus `NextStepDate` in the CRM.

> We lost the Acme deal, budget cut.

> "Stage moves to Lost, and the reason is categorised — 'budget' — from a fixed
> vocabulary, so the funnel stays reportable rather than full of free text."

### 5:40 — Reliability (45s)

Show, quickly:

- **n8n executions list** — "every message traced, with a request id that
  matches the agent logs"
- **Nightly Reindex** execution from 02:00 — "runs unattended"
- **Error Channel** — "failures log to Sheets and alert on WhatsApp. Transient
  network faults log quietly; only real problems interrupt anyone."
- Terminal: `infra/.venv/bin/python -m pytest` → **99 passed**

> "Ninety-nine tests, no network calls — the LLM and the vector store are both
> faked, so the suite runs in about a second."

### 6:25 — Close (20s)

Back to the architecture diagram in `docs/ARCHITECTURE.md`.

> "Two LangGraph agents returning typed JSON. n8n owns every side effect. That
> split is why the agents are testable without mocking half of Google."

## If something fails on camera

Keep recording. Open the failed execution, read the error, say what it means.
A reviewer who has built this knows things break; recovering calmly reads better
than a cut. The error channel firing on camera is a feature demonstration.

---

# Part 2 — Request blueprint

What happens between a user pressing send and the reply arriving.

## The path every message takes

```
User's phone
   │  WhatsApp message
   ▼
Meta's servers
   │  HTTPS POST (JSON envelope)
   ▼
ngrok tunnel                     public URL → your laptop
   ▼
n8n :5678  /webhook/whatsapp-router
   │
   ├─ 1. WhatsApp Webhook      responds 200 immediately  ◄── before any work
   ├─ 2. Normalize Inbound     flatten Meta's payload
   ├─ 3. Is Message?           drop status callbacks
   ├─ 4. Has Attachment?       file branch, or text branch
   │
   ├─ 5. Classify Intent  ──►  Agent B  POST /classify
   ├─ 6. Route by Intent       six outputs
   ├─ 7. <agent call>     ──►  Agent A or Agent B
   ├─ 8. <Google writes>  ──►  Sheets / Drive / Calendar
   └─ 9. Reply            ──►  Meta send API → user's phone
```

### 1. Acknowledge first

Meta abandons a webhook after roughly 20 seconds and redelivers it. The work
below often takes longer, so the webhook answers `200` on receipt and continues
in the background. The user's reply is sent through Meta's **send API**, not the
webhook response, so nothing is lost by answering early.

Answering late caused duplicate executions and one run that hung indefinitely.

### 2. Normalize Inbound

Meta's payload is deeply nested and arrives in three shapes — a live delivery,
a dashboard test, and the flat body used for local `curl` testing. This node
flattens all three into one object:

```json
{
  "isMessage": true,
  "from": "9188...",
  "text": "What is our refund policy?",
  "messageId": "wamid....",
  "requestId": "wamid....",
  "hasAttachment": false,
  "mediaId": "", "mimeType": "", "filename": ""
}
```

`messageId` becomes the **idempotency key**. Meta redelivers on timeout, so
every Sheets write matches on it and updates rather than appends.

### 3. Is Message?

WhatsApp posts delivery and read receipts to the same webhook. They carry a
`statuses` array and no message. Without this gate they reach the fallback
branch and fail trying to reply to an empty recipient — which generated a
spurious alert for every message the bot sent.

### 4. Has Attachment?

**File branch:**

| Step | What happens |
|---|---|
| Get Media URL | `graph.facebook.com/{mediaId}` returns a download link |
| Download Media | fetches from `lookaside.fbsbx.com` — needs a **Bearer header**, not a query parameter, which is why a second credential exists |
| Upload to Drive | saved to `KnowledgeBase/` |
| Extract Text | PDF → plain text |
| Agent A — Ingest | `POST /agentA/ingest` |
| Sheets | Conversations row, `Intent = file_ingest` |
| Reply | "Got it — I added **filename** to the knowledge base." |

Inside Agent A: `Ingest → Embed → Persist`. Chunk ids derive from the Drive file
id, and existing chunks are purged before writing — so re-ingesting replaces
rather than duplicates, and a file renamed in Drive keeps its identity.

### 5. Classify Intent

`POST /classify` on Agent B, a shared LangGraph mini-graph:

```
Normalize → Classify (LLM, JSON schema) → Validate (schema + keyword fallback)
```

Returns:

```json
{
  "intent": "knowledge_qa",
  "context": "knowledge",
  "entities": { "person": null, "company": null, "datetimeText": null,
                "budget": null, "topic": "refunds", "statusLabel": null },
  "confidence": 0.9,
  "fallbackUsed": false
}
```

If the model returns something off-schema, an ordered keyword layer still
produces a route, and `fallbackUsed` records which path ran. A bad model reply
degrades into a plausible route instead of a failed request.

### 6–9. The five branches

#### knowledge_qa

```
Agent A  POST /agentA/ask     Retrieve → Answer → SelfReflect → Revise → LogIntent
   ↓
Sheets   Conversations row    timestamp, user, intent, input, output,
   ↓                          confidence, citations, messageId
Reply    answer + "Sources: ..."
```

`SelfReflect` is conditional — skipped when nothing was retrieved or the first
answer already scored 0.9 or above, so the common case costs one LLM call.
When reflection finds a claim unsupported, `Revise` rewrites it strictly within
the retrieved context and the response reports `revised: true`.

#### lead_capture

```
Agent B  POST /agentB/newlead   Parse → ValidateEnrich → Score → LogIntent
   ↓
Sheets   CRM appendOrUpdate on LeadId
   ↓
Reply    normalised summary + anything still missing
```

`ValidateEnrich` runs without an LLM: domain guessing, budget normalisation
(`"around 10k"` → `10000`), HTML stripping. `Score` returns `qualityScore` and
`missingFields`, so n8n can ask a targeted follow-up rather than a generic one.

#### proposal_request

```
Agent B  POST /agentB/proposal-copy   ProposalCopy → ValidateCopy
   ↓
Drive    copy the template document
   ↓
Docs     batchUpdate replaces {{TITLE}}, {{SUMMARY}}, {{BULLETS}} in one call
   ↓
Sheets   CRM Links + Stage = "Proposal sent"
   ↓
Reply    document link
```

All three placeholders are replaced in a single batch, so a partial fill cannot
happen.

#### next_step

```
Knowledge Context?  ── knowledge ──►  Agent A  /agentA/followup-parse
                    └─ dealflow  ──►  Agent B  /agentB/nextstep-parse
   ↓
Calendar  create event
   ↓
Sheets    CRM NextStepDate
   ↓
Reply     confirmation + event link
```

The classifier's `context` decides which agent handles it — a call about a
documented topic belongs to Agent A, a demo for a deal to Agent B.

Both parsers end in a validation step that applies **weekday correction**: when
the message names a weekday, that name is authoritative and the model's date is
snapped onto the nearest matching day. Asked for "next Wed" on a Tuesday,
gpt-4o-mini returned a Tuesday — in production, not just in testing.

#### status_update

```
Agent B  POST /agentB/status-classify   StatusClassify → ValidateStatus
   ↓
Sheets   CRM Stage + Notes
   ↓
Reply    confirmation
```

`reasonCategory` is coerced to a fixed vocabulary — budget, timing, competitor,
no_decision, bad_fit, scope, champion_left, other — so the funnel stays
reportable. Anything unrecognised becomes `other` rather than polluting the CRM.

## When something fails

Agent calls retry three times with backoff. Unhandled failures reach the
**Error Channel**, which logs to the Conversations sheet and alerts the operator
on WhatsApp — unless the fault is transient (a dropped connection on a poll),
which is logged quietly because the next poll recovers on its own.

Every response carries a `requestId`, forwarded from n8n as `X-Request-Id` and
attached to every structured log line, so one message can be traced end to end.

---

# Part 3 — User manual

## What this is

A WhatsApp assistant for a sales team. You message it the way you would message
a colleague. It answers questions from your company's documents, and it records
and advances your deals.

**There are no commands to learn.** You never type a slash or a keyword. Write
normally and it works out what you meant.

## Features, and the problem each one solves

### Ask about company documents

> *"What is our refund policy?"*
> *"Do we support bulk discounts?"*

**The problem.** Answers live in documents nobody can find mid-conversation. A
rep either guesses — and risks telling a customer something wrong — or breaks
off to search and loses momentum.

**What it does.** Answers from your own documents and cites the file it used, so
you can verify before repeating it. When it cannot find an answer it says so
rather than inventing one.

### Add a document by sending it

> *[send a PDF]*

**The problem.** Knowledge bases go stale because updating them is somebody's
job and nobody's priority.

**What it does.** Sending a file adds it. It is saved to Drive, indexed, and
answerable within seconds. Anyone who can forward a PDF can keep the knowledge
base current.

Documents dropped straight into the Drive folder are picked up automatically
too, and everything is re-indexed nightly.

### Capture a lead from a sentence

> *"John from Acme wants a PoC in September, budget around 10k."*

**The problem.** CRM data decays because entering it is tedious. Leads get
noted in a phone, a notebook, or not at all.

**What it does.** Writes a structured CRM row from one sentence — name, company,
intent, budget, timeline — normalises the budget (`around 10k` → `10000`),
guesses the company domain, and scores the lead on completeness. It tells you
what is missing, so you know to ask about timeline before the call ends.

### Draft a proposal

> *"Draft a proposal for Acme."*

**The problem.** The gap between a good conversation and a sent proposal is
where deals go cold — usually a day or two of someone finding the template.

**What it does.** Copies your Drive template, fills in a title, a summary and
bullets written for that lead, and returns a link. You review and send. The
link is recorded against the lead.

### Schedule from natural language

> *"Schedule a call next Tue 10:00 with Dana about refunds."*
> *"Let us set a demo next Wed at 11."*

**The problem.** Follow-ups agreed in conversation never make it into a
calendar, so they quietly don't happen.

**What it does.** Creates the calendar event and records the next step against
the deal. Relative dates work — "next Tuesday", "tomorrow". When you name a
weekday, that name is treated as authoritative, so a meeting cannot land on the
wrong day.

### Update deal status

> *"We lost the Acme deal, budget cut."*

**The problem.** Nobody updates a CRM after losing. Loss reasons are the most
valuable data a sales team has and the least reliably captured.

**What it does.** Moves the stage and categorises the reason against a fixed
list — budget, timing, competitor, bad fit and so on — so you can later ask
"why are we losing?" and get an answer from data rather than anecdote.

### It keeps a record of everything

Every exchange is logged with its intent, the answer, a confidence score and the
sources cited.

**The problem.** When a customer says "your team told me X", there is usually no
way to check.

**What it does.** Gives you a searchable history, and shows which answers were
low-confidence — the ones worth reviewing.

### It chases stale leads

Nightly, it looks for leads with no next step booked and no activity for a week,
and messages the owner.

**The problem.** Deals die from neglect, not rejection.

## Getting started

1. Save the business number to your contacts
2. Send `Hi` — it will tell you what it can do
3. Ask a question about a document you have added

## Good to know

**Be specific about dates.** "Next Tuesday at 10" is unambiguous. "Sometime next
week" is not, and it will tell you so rather than guess.

**It only knows what you have given it.** If an answer seems wrong, check the
cited file — the document may be out of date rather than the assistant.

**Check its work on proposals.** Generated copy is a strong first draft, not
something to send unread. It is written to avoid commitments it cannot support,
but it has not met your customer.

**Low confidence is flagged.** When it is unsure it says what is missing and
suggests rephrasing or adding a document, rather than answering anyway.

## What it will not do

- Send anything to a customer on your behalf — every reply comes back to you
- Invent an answer when the documents do not contain one
- Record a deal stage you have not told it about
- Act on anything written inside a document you upload; documents are data, not
  instructions
