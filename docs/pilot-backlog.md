# Backlog: from demo to a two-region, three-branch pilot

This repository is a single-facility demo. The product intent recovered from
earlier conversations is broader: employee document administration (medical
certificates, leave forms, shift forms, as PDF and Excel) across about 120
branches and 20,000 staff, on-premises and open source. This file lists what
a pilot needs, what is decided, what is not, and how each item will be
checked. Nothing here is implemented yet unless it says so.

Pilot shape assumed below: Region 1 with branches B1 and B2, Region 2 with
branch B3, one HR group with organisation-wide access.

## Open decisions (block design work)

| # | Question | Why it matters |
|---|---|---|
| D1 | One central Paperless instance, or one per branch with transfer to head office? "Fortnightly transfer to head-office archive" suggests branch-local storage; Paperless permissions only work within one instance. | Decides where access control, search and AI run, and what "transfer" means. |
| D2 | Exact access rules: may regional managers change documents or only view? Which HR staff are "authorised", for which document types (medical certificates are health information)? Can branch administrators see other branches' staff who work across branches? | Drives the group and permission model (B3). |
| D3 | Which system is authoritative for each person's role and branch: Entra groups, Entra attributes, or the HR system? Who maintains it, and how fast must a transfer between branches revoke access? | Drives sign-in mapping (B4) and revocation tests. |
| D4 | Volume: is "500 files per branch daily" a planning figure for every branch? 120 x 500 = 60,000 files a day, about 840,000 a fortnight and roughly 150 million over seven years. Average file size is unknown. | Decides database, storage, OCR and indexing capacity (B10). |
| D5 | Retention per document type. Seven years is a planning horizon, not a legal rule; the records/privacy team must set retention classes and legal-hold handling. | Retention controls (B9). |
| D6 | Do branch copies remain after a verified transfer, and for how long? | Archive design (B8). |
| D7 | Is AI allowed on health information at all, which model, on what hardware? Capacity has not been established. | AI scope (B11, B12). |

## Backlog

Priority P0 = before any real document is loaded; P1 = needed for the pilot;
P2 = needed before scaling past the pilot. "Repo" means it can be built and
tested in this repository with Docker; "Infra" means it needs systems that are
not available here (Entra tenant, branch servers, head-office storage, GPU).

### B1 (P0, Repo) Integration test harness against a disposable Paperless-ngx 3.4.2

- Compose project with throwaway volumes, started in CI; seeded with
  `seed/setup_demo.py` (allowed host: the compose service name).
- A stub OpenAI-compatible server for chat and embeddings so AI tests are
  deterministic and offline.
- Acceptance: for each demo user, assert the documents visible in
  `/api/documents/`, full-text search results (`query=`), access to
  `/download/`, `/preview/`, `/thumb/`, notes and share links, and the
  references returned by `/api/documents/chat/` contain only permitted ids.
  Re-run the seeder and assert no duplicates. Kill the seeder between upload
  and permission assignment and assert the document is invisible to demo
  users until the re-run (upstream assigns the uploader as owner).
- Replaces the current in-memory fake as the evidence for access control.

### B2 (P0, Repo) Revocation and precedence tests

- Remove a user from a group, change a document's groups, delete a document,
  move a document to trash: assert search, download and AI chat stop
  returning it immediately (upstream filters chat retrieval by permitted ids
  at query time; verify, including streaming responses through nginx).
- Current-version precedence: decide the mechanism (Paperless document
  versions, removing superseded documents from general access, or a filter)
  and test that a question answered by both v3 and v4 cites v4. Tags alone do
  not do this today.

### B3 (P0, Repo after D1/D2) Branch, region and HR permission model

- Groups per scope, e.g. `branch-B1`, `branch-B1-admins`, `region-R1`,
  `hr-org`; every document gets view groups {its branch, its region, HR} and
  change groups {its branch admins, HR} (adjust to D2).
- Assignment at import time (the loader's client or a Paperless workflow),
  never by tags or saved views.
- Acceptance: permission matrix test over 2 regions x 3 branches x roles,
  including a user moved from B1 to B3.

### B4 (P1, Infra: Entra test tenant) Microsoft Entra sign-in

- `PAPERLESS_APPS=allauth.socialaccount.providers.openid_connect`,
  `PAPERLESS_SOCIALACCOUNT_PROVIDERS`, `PAPERLESS_SOCIAL_ACCOUNT_SYNC_GROUPS`
  (groups must exist in Paperless with the same names as the claim values;
  Entra emits group object ids by default, and large memberships hit the
  groups overage limit, so app roles may be the better claim),
  `PAPERLESS_DISABLE_REGULAR_LOGIN`, a documented break-glass admin.
- Acceptance: sign-in with test identities for each role; group membership
  updates on next login; removed user loses access (B2 tests).

### B5 (P0, Repo + Infra) Security baseline

- HTTPS only, secrets outside the repo, MFA for local accounts, backup and
  tested restore (`document_exporter` / `document_importer`), audit log
  retention, upgrade procedure with image digests.
- AI disabled for health-information document types until D7 is decided.

### B6 (P1, Repo) Employee and branch metadata

- Custom fields: employee id, branch code, document category, retention
  class, source system key (the import-key pattern from the loader).
- Validation against an HR export (who exists, which branch); reject or
  quarantine documents for unknown employees.

### B7 (P1, Repo) Ingestion including Excel

- Enable Tika and Gotenberg (`PAPERLESS_TIKA_ENABLED`; without them
  Paperless-ngx 3.4.2 rejects `.xlsx`), test leave/shift spreadsheets for
  text extraction and search.
- Per-branch consume folders or an API importer reusing the loader's bounded
  client and import keys; decide duplicate policy
  (`PAPERLESS_CONSUMER_DELETE_DUPLICATES`, off by default in 3.x).

### B8 (P1, Infra after D1/D6) Fortnightly archive transfer and recovery

- Export with checksums, encrypted transfer to head office, verified import
  or object storage with integrity check, transfer log.
- Acceptance: restore drill from head-office copy to an empty instance with
  permissions intact; tampered or partial transfer detected.

### B9 (P1, Repo after D5) Retention controls

- Retention class per document type, scheduled report of documents past
  retention, deletion only after approval, legal hold that blocks deletion,
  audit trail of disposals.

### B10 (P2, Infra) Scale evidence

- PostgreSQL instead of SQLite, worker sizing, storage estimate from real
  file sizes.
- Load test with synthetic documents: one branch-day (500), one branch
  fortnight (7,000), and the D4 projection; measure consumption throughput,
  OCR time, search latency, AI index build time and size.

### B11 (P2, Repo) Grounded answers and citations

- Question set with expected source documents; measure citation accuracy and
  refusal when no permitted source exists; check the trailer references
  against what the user may open.

### B12 (P2, Infra) Local model hardware

- Benchmark candidate open-weight models and embedding models on the target
  hardware for latency, memory and answer quality before any purchase.

## Done in this repository (demo only)

- Loader: stable import keys, bounded HTTP, recovery of interrupted imports,
  refusal to modify unrelated or remote instances, dry run.
- Configuration: `.env` embedding choice honoured, images pinned, config
  checker, CI workflow. Evidence is unit-level against a fake API (see
  README "Development and tests").
