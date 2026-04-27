# Virtual Trader Scheduler

Cloudflare Worker Cron scheduler for the Railway-hosted virtual trader backend.

## Schedule

Cloudflare cron expressions are UTC. This config uses weekday names to avoid numeric day-of-week ambiguity.

- `*/5 * * * *` runs every 5 minutes every day.
- The Worker sends `start` only during `08:55` to `09:20` IST.
- The Worker sends `renew` during `11:30` to `12:30` IST so 24-hour Dhan tokens are renewed before early-afternoon expiry.
- The Worker also sends `renew` during `15:30` to `15:39` IST so the token is refreshed before shutdown when possible.
- The Worker sends `stop` only during `15:40` to `15:55` IST.

The Worker checks the actual IST time before sending an action. If Cloudflare fires outside the allowed windows, it returns a no-op and does not call the backend.

## Secrets

Required for current deployment:

- `GITHUB_TOKEN`: GitHub token with `workflow` permission. Used to call the existing `railway-scheduler.yml` workflow with `workflow_dispatch`.
- `SCHEDULER_SECRET`: Bearer token for manual `POST /run` testing.

Optional future direct mode:

- `RAILWAY_API_TOKEN`: If set, the Worker calls Railway GraphQL directly and skips GitHub dispatch.

## Manual Test

```bash
curl -X POST "https://virtual-trader-scheduler.<subdomain>.workers.dev/run?action=status" \
  -H "Authorization: Bearer $SCHEDULER_SECRET"
```

```bash
curl -X POST "https://virtual-trader-scheduler.<subdomain>.workers.dev/run?action=renew" \
  -H "Authorization: Bearer $SCHEDULER_SECRET"
```
