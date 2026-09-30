# Architecture

The `.mmd` files beside this one hold the same diagrams as standalone Mermaid
sources (`sequence-diagram.mmd`, `agentA-graph.mmd`, `agentB-graph.mmd`). They
are embedded here too, because GitHub renders Mermaid inside Markdown but not
`.mmd` files on their own.

## End-to-end sequence

```mermaid
sequenceDiagram
  participant WA as WhatsApp (User)
  participant N8 as n8n Orchestrator
  participant A as Agent A (Knowledge, LangGraph)
  participant B as Agent B (Dealflow, LangGraph)
  participant G as Google APIs (Drive/Sheets/Calendar)
  participant VS as Vector Store (Chroma)

  WA->>N8: inbound message (text and/or file)
  N8-->>WA: 200 acknowledged immediately
  Note over N8: Meta abandons a webhook after ~20s,<br/>so the work runs after the ack
  alt status callback (delivered/read)
    N8->>N8: ignored, no further work
  else has file
    N8->>G: save to Drive:/KnowledgeBase/**
    N8->>N8: extract PDF text
    N8->>A: POST /agentA/ingest {driveFileId, filename, text}
    A->>VS: Ingest -> Embed -> Persist
    A-->>N8: {chunks, tokens, docKey}
    N8->>G: append Conversations row
    N8->>WA: confirmation
  else text only
    N8->>B: POST /classify {text}
    B-->>N8: {intent, context, entities, confidence}
    alt intent == knowledge_qa
      N8->>A: POST /agentA/ask {userId, text}
      A->>VS: Retrieve
      A->>A: Answer -> SelfReflect -> Revise -> LogIntent
      A-->>N8: {answer, citations[], confidence}
      N8->>G: append Conversations row
      N8->>WA: grounded answer + citations
    else intent == next_step
      alt context == knowledge
        N8->>A: POST /agentA/followup-parse {text}
        A-->>N8: {title, startISO, endISO, attendees}
      else context == dealflow
        N8->>B: POST /agentB/nextstep-parse {text}
        B-->>N8: {title, startISO, endISO}
      end
      N8->>G: create Calendar event
      N8->>G: update CRM NextStepDate
      N8->>WA: confirmation + link
    else intent in {lead_capture, proposal_request, status_update}
      N8->>B: matching endpoint with normalized payload
      B-->>N8: typed JSON result
      N8->>G: Sheets / Drive / Calendar side effects
      N8->>WA: confirmation + links
    end
  end
```

## Agent A — Knowledge

```mermaid
flowchart LR
  subgraph INGEST_GRAPH
    I1[Ingest: split text] --> I2[Embed] --> I3[Persist in Chroma]
  end

  subgraph ASK_GRAPH
    Q1[Retrieve] --> Q2[Answer]
    Q2 -->|confidence >= 0.9<br/>or no hits| Q5[LogIntent]
    Q2 -->|otherwise| Q3{SelfReflect}
    Q3 -->|supported| Q5
    Q3 -->|unsupported| Q4[Revise]
    Q4 --> Q5
    Q5 --> Q6[answer + citations + confidence]
  end

  subgraph FOLLOWUP_GRAPH
    F1[ScheduleIntent: parse time] --> F2[Validate<br/>weekday correction]
  end
```

The self-reflection pass is conditional: it is skipped when nothing was
retrieved, or when the first answer already scored 0.9 or above. The common
case therefore costs one LLM call, not two.

## Agent B — Dealflow

```mermaid
flowchart LR
  subgraph LEAD_GRAPH
    L1[Parse] --> L2[ValidateEnrich<br/>domain guess, budget normalise] --> L3[Score<br/>qualityScore + missingFields] --> L4[LogIntent]
  end

  subgraph PROPOSAL_GRAPH
    P1[ProposalCopy] --> P2[ValidateCopy<br/>title, summary, 3-5 bullets]
  end

  subgraph NEXTSTEP_GRAPH
    N1[ScheduleIntent] --> N2[ValidateTime<br/>weekday correction]
  end

  subgraph STATUS_GRAPH
    S1[StatusClassify] --> S2[ValidateStatus<br/>fixed reason vocabulary]
  end
```

## Shared intent classifier

```mermaid
flowchart LR
  C1[Normalize] --> C2[Classify<br/>LLM, JSON schema] --> C3[Validate<br/>schema check + keyword fallback]
  C3 --> C4[intent, context, entities, confidence, fallbackUsed]
```

One mini-graph rather than duplicated logic in both agents. When the model
returns something off-schema, an ordered keyword fallback still produces a
route, and `fallbackUsed` records which path ran.

## Deviations from the suggested layout

`§10` of the brief suggests `docker-compose.yml` and `env.sample` under
`/infra/`, and per-agent `tests/` directories. This repository keeps
`docker-compose.yml` and `env.sample` at the root, and a single top-level
`tests/`.

- **Compose at the root** so `docker compose up -d --build` works with no `-f`
  flag, which is what the brief's own one-command bootstrap asks for. Moving it
  under `/infra/` would require every build context and volume path to reach
  back out with `../`.
- **One `tests/` directory** because the suite loads both agents in the same
  process to prove they stay independent — they each define a `graph.py` and a
  `tools.py`, and `tests/conftest.py` loads them by path so the names cannot
  collide. Splitting the suite per agent would lose that check.
