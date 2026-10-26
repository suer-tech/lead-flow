# Lead Flow: OCR commercial validation

Owner priority (26 October 2026): establish WHO has a costly unsolved problem,
WHAT they would buy and WHY our offer wins, before optimising email volume.
Read research/product-brief.json, competitor-baseline.json,
customer-review-evidence.json and research-policy.json before revising research
or outreach prompts. Vendor documentation is evidence of a claim, not proof of
quality. Existing PAU automation does not disqualify customers with real failures.

Russia and Russian-language companies first. Main offer: bounded implementation
under client documents/workflow; test API demand separately. Sender chosen by
owner: ai.studio.ufa@mail.ru; credentials have not been supplied. Do not claim a
live mailbox integration, active schedule or production deployment from local tests.

The checkout was cloned from suer-tech/lead-flow. The existing Vercel site has
its own private production PostgreSQL/configuration; never seed or overwrite it.
All development tests use isolated databases. New automatic demo seeding is off.
Preserve existing leads and manual notes/stages. Refusals need suppression even
when archived. Transport acceptance is not delivery; uncertainty never retries.

worker/ uses the existing Iva research lease/readers and separate checkpoints.
Do not resume Iva's paused general demand-research job as a side effect. Server
changes must follow E:/Projects/main_agent/AGENTS.md and its deployment docs.
The worker/native Iva control are now installed on the VPS. Read actual timer
and config state; do not infer activation from templates. First native preview
read 5 of 7 sources and rejected 3 candidates. SMTP/IMAP remain unverified and
disabled without mailbox credentials. PostgreSQL acceptance passed 27 gates
on an isolated database. Backups/retention are in maintenance.py; completed raw
source bodies may compact after 14 days while evidence/checkpoints survive.
