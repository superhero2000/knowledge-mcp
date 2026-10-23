# Deployment Process

All services are deployed through the "Lighthouse" pipeline. Manual deployments to
production are forbidden except during a declared incident.

## Release Trains

We ship on release trains. The train departs every Tuesday and Thursday at 10:00 CET.
Changes must be merged to `main` and pass the full test suite before 08:00 on the day of
departure to be included. Missing a train is fine; there is always another one.

## Canary Rollout

Every deployment starts as a canary on 5% of traffic for 30 minutes. Lighthouse watches
the error rate and the p99 latency. If the error rate rises by more than 0.5 percentage
points or p99 latency by more than 20%, the canary is aborted automatically.

After a healthy canary, traffic increases to 25%, 50% and then 100% in 15-minute steps.

## Rollback

To roll back, run `bw deploy rollback <service> --to <version>`. A rollback is always
allowed without approval, even outside release trains. Rollbacks complete in under
five minutes because previous container images are kept warm for 72 hours.

After a rollback, open a ticket in the RELIABILITY project and link the failed release.

## Feature Flags

Risky changes must be wrapped in a feature flag managed in the "Beacon" flag service.
Flags older than 90 days are reported weekly and must be removed or justified.

## Database Migrations

Schema migrations run in a separate step before the code deploy. Migrations must be
backwards compatible for at least one release (expand/contract pattern). Destructive
migrations (dropping columns or tables) require approval from a database owner.

## Freeze Periods

There is a deployment freeze from December 20 to January 6. During the freeze only
security fixes and rollbacks are allowed, and each needs sign-off from the duty manager.
