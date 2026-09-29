#!/usr/bin/env python3
"""Generate the importable n8n workflow JSON in n8n/workflows/.

The router is a ~40 node graph; hand-editing that much JSON is how connection
bugs get in. Building it from this script keeps node names, positions and wiring
consistent, and makes a change like "add a Sheets column" a one-line edit.

Run:  python3 n8n/build_workflows.py
Then: n8n UI -> Workflows -> Import from File

Nothing secret lives in the generated JSON. Google/WhatsApp ids are read at run
time via {{ $env.NAME }}, which docker-compose passes into the n8n container,
and OAuth credentials are attached in the n8n UI after import.
"""

import json
import pathlib
import uuid

OUT = pathlib.Path(__file__).resolve().parent / "workflows"

AGENT_A = "{{ $env.AGENT_A_URL }}"
AGENT_B = "{{ $env.AGENT_B_URL }}"

# Node type versions pinned to what current n8n imports cleanly.
TV = {
    "webhook": 2,
    "http": 4.2,
    "code": 2,
    "switch": 3.2,
    "if": 2.2,
    "set": 3.4,
    "respond": 1.1,
    "sheets": 4.5,
    "drive": 3,
    "calendar": 1.3,
    "schedule": 1.2,
    "errorTrigger": 1,
    "extract": 1,
    "driveTrigger": 1,
}


def node(name, type_, params, pos, tv=1, **extra):
    n = {
        "parameters": params,
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"wa-copilot/{name}")),
        "name": name,
        "type": f"n8n-nodes-base.{type_}",
        "typeVersion": tv,
        "position": pos,
    }
    n.update(extra)
    return n


def chain(*names):
    """Wire a straight line of nodes: a -> b -> c."""
    links = {}
    for src, dst in zip(names, names[1:]):
        links.setdefault(src, {"main": [[]]})["main"][0].append(
            {"node": dst, "type": "main", "index": 0}
        )
    return links


def fan(src, targets):
    """Wire one node's numbered outputs to a list of targets (None to skip)."""
    outputs = []
    for target in targets:
        outputs.append([] if target is None else [{"node": target, "type": "main", "index": 0}])
    return {src: {"main": outputs}}


def merge(*dicts):
    out = {}
    for d in dicts:
        for key, value in d.items():
            if key in out:
                # Same source wired twice: extend its output lists.
                for i, conns in enumerate(value["main"]):
                    while len(out[key]["main"]) <= i:
                        out[key]["main"].append([])
                    out[key]["main"][i].extend(conns)
            else:
                out[key] = json.loads(json.dumps(value))
    return out


def workflow(name, nodes, connections, active=False):
    return {
        "name": name,
        "nodes": nodes,
        "connections": connections,
        "active": active,
        # errorWorkflow is deliberately absent. It takes a literal workflow id,
        # not an expression — n8n does not evaluate {{ $env.X }} here, it just
        # logs "Could not find error workflow" on every failure. The id differs
        # per instance, so hardcoding it would not survive a re-import either.
        # Set it per workflow after import: Workflow menu -> Settings ->
        # Error Workflow -> Error Channel.
        "settings": {"executionOrder": "v1"},
        "pinData": {},
    }


# --- reusable node builders --------------------------------------------------

def agent_call(name, url, body_expr, pos):
    """POST JSON to an agent, with retries for transient failures.

    onError=continueRegularOutput keeps a failed agent call flowing to the reply
    node so the user gets a message instead of silence; the body carries the
    error and the error channel picks it up.
    """
    return node(
        name,
        "httpRequest",
        {
            "method": "POST",
            "url": url,
            "sendHeaders": True,
            "headerParameters": {
                "parameters": [
                    # Propagate the trace id so agent logs line up with n8n runs.
                    {"name": "X-Request-Id", "value": "={{ $('Normalize Inbound').first().json.requestId }}"}
                ]
            },
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": body_expr,
            "options": {"timeout": 120000, "response": {"response": {"neverError": True}}},
        },
        pos,
        tv=TV["http"],
        retryOnFail=True,
        maxTries=3,
        waitBetweenTries=2000,
    )


def whatsapp_reply(name, body_expr, pos):
    """Send a WhatsApp text back to whoever sent the inbound message."""
    payload = (
        "={{ JSON.stringify({ messaging_product: 'whatsapp', "
        "to: $('Normalize Inbound').first().json.from, type: 'text', "
        "text: { body: " + body_expr + " } }) }}"
    )
    return node(
        name,
        "httpRequest",
        {
            "method": "POST",
            "url": "=https://graph.facebook.com/v18.0/{{ $env.WHATSAPP_PHONE_ID }}/messages",
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "facebookGraphApi",
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": payload,
            "options": {},
        },
        pos,
        tv=TV["http"],
        retryOnFail=True,
        maxTries=3,
        waitBetweenTries=2000,
    )


def respond(name, pos):
    return node(
        name,
        "respondToWebhook",
        {"respondWith": "json", "responseBody": "={{ JSON.stringify($json) }}", "options": {}},
        pos,
        tv=TV["respond"],
    )


def sheet_append(name, sheet_env, tab, pos, matching=None):
    """Append (or update, when a matching column is given) a row.

    appendOrUpdate + a matching column is what makes Sheets writes idempotent:
    Meta retries webhooks, and without it a retry writes a duplicate row.
    """
    columns = {
        "mappingMode": "autoMapInputData",
        "value": {},
        "matchingColumns": matching or [],
        "schema": [],
    }
    return node(
        name,
        "googleSheets",
        {
            "resource": "sheet",
            "operation": "appendOrUpdate" if matching else "append",
            "documentId": {"__rl": True, "value": f"={{{{ $env.{sheet_env} }}}}", "mode": "id"},
            "sheetName": {"__rl": True, "value": tab, "mode": "name"},
            "columns": columns,
            "options": {},
        },
        pos,
        tv=TV["sheets"],
        retryOnFail=True,
        maxTries=3,
        waitBetweenTries=2000,
    )


def set_fields(name, fields, pos):
    """Build a Set node that emits exactly the given columns.

    Set typeVersion 3.3+ takes `assignments`, not the older `fields.values`.
    Using the old shape is accepted on import and runs green, but emits an empty
    object — which then appends a blank row to Sheets with no error anywhere.
    """
    return node(
        name,
        "set",
        {
            "mode": "manual",
            "assignments": {
                "assignments": [
                    {
                        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{name}/{k}")),
                        "name": k,
                        "value": v,
                        "type": "string",
                    }
                    for k, v in fields.items()
                ]
            },
            "includeOtherFields": False,
            "options": {},
        },
        pos,
        tv=TV["set"],
    )


# --- 1) whatsapp_router ------------------------------------------------------

NORMALIZE_JS = r"""
// Flatten Meta's nested webhook payload into one predictable object.
// Also accepts a flat {message, from} body so the flow can be tested with curl.
const body = $input.first().json.body ?? $input.first().json;

let text = '', from = '', messageId = '', mediaId = '', mimeType = '', filename = '';

try {
  const value = body.entry[0].changes[0].value;
  const msg = (value.messages || [])[0];
  if (msg) {
    from = msg.from || '';
    messageId = msg.id || '';
    if (msg.type === 'text') {
      text = msg.text?.body || '';
    } else if (['document', 'image', 'audio', 'video'].includes(msg.type)) {
      const media = msg[msg.type] || {};
      mediaId = media.id || '';
      mimeType = media.mime_type || '';
      filename = media.filename || `${msg.type}-${mediaId}`;
      text = media.caption || '';
    }
  }
} catch (e) {
  text = body.message || body.text || '';
  from = body.from || '';
  messageId = body.messageId || '';
}

// Meta retries deliveries; the message id is the natural idempotency key, so
// downstream Sheets writes match on it instead of blindly appending.
const requestId = messageId || `wa-${Date.now()}`;

return [{
  json: {
    from, text, messageId, requestId,
    mediaId, mimeType, filename,
    hasAttachment: Boolean(mediaId),
    receivedAt: new Date().toISOString(),
  }
}];
""".strip()


def build_router():
    nodes = [
        node("Meta Verify", "webhook",
             {"httpMethod": "GET", "path": "whatsapp-router", "responseMode": "responseNode", "options": {}},
             [-200, 60], tv=TV["webhook"], webhookId=str(uuid.uuid4())),
        node("Verify Handler", "code", {"jsCode": (
            "// Meta's one-time subscription handshake: echo hub.challenge back\n"
            "// as plain text when the verify token matches.\n"
            "const q = $input.first().json.query || {};\n"
            "if (q['hub.verify_token'] !== $env.WHATSAPP_VERIFY_TOKEN) {\n"
            "  return [{ json: { status: 403, body: 'forbidden' } }];\n"
            "}\n"
            "return [{ json: { status: 200, body: q['hub.challenge'] } }];"
        )}, [20, 60], tv=TV["code"]),
        node("Respond Verify", "respondToWebhook",
             {"respondWith": "text", "responseBody": "={{ $json.body }}", "options": {}},
             [240, 60], tv=TV["respond"]),

        node("WhatsApp Webhook", "webhook",
             {"httpMethod": "POST", "path": "whatsapp-router", "responseMode": "responseNode", "options": {}},
             [-200, 420], tv=TV["webhook"], webhookId=str(uuid.uuid4())),
        node("Normalize Inbound", "code", {"jsCode": NORMALIZE_JS}, [20, 420], tv=TV["code"]),
        node("Has Attachment?", "if", {
            "conditions": {
                "options": {"caseSensitive": True, "typeValidation": "loose", "version": 2},
                "conditions": [{
                    "id": "has-attachment",
                    "leftValue": "={{ $json.hasAttachment }}",
                    "rightValue": "true",
                    "operator": {"type": "boolean", "operation": "true", "singleValue": True},
                }],
                "combinator": "and",
            },
            "options": {},
        }, [240, 420], tv=TV["if"]),
    ]

    # --- attachment branch: Drive -> extract -> Agent A ingest ---
    nodes += [
        node("Get Media URL", "httpRequest", {
            "method": "GET",
            "url": "=https://graph.facebook.com/v18.0/{{ $json.mediaId }}",
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "facebookGraphApi",
            "options": {},
        }, [460, 200], tv=TV["http"], retryOnFail=True, maxTries=3),
        node("Download Media", "httpRequest", {
            "method": "GET",
            "url": "={{ $json.url }}",
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "facebookGraphApi",
            "options": {"response": {"response": {"responseFormat": "file", "outputPropertyName": "data"}}},
        }, [680, 200], tv=TV["http"], retryOnFail=True, maxTries=3),
        node("Upload to Drive", "googleDrive", {
            "name": "={{ $('Normalize Inbound').first().json.filename }}",
            "driveId": {"__rl": True, "value": "My Drive", "mode": "list"},
            "folderId": {"__rl": True, "value": "={{ $env.DRIVE_KNOWLEDGEBASE_FOLDER_ID }}", "mode": "id"},
            "options": {},
        }, [900, 200], tv=TV["drive"], retryOnFail=True, maxTries=3),
        node("Extract Text", "extractFromFile", {
            "operation": "pdf",
            "binaryPropertyName": "data",
            "options": {},
        }, [1120, 200], tv=TV["extract"], onError="continueRegularOutput"),
        agent_call("Agent A — Ingest", f"={AGENT_A}/agentA/ingest",
                   "={{ JSON.stringify({ "
                   "filename: $('Normalize Inbound').first().json.filename, "
                   "driveFileId: $('Upload to Drive').first().json.id, "
                   "text: $json.text || '', "
                   "metadata: { source: 'whatsapp', from: $('Normalize Inbound').first().json.from } "
                   "}) }}", [1340, 200]),
        set_fields("Conversations Row (Ingest)", {
            "Timestamp": "={{ $now.toISO() }}",
            "User": "={{ $('Normalize Inbound').first().json.from }}",
            "Intent": "file_ingest",
            "Input": "={{ $('Normalize Inbound').first().json.filename }}",
            "Output": "={{ $json.chunks ? $json.chunks + ' chunks indexed' : 'ingest failed' }}",
            "Confidence": "",
            "Citations": "={{ 'https://drive.google.com/file/d/' + $('Upload to Drive').first().json.id }}",
            "MessageId": "={{ $('Normalize Inbound').first().json.messageId }}",
            "Error": "={{ $json.error || '' }}",
        }, [1560, 200]),
        sheet_append("Log Conversation (Ingest)", "SHEET_CONVERSATIONS_ID", "Conversations",
                     [1780, 200], matching=["MessageId"]),
        whatsapp_reply("Reply — File Added",
                       "'Got it — I added *' + $('Normalize Inbound').first().json.filename + "
                       "'* to the knowledge base.'", [2000, 200]),
        respond("Respond — Ingest", [2220, 200]),
    ]

    # --- text branch: classify then route ---
    nodes += [
        agent_call("Classify Intent", f"={AGENT_B}/classify",
                   "={{ JSON.stringify({ text: $json.text }) }}", [460, 640]),
        node("Route by Intent", "switch", {
            "rules": {"values": [
                {"conditions": {
                    "options": {"caseSensitive": True, "typeValidation": "loose", "version": 2},
                    "conditions": [{
                        "id": f"intent-{name}",
                        "leftValue": "={{ $json.intent }}",
                        "rightValue": name,
                        "operator": {"type": "string", "operation": "equals"},
                    }],
                    "combinator": "and",
                }, "renameOutput": True, "outputKey": name}
                for name in ["knowledge_qa", "lead_capture", "proposal_request",
                             "next_step", "status_update"]
            ]},
            "options": {"fallbackOutput": "extra", "renameFallbackOutput": "other"},
        }, [680, 640], tv=TV["switch"]),
    ]

    # knowledge_qa
    nodes += [
        agent_call("Agent A — Ask", f"={AGENT_A}/agentA/ask",
                   "={{ JSON.stringify({ userId: $('Normalize Inbound').first().json.from, "
                   "text: $('Normalize Inbound').first().json.text }) }}", [900, 140]),
        set_fields("Conversations Row (QA)", {
            "Timestamp": "={{ $now.toISO() }}",
            "User": "={{ $('Normalize Inbound').first().json.from }}",
            "Intent": "knowledge_qa",
            "Input": "={{ $('Normalize Inbound').first().json.text }}",
            "Output": "={{ $json.answer || '' }}",
            "Confidence": "={{ $json.confidence }}",
            "Citations": "={{ ($json.citations || []).map(c => c.title).join(', ') }}",
            "MessageId": "={{ $('Normalize Inbound').first().json.messageId }}",
            "Error": "={{ $json.error || '' }}",
        }, [1120, 140]),
        sheet_append("Log Conversation (QA)", "SHEET_CONVERSATIONS_ID", "Conversations",
                     [1340, 140], matching=["MessageId"]),
        whatsapp_reply("Reply — Answer",
                       "$('Agent A — Ask').first().json.answer + "
                       "(($('Agent A — Ask').first().json.citations || []).length "
                       "? '\\n\\n_Sources: ' + $('Agent A — Ask').first().json.citations"
                       ".map(c => c.title).join(', ') + '_' : '')",
                       [1560, 140]),
        respond("Respond — QA", [1780, 140]),
    ]

    # lead_capture
    nodes += [
        agent_call("Agent B — New Lead", f"={AGENT_B}/agentB/newlead",
                   "={{ JSON.stringify({ raw: $('Normalize Inbound').first().json.text }) }}",
                   [900, 340]),
        set_fields("CRM Row (Lead)", {
            "Timestamp": "={{ $now.toISO() }}",
            "LeadId": "={{ ($json.company || 'unknown').toLowerCase().replace(/[^a-z0-9]+/g,'-') }}",
            "Name": "={{ $json.name || '' }}",
            "Company": "={{ $json.company || '' }}",
            "Intent": "={{ $json.intent || '' }}",
            "Budget": "={{ $json.budget || '' }}",
            "Stage": "New",
            "Owner": "={{ $('Normalize Inbound').first().json.from }}",
            "NextStepDate": "",
            "Domain": "={{ $json.normalizedCompanyDomain || '' }}",
            "QualityScore": "={{ $json.qualityScore }}",
            "Notes": "={{ $json.notes || '' }}",
        }, [1120, 340]),
        sheet_append("Upsert CRM (Lead)", "SHEET_CRM_ID", "CRM", [1340, 340], matching=["LeadId"]),
        whatsapp_reply("Reply — Lead",
                       "'Captured: *' + ($('Agent B — New Lead').first().json.name || '?') + "
                       "'* at *' + ($('Agent B — New Lead').first().json.company || '?') + "
                       "'*\\nBudget: ' + ($('Agent B — New Lead').first().json.budget || 'not stated') + "
                       "'\\nTimeline: ' + ($('Agent B — New Lead').first().json.timeline || 'not stated') + "
                       "(($('Agent B — New Lead').first().json.missingFields || []).length "
                       "? '\\n\\nStill missing: ' + $('Agent B — New Lead').first().json"
                       ".missingFields.join(', ') : '')",
                       [1560, 340]),
        respond("Respond — Lead", [1780, 340]),
    ]

    # proposal_request
    nodes += [
        agent_call("Agent B — Proposal Copy", f"={AGENT_B}/agentB/proposal-copy",
                   "={{ JSON.stringify({ lead: { "
                   "company: ($json.entities && $json.entities.company) || '', "
                   "name: ($json.entities && $json.entities.person) || '', "
                   "intent: $('Normalize Inbound').first().json.text } }) }}", [900, 540]),
        node("Copy Proposal Template", "googleDrive", {
            "resource": "file",
            "operation": "copy",
            "fileId": {"__rl": True, "value": "={{ $env.DRIVE_PROPOSAL_TEMPLATE_ID }}", "mode": "id"},
            "name": "={{ $json.title }}",
            "options": {"parents": ["={{ $env.DRIVE_PROPOSALS_FOLDER_ID }}"]},
        }, [1120, 540], tv=TV["drive"], retryOnFail=True, maxTries=3),
        node("Merge Proposal Fields", "httpRequest", {
            "method": "POST",
            "url": "=https://docs.googleapis.com/v1/documents/{{ $json.id }}:batchUpdate",
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "googleDocsOAuth2Api",
            "sendBody": True,
            "specifyBody": "json",
            # The document's placeholders are {{TITLE}}, {{SUMMARY}} and
            # {{BULLETS}}, but writing those braces literally inside an n8n
            # ={{ }} expression makes its parser try to evaluate them and fail
            # with "invalid syntax". Assembling each token by concatenation
            # keeps the produced string identical while leaving nothing for the
            # template engine to latch onto.
            "jsonBody": "={{ (() => { const ph = n => '{' + '{' + n + '}' + '}'; "
                        "const c = $('Agent B — Proposal Copy').first().json; "
                        "return JSON.stringify({ requests: ["
                        "{ replaceAllText: { containsText: { text: ph('TITLE'), matchCase: true }, "
                        "replaceText: c.title } },"
                        "{ replaceAllText: { containsText: { text: ph('SUMMARY'), matchCase: true }, "
                        "replaceText: c.summaryBlurb } },"
                        "{ replaceAllText: { containsText: { text: ph('BULLETS'), matchCase: true }, "
                        "replaceText: (c.bulletPoints || []).map(b => '• ' + b).join('\\n') } }"
                        "] }); })() }}",
            "options": {},
        }, [1340, 540], tv=TV["http"], retryOnFail=True, maxTries=3),
        set_fields("CRM Row (Proposal)", {
            "LeadId": "={{ (($('Agent B — Proposal Copy').first().json.company) || 'unknown')"
                      ".toLowerCase().replace(/[^a-z0-9]+/g,'-') }}",
            "Links": "={{ 'https://docs.google.com/document/d/' + "
                     "$('Copy Proposal Template').first().json.id + '/export?format=pdf' }}",
            "Stage": "Proposal sent",
        }, [1560, 540]),
        sheet_append("Update CRM (Proposal)", "SHEET_CRM_ID", "CRM", [1780, 540], matching=["LeadId"]),
        whatsapp_reply("Reply — Proposal",
                       "'Proposal ready: *' + $('Agent B — Proposal Copy').first().json.title + "
                       "'*\\n' + 'https://docs.google.com/document/d/' + "
                       "$('Copy Proposal Template').first().json.id + '/export?format=pdf'",
                       [2000, 540]),
        respond("Respond — Proposal", [2220, 540]),
    ]

    # next_step — Agent A when the meeting is about a document, Agent B for deals
    nodes += [
        node("Knowledge Context?", "if", {
            "conditions": {
                "options": {"caseSensitive": True, "typeValidation": "loose", "version": 2},
                "conditions": [{
                    "id": "ctx-knowledge",
                    "leftValue": "={{ $json.context }}",
                    "rightValue": "knowledge",
                    "operator": {"type": "string", "operation": "equals"},
                }],
                "combinator": "and",
            },
            "options": {},
        }, [900, 760], tv=TV["if"]),
        agent_call("Agent A — Followup Parse", f"={AGENT_A}/agentA/followup-parse",
                   "={{ JSON.stringify({ text: $('Normalize Inbound').first().json.text }) }}",
                   [1120, 680]),
        agent_call("Agent B — Nextstep Parse", f"={AGENT_B}/agentB/nextstep-parse",
                   "={{ JSON.stringify({ text: $('Normalize Inbound').first().json.text }) }}",
                   [1120, 840]),
        node("Create Calendar Event", "googleCalendar", {
            "resource": "event",
            "operation": "create",
            "calendar": {"__rl": True, "value": "={{ $env.CALENDAR_ID }}", "mode": "id"},
            "start": "={{ $json.startISO }}",
            "end": "={{ $json.endISO }}",
            "additionalFields": {
                "summary": "={{ $json.title }}",
                "description": "=Created from WhatsApp by {{ $('Normalize Inbound').first().json.from }}\n"
                               "Message: {{ $('Normalize Inbound').first().json.text }}\n"
                               "requestId: {{ $('Normalize Inbound').first().json.requestId }}",
            },
        }, [1340, 760], tv=TV["calendar"], retryOnFail=True, maxTries=3),
        set_fields("CRM Row (Next Step)", {
            "LeadId": "={{ (($('Classify Intent').first().json.entities || {}).company || 'unknown')"
                      ".toLowerCase().replace(/[^a-z0-9]+/g,'-') }}",
            "NextStepDate": "={{ $('Create Calendar Event').first().json.start.dateTime "
                            "|| $('Create Calendar Event').first().json.start }}",
        }, [1560, 760]),
        sheet_append("Update CRM (Next Step)", "SHEET_CRM_ID", "CRM", [1780, 760], matching=["LeadId"]),
        whatsapp_reply("Reply — Scheduled",
                       "'Scheduled *' + $('Create Calendar Event').first().json.summary + "
                       "'* for ' + ($('Create Calendar Event').first().json.start.dateTime "
                       "|| $('Create Calendar Event').first().json.start) + "
                       "'\\n' + ($('Create Calendar Event').first().json.htmlLink || '')",
                       [2000, 760]),
        respond("Respond — Next Step", [2220, 760]),
    ]

    # status_update
    nodes += [
        agent_call("Agent B — Status Classify", f"={AGENT_B}/agentB/status-classify",
                   "={{ JSON.stringify({ "
                   "label: (($json.entities || {}).statusLabel) || 'Lost', "
                   "reasonText: $('Normalize Inbound').first().json.text }) }}", [900, 960]),
        set_fields("CRM Row (Status)", {
            "LeadId": "={{ (($('Classify Intent').first().json.entities || {}).company || 'unknown')"
                      ".toLowerCase().replace(/[^a-z0-9]+/g,'-') }}",
            "Stage": "={{ $json.label }}",
            "Notes": "={{ '[' + $json.reasonCategory + '] ' + $json.reasonSummary }}",
        }, [1120, 960]),
        sheet_append("Update CRM (Status)", "SHEET_CRM_ID", "CRM", [1340, 960], matching=["LeadId"]),
        whatsapp_reply("Reply — Status",
                       "'Updated to *' + $('Agent B — Status Classify').first().json.label + "
                       "'* — ' + $('Agent B — Status Classify').first().json.reasonSummary",
                       [1560, 960]),
        respond("Respond — Status", [1780, 960]),
    ]

    # fallback: smalltalk / unknown
    nodes += [
        whatsapp_reply("Reply — Fallback",
                       "\"I can answer questions about our documents, capture leads, draft \" + "
                       "\"proposals, schedule calls and update deal status. What do you need?\"",
                       [900, 1140]),
        respond("Respond — Fallback", [1120, 1140]),
    ]

    connections = merge(
        chain("Meta Verify", "Verify Handler", "Respond Verify"),
        chain("WhatsApp Webhook", "Normalize Inbound", "Has Attachment?"),
        fan("Has Attachment?", ["Get Media URL", "Classify Intent"]),
        chain("Get Media URL", "Download Media", "Upload to Drive", "Extract Text",
              "Agent A — Ingest", "Conversations Row (Ingest)", "Log Conversation (Ingest)",
              "Reply — File Added", "Respond — Ingest"),
        chain("Classify Intent", "Route by Intent"),
        fan("Route by Intent", [
            "Agent A — Ask", "Agent B — New Lead", "Agent B — Proposal Copy",
            "Knowledge Context?", "Agent B — Status Classify", "Reply — Fallback",
        ]),
        chain("Agent A — Ask", "Conversations Row (QA)", "Log Conversation (QA)",
              "Reply — Answer", "Respond — QA"),
        chain("Agent B — New Lead", "CRM Row (Lead)", "Upsert CRM (Lead)",
              "Reply — Lead", "Respond — Lead"),
        chain("Agent B — Proposal Copy", "Copy Proposal Template", "Merge Proposal Fields",
              "CRM Row (Proposal)", "Update CRM (Proposal)", "Reply — Proposal",
              "Respond — Proposal"),
        fan("Knowledge Context?", ["Agent A — Followup Parse", "Agent B — Nextstep Parse"]),
        chain("Agent A — Followup Parse", "Create Calendar Event"),
        chain("Agent B — Nextstep Parse", "Create Calendar Event"),
        chain("Create Calendar Event", "CRM Row (Next Step)", "Update CRM (Next Step)",
              "Reply — Scheduled", "Respond — Next Step"),
        chain("Agent B — Status Classify", "CRM Row (Status)", "Update CRM (Status)",
              "Reply — Status", "Respond — Status"),
        chain("Reply — Fallback", "Respond — Fallback"),
    )
    return workflow("WhatsApp Router", nodes, connections)


# --- 2) drive_watch ----------------------------------------------------------

def build_drive_watch():
    """Index anything dropped straight into the Drive KnowledgeBase folder.

    Covers the case where a document arrives without going through WhatsApp.
    """
    nodes = [
        node("Watch KnowledgeBase", "googleDriveTrigger", {
            "event": "fileCreated",
            "triggerOn": "specificFolder",
            "folderToWatch": {"__rl": True, "value": "={{ $env.DRIVE_KNOWLEDGEBASE_FOLDER_ID }}", "mode": "id"},
            "options": {},
            "pollTimes": {"item": [{"mode": "everyMinute"}]},
        }, [0, 300], tv=TV["driveTrigger"]),
        node("Download File", "googleDrive", {
            "resource": "file",
            "operation": "download",
            "fileId": {"__rl": True, "value": "={{ $json.id }}", "mode": "id"},
            "options": {"binaryPropertyName": "data", "googleFileConversion": {
                "conversion": {"docsToFormat": "text/plain"}}},
        }, [220, 300], tv=TV["drive"], retryOnFail=True, maxTries=3),
        node("Extract Text", "extractFromFile", {
            "operation": "pdf", "binaryPropertyName": "data", "options": {},
        }, [440, 300], tv=TV["extract"], onError="continueRegularOutput"),
        node("Agent A — Ingest", "httpRequest", {
            "method": "POST",
            "url": "={{ $env.AGENT_A_URL }}/agentA/ingest",
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": "={{ JSON.stringify({ "
                        "filename: $('Watch KnowledgeBase').item.json.name, "
                        "driveFileId: $('Watch KnowledgeBase').item.json.id, "
                        "text: $json.text || '', "
                        "metadata: { source: 'drive_watch' } }) }}",
            "options": {"timeout": 120000},
        }, [660, 300], tv=TV["http"], retryOnFail=True, maxTries=3, waitBetweenTries=5000),
        set_fields("Conversations Row", {
            "Timestamp": "={{ $now.toISO() }}",
            "User": "drive-watch",
            "Intent": "file_ingest",
            "Input": "={{ $('Watch KnowledgeBase').item.json.name }}",
            "Output": "={{ $json.chunks + ' chunks indexed' }}",
            "MessageId": "={{ 'drive-' + $('Watch KnowledgeBase').item.json.id }}",
        }, [880, 300]),
        sheet_append("Log Conversation", "SHEET_CONVERSATIONS_ID", "Conversations",
                     [1100, 300], matching=["MessageId"]),
    ]
    connections = chain("Watch KnowledgeBase", "Download File", "Extract Text",
                        "Agent A — Ingest", "Conversations Row", "Log Conversation")
    return workflow("Drive Watch", nodes, connections)


# --- 3) nightly_reindex ------------------------------------------------------

def build_nightly_reindex():
    """Re-embed every KnowledgeBase file nightly, and nudge on stale leads.

    Safe to re-run: Agent A keys chunks by Drive file id and purges the old ones
    before writing, so a re-index replaces rather than duplicates.
    """
    nodes = [
        node("Nightly 02:00", "scheduleTrigger", {
            "rule": {"interval": [{"field": "cronExpression", "expression": "0 2 * * *"}]},
        }, [0, 200], tv=TV["schedule"]),
        node("List KnowledgeBase", "googleDrive", {
            "resource": "fileFolder",
            "operation": "search",
            "searchMethod": "query",
            "queryString": "={{ \"'\" + $env.DRIVE_KNOWLEDGEBASE_FOLDER_ID + \"' in parents and trashed = false\" }}",
            "returnAll": True,
            "options": {},
        }, [220, 200], tv=TV["drive"], retryOnFail=True, maxTries=3),
        node("Download File", "googleDrive", {
            "resource": "file",
            "operation": "download",
            "fileId": {"__rl": True, "value": "={{ $json.id }}", "mode": "id"},
            "options": {"binaryPropertyName": "data"},
        }, [440, 200], tv=TV["drive"], retryOnFail=True, maxTries=3, onError="continueRegularOutput"),
        node("Extract Text", "extractFromFile", {
            "operation": "pdf", "binaryPropertyName": "data", "options": {},
        }, [660, 200], tv=TV["extract"], onError="continueRegularOutput"),
        node("Agent A — Reingest", "httpRequest", {
            "method": "POST",
            "url": "={{ $env.AGENT_A_URL }}/agentA/ingest",
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": "={{ JSON.stringify({ "
                        "filename: $('Download File').item.json.name || 'unknown', "
                        "driveFileId: $('Download File').item.json.id, "
                        "text: $json.text || '', "
                        "metadata: { source: 'nightly_reindex' } }) }}",
            "options": {"timeout": 300000, "batching": {"batch": {"batchSize": 2, "batchInterval": 1000}}},
        }, [880, 200], tv=TV["http"], retryOnFail=True, maxTries=2, onError="continueRegularOutput"),

        # Stale-lead nudge, same schedule, independent branch.
        node("Read CRM", "googleSheets", {
            "resource": "sheet",
            "operation": "read",
            "documentId": {"__rl": True, "value": "={{ $env.SHEET_CRM_ID }}", "mode": "id"},
            "sheetName": {"__rl": True, "value": "CRM", "mode": "name"},
            "options": {},
        }, [220, 460], tv=TV["sheets"], retryOnFail=True, maxTries=3),
        node("Stale Over 7 Days", "code", {"jsCode": (
            "// A lead with no next step booked and no touch in a week needs a nudge.\n"
            "const WEEK_MS = 7 * 24 * 60 * 60 * 1000;\n"
            "const now = Date.now();\n"
            "return $input.all()\n"
            "  .filter(i => {\n"
            "    const row = i.json;\n"
            "    if (['Won', 'Lost'].includes(row.Stage)) return false;\n"
            "    if (row.NextStepDate) return false;\n"
            "    const ts = Date.parse(row.Timestamp);\n"
            "    return Number.isFinite(ts) && (now - ts) > WEEK_MS;\n"
            "  })\n"
            "  .map(i => ({ json: i.json }));"
        )}, [440, 460], tv=TV["code"]),
        node("Nudge Owner", "httpRequest", {
            "method": "POST",
            "url": "=https://graph.facebook.com/v18.0/{{ $env.WHATSAPP_PHONE_ID }}/messages",
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "facebookGraphApi",
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": "={{ JSON.stringify({ messaging_product: 'whatsapp', to: $json.Owner, "
                        "type: 'text', text: { body: 'No movement on *' + $json.Company + "
                        "'* for over a week. Want to schedule a follow-up?' } }) }}",
            "options": {},
        }, [660, 460], tv=TV["http"], retryOnFail=True, maxTries=3, onError="continueRegularOutput"),
    ]
    connections = merge(
        chain("Nightly 02:00", "List KnowledgeBase", "Download File", "Extract Text",
              "Agent A — Reingest"),
        chain("Nightly 02:00", "Read CRM", "Stale Over 7 Days", "Nudge Owner"),
    )
    return workflow("Nightly Reindex", nodes, connections)


# --- 4) error_channel --------------------------------------------------------

def build_error_channel():
    """Central failure handler.

    Set as the Error Workflow on the other three (Workflow Settings -> Error
    Workflow), so any unhandled failure lands in the Conversations sheet with
    its requestId and reaches the operator on WhatsApp.
    """
    nodes = [
        node("On Failure", "errorTrigger", {}, [0, 300], tv=TV["errorTrigger"]),
        node("Shape Error", "code", {"jsCode": (
            "const e = $input.first().json;\n"
            "const ex = e.execution || {};\n"
            "const wf = e.workflow || {};\n"
            "return [{ json: {\n"
            "  Timestamp: new Date().toISOString(),\n"
            "  User: 'system',\n"
            "  Intent: 'error',\n"
            "  Input: wf.name || 'unknown workflow',\n"
            "  Output: ex.lastNodeExecuted || '',\n"
            "  MessageId: 'err-' + (ex.id || Date.now()),\n"
            "  Error: (ex.error && (ex.error.message || ex.error.description)) || 'unknown error',\n"
            "  Url: ex.url || '',\n"
            "} }];"
        )}, [220, 300], tv=TV["code"]),
        sheet_append("Log Error", "SHEET_CONVERSATIONS_ID", "Conversations",
                     [440, 300], matching=["MessageId"]),
        node("Alert Operator", "httpRequest", {
            "method": "POST",
            "url": "=https://graph.facebook.com/v18.0/{{ $env.WHATSAPP_PHONE_ID }}/messages",
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "facebookGraphApi",
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": "={{ JSON.stringify({ messaging_product: 'whatsapp', "
                        "to: $env.OPERATOR_WHATSAPP_NUMBER, type: 'text', "
                        "text: { body: '⚠️ ' + $json.Input + ' failed at ' + $json.Output + "
                        "'\\n' + $json.Error } }) }}",
            "options": {},
        }, [660, 300], tv=TV["http"], onError="continueRegularOutput"),
    ]
    connections = chain("On Failure", "Shape Error", "Log Error", "Alert Operator")
    return workflow("Error Channel", nodes, connections)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    built = {
        "whatsapp_router.json": build_router(),
        "drive_watch.json": build_drive_watch(),
        "nightly_reindex.json": build_nightly_reindex(),
        "error_channel.json": build_error_channel(),
    }
    for filename, wf in built.items():
        path = OUT / filename
        path.write_text(json.dumps(wf, indent=2, ensure_ascii=False) + "\n")
        print(f"{filename:26} {len(wf['nodes']):2d} nodes")


if __name__ == "__main__":
    main()
