# CareHub

An open-source document hub for aged-care admin teams: one place to store,
find and ask questions about policies, forms, rosters, invoices and records,
with access controlled by role.

Built on [Paperless-ngx](https://docs.paperless-ngx.com) 3.4.2 (GPL-3.0), with
no subscriptions. The only running costs are the computer it runs on and,
optionally, a hosted AI model.

## What it does

- **Finds the right document fast.** Full-text search, tags, document types,
  suppliers and dates, with scanned paper made searchable by OCR.
- **Lets staff ask questions in plain English.** For example: "Where is the fire
  assembly point?" or "Which staff are overdue for fire training?" Answers come
  from the documents and link to the sources. Current versions take precedence
  over superseded ones.
- **Enforces permission layers.** Each document is visible only to the groups
  allowed to see it, and **the AI follows the same rules**: a care worker
  asking about an invoice gets no figures, and an admin officer can't see
  resident care plans.
- **Keeps an audit log** of who changed what.

## Demo roles

| Group | Sees |
|---|---|
| All Staff | Policies, procedures, forms, rosters |
| Admin Officers | All of the above, plus invoices, contracts, minutes, training records and service reports |
| Care Managers | Policies, minutes, training, plus confidential resident records |
| Finance | Invoices, contracts, budget |

Demo users: `demo.manager` (all groups), `demo.admin` (Admin Officers),
`demo.carer` (All Staff). The demo data in `seed/` is entirely fictional
("Bluegum Ridge Care Community").

## Run it

Requirements: Docker with Compose, and about 1.5 GB of free RAM.

```bash
cp .env.example .env      # then fill in the values
docker compose up -d      # web app on 127.0.0.1:8040
```

Load the fictional demo (optional):

```bash
docker run --rm --network host --env-file .env -v "$PWD/seed:/seed" -w /seed python:3.12-slim \
  sh -c "pip install -q reportlab requests && python make_docs.py out && python setup_demo.py out"
```

Publish it behind a reverse proxy with HTTPS (nginx and certbot, or Caddy), and
set `PAPERLESS_URL` to the public address.

## AI model options

| Setup | Where documents go | Notes |
|---|---|---|
| Claude API (online demo) | Text of the documents relevant to a question is sent to Anthropic | Fast, high-quality answers. Search indexing still runs locally. |
| Ollama on a Mac mini or GPU server | Nowhere: fully in-house | Mac mini with 48–64 GB memory recommended. See the Option B block in `.env.example`. |

## Production checklist (beyond the demo)

- Single sign-on with the organisation's Microsoft Entra ID (OIDC is supported)
- Automated encrypted backups (`document_exporter`) with off-site copies
- Retention rules for records, agreed with privacy and records teams
- Security review by IT before any real data is loaded
- A named owner in IT for updates and support
