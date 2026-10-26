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
enabled=true, mode=preview; the research timer is enabled/active. Admission returned
daily_cycle_already_completed with zero model calls after activation. Today's
slot is terminal; next new search is 2026-10-27 09:00 Yekaterinburg, no pending loop.
First native preview read 5 of 7 attempted URLs and rejected 3 candidates. Second
cycle 1d108003540a48708b8458474074d4d7 used 17 model calls, read 17 of 20 attempted
URLs and rejected 4 candidates; no letters were prepared or sent in either run.
The v2 audit also found overly long/altered source quotes and dropped footer
contacts. Iva's opt-in contact reader is installed and passed 102 server tests;
real direct/VPN reads proved unchanged ArbitrKIT PAU body plus its footer contact.
Default remains off, and contact fragments cannot prove identity or demand.
Worker pipeline4 is installed and passed 70 VPS worker/HTTP/probe tests. A first
cached native quote probe failed its long identity quote after one call. With
explicit feedback, a second passed quote repair and independent research review
in six calls: 30->6 / 29->12 words, only saved sources, production unchanged.
The quote probe's two letter attempts failed review. A later isolated letter-only
probe b09e37e9f2da4d099bfd79eb3dadc954 passed with one local draft: three model
calls (two author, one reviewer), all eight flags true. Provenance is pinned from
the checked assessment; author returns prose only. MYFORCE stays needs_review/
intent0, source/assessment/critic unchanged; no CRM/SMTP or new research. Production
v2 remains four rejected/zero drafts. The renderer subsequently lost a duplicate
fixed criteria sentence without another model probe. Full local pytest passed
136 tests/13 subtests; all 74 server tests passed after that cosmetic deployment
in 0.864s.
Do not interpret all rejected candidates as proven lack of customer need.
Do not equate attempted URLs with readable evidence, or inactive oneshot with failure.
CRM PR1 merged as f8eb62a, but CRM_AGENT_TOKEN access was approved; authenticated commissioning is still in progress;
do not claim server export connected. Host DNS resolves Vercel but direct HTTPS
fails (one TCP timeout, another TCP connection followed by TLS timeout);
existing iva-codex VPN public GET passes 200/verified TLS.
Upstream cause and authenticated API remain unverified; a network bridge is
local preparation, not installed/enabled. SMTP/IMAP remain disabled and unverified
without mailbox credentials. PostgreSQL acceptance passed 27 gates on an isolated
database. Backups/retention are in maintenance.py; 31 backup/restore gates passed
on an isolated online copy. Completed raw bodies may compact after 14 days while
evidence/checkpoints survive. First production maintenance succeeded at 15:10:14
Yekaterinburg on 2026-10-26: one DB, zero compacted/pruned/model calls, exit0;
backup /opt/iva/data/lead-flow/backups/lead-flow-20261026-101014.tar.gz. Maintenance
timer is enabled/active, next 2026-10-27 03:30 Yekaterinburg; future night backup
is not yet proven. General demand research remains paused.
