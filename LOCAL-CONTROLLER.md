# Local Viber controller

Reply voice research, private prompt setup and offline conversation evaluation are
documented in [REPLY-VOICE.md](REPLY-VOICE.md).

The running installation remains `C:\viber-cli`. PostgreSQL 18 runs in Docker,
listening only on `127.0.0.1:5433`, with a persistent volume and a 512 MB memory
limit. Run `docker compose up -d postgres`, then the existing
`scripts/start_outreach_host.ps1` launcher. The dashboard remains
http://127.0.0.1:4001/viber and the email service remains on port 4000.

The controller supports `instance-1` and `instance-2`, bound to ViberWorker1 and
ViberWorker2 after each native phone number is verified. The archived `current`
account stays disabled and its contacts are ignored. Contacts are exclusively owned by
normalized phone number. The controller holds lifetime database locks; an
expired heartbeat never authorizes replacing a worker that might still run.
The saved machine, process ID and process start time must prove the old process
stopped, or the previous controller must have completed graceful shutdown.

Jobs persist in PostgreSQL. Each account executes one Viber operation at a time.
Codex replies have priority over outreach; earlier work for the same phone
stays ahead of later work. A future due time or account cooldown stays in the
database. An uncertain send blocks further work for that recipient and is never
retried automatically. Campaign approval still authorizes its reviewed batch.
After checking an uncertain send in Viber and Sent, explicitly resuming that
conversation permits future turns. The uncertain attempt remains recorded and
cannot be approved or retried as the same job.

Codex uses the configured model with `low` reasoning for Viber drafts only.
Two conversations can generate concurrently. Full retained context, isolated
structured output, recipient checks, and dispatch markers are preserved.
Incoming messages settle for one second; ingestion wakes generation. Completed
AI replies send automatically through the durable account queue when replies
are enabled. Verified replacement accounts start enabled, and updates/restarts
preserve the saved setting. Only an explicit owner pause disables replies.
Changes to the conversation, recipient, owner, or instructions
cancel an outdated reply before dispatch. Generation and queue times are shown
separately. Manual messages and outreach campaigns still require review.

Routine sales assumptions continue as replies. In Inbox, **Questions for you and
saved rules** shows the assumption and a private question about next time.
Answering does not delay a valid automatic reply. Saved answers persist per account,
become trusted business instructions for future generations, and invalidate older
replies. **Regenerate and send** generates a fresh reply for an unsent stale turn;
recipient/history checks still apply. One open question is retained per topic.
The owner-confirmed catalog has 400 projects; generation receives at most four
examples from a relevant category. Run `scripts/validate_reply_feedback.py` for
synthetic model checks without sending customer messages.

`POST /worker/register`, `/worker/heartbeat`, `/worker/claim`, `/worker/result`,
and `/worker/incoming` require a bearer token and account_id, worker_id, epoch.
Registration refreshes an existing authenticated identity. Provisioning another
account and confirming the previous process stopped is an operator action;
the network endpoint cannot authorize replacement. Future workers require their
own isolated Viber desktop or VM and source binding. Ten GUI instances cannot
share one desktop safely. This machine runs the two configured replacement VMs;
each account becomes active after its new number is linked and verified.

Verification: `python -m unittest discover -s tests`. To run ledger fixtures
against PostgreSQL, set `VIBER_TEST_POSTGRES_URL`; each fixture uses its own schema.
The suite includes 1,000 jobs across ten simulated workers, with no real sends.
`python scripts/benchmark_replies.py --repeats 3` compares identical synthetic
prompts at xhigh and low and checks output quality, median, and p95 latency.

## Cutover and rollback

Routine updates preserve saved reply enablement, wait for active operations,
and gracefully stop the controller before replacing files. The initial SQLite
cutover is complete. For an explicitly requested database rollback, use SQLite's backup API and
retain the old launcher and source. Import the offline backup using
`scripts/migrate_postgres.py --enable-drafts`; counts, native checkpoints, and
send states must match before activation. Imported history is below the fresh
reply boundary and cannot produce drafts.

Before rollback, pause new work and stop the PostgreSQL controller after active
work ends. Compare every post-cutover web_sends record with Viber, including
SUBMITTING/UNKNOWN outcomes. Reconcile those IDs and states into the SQLite
ledger before restoring the previous source/launcher. Never revert a stale
SQLite backup and resend pending work. Keep the PostgreSQL volume and backup
until reconciliation is complete. Do not delete the volume as a rollback step.
