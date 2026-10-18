# Shared regional catalogs

The production and local worker entry point uses `SharedMonitor` and `SharedStore`.
Each active Vinted region has one durable catalog cursor, independent of brands,
models and keyword filters. One fetch supplies all active subscriptions to that
region. Different countries still have distinct catalogs; this is not one global
Vinted export. No active valid subscriptions means no catalog request.

Results are matched locally with the existing predicates, exclusion rules,
subscriptions, personal Start/Stop, publication age (60 seconds), and delivery
deduplication. Up to four user groups process a page concurrently per region.
Within a user's group the filters run sequentially to protect shared delivery
transactions. Matching different filters does not create extra catalog fetches;
item detail requests are still sometimes necessary and consume budget.

`regional_catalogs` stores the cursor and request lease; `regional_identities`
stores observed IDs for traversal. Both tables are added by the existing migration.
Existing jobs retain baseline eligibility and seen-item/delivery history. Their
old derived query and checkpoint are retained in `scheduling.previous_catalog`.
New subscribers establish their own quiet baseline. A page interrupted by a
restart, expired lease or failed subscriber can be fetched again; delivery
transactions remain idempotent. Source and proxy-budget pauses apply to the feed.

The existing shared proxy limiter still stops at 36 billion locally counted
bytes per month, leaving 4 billion of a 40-billion-byte allowance as reserve.
Daily limits remain 36 billion divided by the days in the month, Europe/Kyiv,
starting November 2026. The local counter is not a DataImpulse invoice.
An unfiltered catalog may receive more new items than the available budget can
cover: sharing requests avoids multiplication by filters, but does not guarantee
complete discovery or a fixed delay at 40 GB. Old or unverified items remain
subject to the same age rules. No live traffic measurement was run for this change.

## Local installation and publishing

Use `tools/export_github.py --output <new-directory>` from the working project
with Python 3.12+. It creates an exclusive clean source directory and hash manifest.
It excludes `.env`, databases, browser profiles, logs, private reports, backups,
SSH keys and Git history. It checks known local credentials without printing them
and refuses common embedded token/private-key patterns. This is a source export,
not a copy of production users or saved searches. Commit the exported directory.
The original working directory still contains private runtime files.

The normal Windows setup is documented in the root README. The local gateway
now builds the same Byparr Dockerfile as the server, including the budget/network
modules, and stores its own accounting in a persistent Docker volume. Local and
server budgets are separate: do not run both against one paid allowance expecting
a combined 40 GB cap. Do not reuse a production bot token for simultaneous polling.
A fresh budget store after its configured start date fails closed until its
initial balance is deliberately established; copying source cannot reset spend.

Offline integration tests: `services/worker/tests/test_shared_catalog.py` uses an
isolated PostgreSQL, synthetic catalogs and a fake Telegram sender. It covers four
countries, different brands/keywords/users, delivery deduplication, Stop during
fetch, quiet subscriber baseline, restart, concurrent claims, fan-out failures,
expired leases, budget pauses and more than 100 subscribing jobs.

## Verification, 2026-10-18

98 distinct worker integration/regression cases passed against isolated test
PostgreSQL and fake source/Telegram clients; five source-export tests passed.
One existing millisecond scheduling assertion failed during simultaneous test
workloads (41 ms versus a 39 ms bound); its complete 37-case group passed on the
isolated retry without changing that assertion or the scheduler. The local
Compose configuration also passed syntax validation. These checks do not measure
live Vinted throughput, end-to-end delay, or paid provider billing.
