# Privacy Policy — WhatsApp Revenue Copilot

_Last updated: 29 September 2026_

This is a demonstration project. It is not a commercial service and is not
offered to the public.

## What this application is

WhatsApp Revenue Copilot is a self-hosted assistant. An operator runs it on
their own machine, connects it to their own Google Workspace and their own
WhatsApp Business test number, and messages it themselves. There is no hosted
service, no shared instance and no third-party users.

## What data is processed

When a message is sent to the connected WhatsApp number, the application
processes:

- the message text, and any document attached to it
- the sender's WhatsApp phone number
- the WhatsApp message id

Documents placed in the operator's own Google Drive folder are read so their
text can be indexed and searched.

## Where that data goes

| Destination | What is sent | Why |
|---|---|---|
| The operator's Google Drive, Sheets and Calendar | messages, documents, extracted lead details, calendar events | to store the operator's own records in their own account |
| The configured language-model provider | message text and retrieved document excerpts | to classify intent and generate answers |
| A vector database on the operator's machine | document text and embeddings | to answer questions from the operator's documents |

No data is sold, shared with advertisers, or transferred to any party beyond
those listed above.

## Retention and deletion

All data is held in the operator's own Google account and on their own machine.
Nothing is retained by the project's authors, who have no access to any running
instance. Deleting the Drive files, Sheets rows and local containers removes
everything the application holds.

## Access and control

Because every instance is self-hosted, the operator has direct control over all
stored data and can inspect, export or delete it at any time through Google
Drive, Google Sheets, Google Calendar and their local environment.

## Contact

Questions about this project can be raised as an issue at
<https://github.com/iamabhisheksaini/whatsapp-copilot>.
