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
The worker/native Iva control are installed on the VPS. On 2026-10-26 config is
enabled=false, mode=preview; the research timer is disabled/inactive.
First native preview read 5 of 7 attempted URLs and rejected 3 candidates. Second
cycle 1d108003540a48708b8458474074d4d7 used 17 model calls, read 17 of 20 attempted
URLs and rejected 4 candidates; no letters were prepared or sent in either run.
The v2 audit also found overly long/altered source quotes and dropped footer
contacts. Iva's opt-in contact reader is installed and passed 102 server tests;
real direct/VPN reads proved unchanged ArbitrKIT PAU body plus its footer contact.
Default remains off, and contact fragments cannot prove identity or demand.
Worker pipeline4 bounded quote repair is still being tested.
Do not interpret all rejected candidates as proven lack of customer need.
Do not equate attempted URLs with readable evidence, or inactive oneshot with failure.
CRM PR1 merged as f8eb62a, but CRM_AGENT_TOKEN commissioning is still pending;
do not claim server export connected. SMTP/IMAP remain disabled and unverified
without mailbox credentials. PostgreSQL acceptance passed 27 gates on an isolated
database. Backups/retention are in maintenance.py; 31 backup/restore gates passed
on an isolated online copy. Completed raw bodies may compact after 14 days while
evidence/checkpoints survive. First production maintenance succeeded at 15:10:14
Yekaterinburg on 2026-10-26: one DB, zero compacted/pruned/model calls, exit0;
backup /opt/iva/data/lead-flow/backups/lead-flow-20261026-101014.tar.gz. Maintenance
timer is enabled/active, next 2026-10-27 03:30 Yekaterinburg. Research remains off
while pipeline4 is being checked; future night backup is not yet proven.
