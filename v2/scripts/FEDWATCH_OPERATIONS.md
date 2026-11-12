# Secure FedWatch fetch

Run the Python helper normally, not inside `pplx credentials run`. It wraps only
the OAuth token POST to `auth.cmegroup.com`, captures the provider-issued token
in memory, then makes data requests from the normal parent process to
`markets.api.cmegroup.com`. No proxy settings are changed. The token is passed
through curl configuration stdin, never command arguments, logs or disk.

## Fetch and validate

```sh
python3 v2/scripts/fedwatch_secure.py \
  --previous /absolute/path/to/last-observed-raw-payload.json \
  --expected-reporting-date YYYY-MM-DD \
  --output /absolute/path/to/new-run-fedwatch.json
```

Use the calendar-verified latest expected CME reporting date, not the fetch
date. Each forecast must match that reporting date; front-meeting fields and
date must advance versus the supplied baseline. Probability sums are checked.
Unchanged data, a rolled front meeting without a matching baseline, or missing
context are DEGRADED and require review. Never substitute cached probabilities.
On holidays/weekends, document the publication-calendar exception rather than
changing a date or claiming unchanged data is a fresh update.

The default exact handle is the corrected CME credential ending in UUID
`90967ca7-5ef4-400d-a1d1-63ed968b9ae8`. `CME_CREDENTIAL_HANDLE` or `--handle` may
select a newly listed, authorized exact credential. Never fall back to the old
rejected entry. The corrected user-scoped entry worked in the persistent
scheduled thread on November 12, 2026 at 11:01:46 UTC: four forecasts were
retrieved, but freshness failed because all remained on the November 10 cohort.
Evidence: `cron_tracking/b28120e0/run240/fedwatch_raw.json` and `integrity.json`.
This does not establish access in other project/worker contexts. If a scheduler
reports the handle unavailable, ask for the same correct credential to be saved
to Trading Project scope. Do not request a different password based only on
scope failure, and do not confuse successful access with fresh forecast data.

## Phase 1 integration

`fetch_phase1.fetch_fedwatch` uses the same helper. Before calling it, provide
the independently verified current policy-band lower bound in basis points,
expected reporting date, and previous raw payload path as parameters or:

- `FEDWATCH_CURRENT_LOWER`
- `FEDWATCH_EXPECTED_REPORTING_DATE`
- `FEDWATCH_PREVIOUS_PATH`

There is no default policy band: the old June-2026 constant was stale.
The raw helper does not label buckets hold/cut/hike and does not require a
policy-band assumption. Fetch all data once and reuse the validated snapshot
for the website/email rather than refetching during composition.

## Validation

```sh
python3 -m unittest discover -s v2/scripts -p 'test_fedwatch_secure.py' -v
```

These are offline regression tests, not proof of live vendor access.
Live recovery evidence on 2026-11-11 is retained in the automation's
`cron_tracking/b28120e0/fedwatch_recovery_2026-11-11.json`.
