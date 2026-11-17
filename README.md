# CareHub

An open-source document hub for aged-care admin teams: one place to store,
find and ask questions about policies, forms, rosters, invoices and records,
with access controlled by role.

Built on [Paperless-ngx](https://docs.paperless-ngx.com) 3.4.2 (GPL-3.0), with
no subscriptions. The only running costs are the computer it runs on and,
optionally, a hosted AI model.

**Status.** This repository is a demo for one fictional facility: Docker
Compose configuration for Paperless-ngx plus a loader for fictional sample
documents. It does not yet implement the multi-branch employee-document
system (branches and regions, Microsoft Entra sign-in, Excel ingestion,
archive transfer, retention). [docs/pilot-backlog.md](docs/pilot-backlog.md)
lists what that needs and which decisions are still open.

## What it does

- **Finds the right document fast.** Full-text search, tags, document types,
  suppliers and dates, with scanned paper made searchable by OCR.
- **Lets staff ask questions in plain English.** For example: "Where is the fire
  assembly point?" Answers are generated from document passages and list up to
  three source documents. Tags mark documents as "Current version" or
  "Superseded", but nothing forces the model to prefer the current one; it can
  cite a superseded policy.
- **Restricts documents by group.** The demo loader gives each document an
  owner and view/change permissions for specific groups. In Paperless-ngx
  3.4.2 the AI chat only retrieves from documents the asking user may view
  (upstream code; this repository does not test it against a running server
  yet). Tags such as "Confidential" are labels, not access controls.
- **Keeps an audit log** of who changed what (Paperless-ngx audit log, enabled
  in `docker-compose.yml`).

## Demo roles

| Group | Sees |
|---|---|
| All Staff | Policies, procedures, forms, rosters |
| Admin Officers | All of the above, plus invoices, contracts, staff meeting minutes, training records and service reports |
| Care Managers | Policies, minutes, training, plus confidential resident records |
| Finance | Invoices, contracts, budget |

Demo users: `demo.manager` (all groups), `demo.admin` (All Staff and Admin
Officers), `demo.carer` (All Staff). Their passwords come from
`DEMO_MANAGER_PASSWORD`, `DEMO_ADMIN_PASSWORD` and `DEMO_CARER_PASSWORD` in
`.env`. The demo data in `seed/` is entirely fictional ("Bluegum Ridge Care
Community").

## Run it

Requirements: Docker with Compose. The containers are limited to 2 GB (web
app) and 128 MB (broker); actual memory use has not been measured.

```bash
cp .env.example .env               # then fill in the values
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt -c constraints.txt
.venv/bin/python tools/check_config.py     # validates .env, shows the effective AI setup
docker compose up -d               # web app on 127.0.0.1:${CAREHUB_PORT:-8040}
docker compose ps                  # the web container reports "healthy" once it answers
```

`CAREHUB_PORT` changes the host port. A reverse proxy must point at the same
port (`proxy_pass` in `deploy/nginx-carehub.conf`, which is the live demo's
site file); `tools/check_config.py --nginx deploy/nginx-carehub.conf` checks
that. Publish it behind HTTPS (nginx and certbot, or Caddy) and set
`PAPERLESS_URL` to the public address.

Images are pinned by tag and digest in `docker-compose.yml`.

## Loading the demo

Only point the loader at a disposable instance or the CareHub demo itself: it
creates and updates groups, users and document permissions.

```bash
set -a; . ./.env; set +a           # loader settings: admin and DEMO_* passwords
.venv/bin/python seed/make_docs.py seed/out
.venv/bin/python seed/setup_demo.py --dry-run seed/out     # read-only preview
.venv/bin/python seed/setup_demo.py seed/out
```

On Linux you can run it in a container instead (host networking reaches the
loopback port; Docker Desktop on macOS/Windows needs the virtualenv route above):

```bash
docker run --rm --network host --env-file .env -v "$PWD/seed:/seed" -w /seed \
  python:3.12.16-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b \
  sh -c "pip install -q -r requirements.txt && python make_docs.py out && python setup_demo.py out"
```

What the loader guarantees:

- **It checks configuration first.** If the admin credentials or any of the
  three demo passwords are missing it stops before sending a request.
- **It only talks to loopback by default.** It targets
  `http://127.0.0.1:${CAREHUB_PORT}` unless `CAREHUB_API` says otherwise, and
  refuses any other host unless you name it with `--allow-host HOST`.
- **It will not take over an unrelated instance.** If the target already has
  documents, or groups/users with the demo names, and has no "CareHub import
  key" custom field (the marker this loader creates), it stops without
  changes. `--adopt-existing` overrides that; use it once, after `--dry-run`,
  for an instance seeded by the earlier loader that matched documents by title.
- **Re-runs are safe and do not duplicate documents.** Each document carries
  its manifest key in the "CareHub import key" custom field. A re-run finds
  documents by that key, finishes uploads that were interrupted (including
  one stopped between upload and permission assignment; until then the
  document is owned by the admin with no group access), and restores the
  manifest's owner and group permissions, removing other direct grants on
  those documents. A document with a demo title but no key is skipped and
  left untouched.
- **It reconciles groups and demo users** (group permission sets; demo users'
  names, flags and group membership). It sets passwords only when it creates
  a user, unless you pass `--reset-passwords`.
- **Every request is bounded.** HTTP calls time out (`CAREHUB_SEED_HTTP_TIMEOUT`,
  default 30 s), waiting for the API and for each upload to be processed
  has a deadline (`CAREHUB_SEED_READY_TIMEOUT`, `CAREHUB_SEED_TASK_TIMEOUT`,
  default 300 s each), and failures end with a message and a non-zero exit.
  Re-run the same command to resume.

Uploads still being processed when the loader stopped are remembered in
`seed/out/.carehub-import-state.json` so that the next run waits for them
instead of uploading again.

## AI model options

| Setup | Where document text goes | Notes |
|---|---|---|
| A: Claude API (online demo) | Passages relevant to a question are sent to Anthropic. Embeddings are computed on the server's CPU. | The embedding model is downloaded from Hugging Face on first use; set `HF_HUB_OFFLINE=1` afterwards. |
| B: Ollama on the Docker host or another in-house server | To the Ollama endpoint only | Chat and embeddings both use Ollama. Hardware and model sizes for this workload have not been benchmarked. |

Pick one block in `.env.example`. `tools/check_config.py` prints the
effective backends and endpoints under Docker Compose's precedence rules.
Settings saved on the app's Configuration page are stored in the database and
override the environment, so check there too. After changing the embedding
backend or model, rebuild the AI index:
`docker compose exec webserver document_llmindex rebuild`.

## Development and tests

```bash
.venv/bin/python -m pytest
```

The tests run the loader against an in-memory imitation of the Paperless-ngx
3.4.2 API (`tests/fake_paperless.py`) and check the Compose/env
configuration. They cover missing configuration, both AI profiles,
non-default ports, interrupted imports, re-runs, stalled and failing HTTP
calls, failed tasks and the target safeguards. They do **not** show that
Paperless-ngx enforces the permissions, search filtering or AI retrieval
filtering; that needs integration tests against a disposable instance (see
the backlog). CI (`.github/workflows/ci.yml`) also renders the Compose file
with `docker compose config`.

## Production checklist (beyond the demo)

- Single sign-on with the organisation's Microsoft Entra ID (Paperless-ngx
  supports OIDC and group sync; not configured here)
- Automated encrypted backups (`document_exporter`) with off-site copies,
  and a tested restore
- Retention rules for records, agreed with privacy and records teams
- Security review by IT before any real data is loaded
- A named owner in IT for updates and support

See [docs/pilot-backlog.md](docs/pilot-backlog.md) for the work towards a
multi-branch pilot.
