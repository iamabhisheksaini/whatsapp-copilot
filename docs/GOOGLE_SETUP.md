# Google setup

You need a Google Cloud project, three OAuth credentials in n8n, two Sheets, two
Drive folders and one Docs template. Budget about 30 minutes the first time.

Nothing here can be automated on your behalf — OAuth consent requires you to
sign in as yourself.

---

## 1. Google Cloud project and APIs

1. Open <https://console.cloud.google.com/> → **New Project** → name it
   `whatsapp-copilot`.
2. **APIs & Services → Library**, enable all four:
   - Google Drive API
   - Google Sheets API
   - Google Calendar API
   - Google Docs API

> Missing the **Docs API** is the usual cause of the proposal step failing with
> a 403 — the copy succeeds and the merge does not.

## 2. OAuth consent screen

1. **APIs & Services → OAuth consent screen** → **External** → Create.
2. App name `WhatsApp Copilot`, your email for both support and developer
   contact. Save and continue.
3. **Scopes** → Add these four, and no more — least privilege is part of the
   grading:

   | Scope | Why |
   |---|---|
   | `.../auth/drive.file` | Only files the app creates or opens. Not full Drive. |
   | `.../auth/spreadsheets` | Read and write the Conversations and CRM sheets. |
   | `.../auth/calendar.events` | Create events. Not calendar settings. |
   | `.../auth/documents` | Merge fields into the proposal template. |

4. **Test users** → add your own Google account. While the app is in *Testing*
   only listed users can authorise it, which is what you want.

## 3. OAuth client

1. **Credentials → Create Credentials → OAuth client ID → Web application**.
2. Name it `n8n`.
3. **Authorised redirect URI** — n8n shows you the exact value to use. Open n8n
   → **Credentials → New → Google Drive OAuth2 API** and copy the *OAuth
   Redirect URL* it displays. For a local instance it is:

   ```
   http://localhost:5678/rest/oauth2-credential/callback
   ```

   If you are tunnelling through ngrok, use the tunnel host instead, and make
   sure `WEBHOOK_URL` in `.env` matches.
4. Save, then copy the **Client ID** and **Client Secret**.

## 4. Credentials in n8n

Create four credentials in n8n, all using the same client ID and secret:

| Credential type | Used by |
|---|---|
| Google Drive OAuth2 API | Upload, copy, search, download |
| Google Sheets OAuth2 API | Conversations and CRM writes |
| Google Calendar OAuth2 API | Event creation |
| Google Docs OAuth2 API | Proposal field merge |

For each: paste the client ID and secret, click **Sign in with Google**,
authorise, and confirm it shows *Connected*.

Then open each imported workflow and select the credential on every Google
node — the committed JSON deliberately ships without credential references.

## 5. Drive folders and the proposal template

1. In Drive, create two folders: `KnowledgeBase` and `Proposals`.
2. Create a Google **Doc** called `Proposal Template` in `Proposals`, containing
   exactly these three placeholders (the merge step does a literal
   find-and-replace):

   ```
   {{TITLE}}

   {{SUMMARY}}

   {{BULLETS}}
   ```

   Style it however you like — formatting is preserved, only the placeholder
   text is swapped.
3. Grab the three ids from their URLs:
   - Folder: `https://drive.google.com/drive/folders/<FOLDER_ID>`
   - Doc: `https://docs.google.com/document/d/<DOC_ID>/edit`

## 6. Sheets

Create two spreadsheets. **The header row must match exactly** — the workflows
map columns by name, so a typo silently writes a blank column.

**`Conversations`** — first tab named `Conversations`:

```
Timestamp | User | Intent | Input | Output | Confidence | Citations | MessageId | Error | Url
```

**`CRM`** — first tab named `CRM`:

```
Timestamp | LeadId | Name | Company | Intent | Budget | Stage | Owner | NextStepDate | Domain | QualityScore | Notes | Links
```

`MessageId` and `LeadId` are the idempotency keys. WhatsApp retries deliveries,
and without a matching column a retry appends a duplicate row instead of
updating the existing one.

## 7. Fill in `.env`

```bash
DRIVE_KNOWLEDGEBASE_FOLDER_ID=...
DRIVE_PROPOSALS_FOLDER_ID=...
DRIVE_PROPOSAL_TEMPLATE_ID=...
SHEET_CONVERSATIONS_ID=...
SHEET_CRM_ID=...
CALENDAR_ID=primary
OPERATOR_WHATSAPP_NUMBER=91XXXXXXXXXX
```

Then restart so n8n picks them up:

```bash
docker compose up -d n8n
```

Verify inside the container:

```bash
docker exec n8n printenv | grep -E 'SHEET_|DRIVE_|CALENDAR_'
```

## 8. Point the workflows at the error channel

1. Open **Error Channel**, copy its workflow id from the URL.
2. Add `ERROR_WORKFLOW_ID=<id>` to `.env`, restart n8n.
3. In each of the other three workflows: **Settings → Error Workflow → Error
   Channel**.

---

## Checks

| Symptom | Cause |
|---|---|
| `403 insufficient permissions` on Drive | Scope missing; re-authorise after adding it |
| Proposal copies but stays templated | Docs API not enabled |
| Blank Sheets columns | Header text does not match the table above |
| `$env.X` renders empty | Var not in the `n8n` service in `docker-compose.yml`, or n8n not restarted |
| Duplicate rows on retry | Matching column missing from the header row |
