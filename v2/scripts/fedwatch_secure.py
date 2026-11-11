#!/usr/bin/env python3
"""Two-step CME OAuth; no vault secret or transient token is stored or logged."""
import argparse
import json
import math
import os
from pathlib import Path
import subprocess
from datetime import date, datetime, timezone
from urllib.parse import urlencode

DEFAULT_HANDLE = "custom-cred:90967ca7-5ef4-400d-a1d1-63ed968b9ae8@auth.cmegroup.com"
TOKEN_URL = "https://auth.cmegroup.com/as/token.oauth2"
API_BASE = "https://markets.api.cmegroup.com/fedwatch/v1"


class FedWatchError(RuntimeError):
    pass


def obtain_token(handle=None):
    """Only the token exchange runs inside the host-scoped credential wrapper."""
    handle = handle or os.environ.get("CME_CREDENTIAL_HANDLE", DEFAULT_HANDLE)
    cmd = ["pplx", "credentials", "run", handle, "--", "curl",
           "--silent", "--show-error", "--fail", "--connect-timeout", "10",
           "--max-time", "30", "--request", "POST", TOKEN_URL,
           "--data", "grant_type=client_credentials"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        if result.returncode:
            raise FedWatchError(f"CME token exchange exit {result.returncode}; check exact credential availability/scope; no fallback")
        obj = json.loads(result.stdout)
        token = obj["access_token"]
        if not isinstance(token, str) or not token or any(c in token for c in "\r\n"):
            raise ValueError("invalid token")
        if obj.get("token_type", "Bearer").lower() != "bearer":
            raise ValueError("unsupported token type")
        return token
    except (ValueError, KeyError, TypeError, subprocess.TimeoutExpired, OSError):
        raise FedWatchError("CME token exchange failed or returned an invalid response") from None


def api_get(path, token):
    """Use the provider-issued bearer token from the normal parent process."""
    if not path.startswith("/") or path.startswith("//"):
        raise FedWatchError("Invalid CME API path")
    escaped = token.replace("\\", "\\\\").replace('"', '\\"')
    config = f'header = "Authorization: Bearer {escaped}"\n'
    try:
        result = subprocess.run(
            ["curl", "--silent", "--show-error", "--connect-timeout", "10",
             "--max-time", "30", "--config", "-", "--write-out", "\n%{http_code}", API_BASE + path],
            input=config, capture_output=True, text=True, timeout=40)
        body, _, status = result.stdout.rpartition("\n")
        if result.returncode or status != "200":
            raise FedWatchError(f"CME data request HTTP {status or '000'}, exit {result.returncode}")
        data = json.loads(body)
        if not isinstance(data, dict) or not isinstance(data.get("payload"), list):
            raise ValueError("invalid payload")
        return data
    except (ValueError, subprocess.TimeoutExpired, OSError):
        raise FedWatchError("CME data request failed or returned an invalid response") from None


def fetch_snapshot(handle=None, count=4):
    token = obtain_token(handle)
    meetings = api_get("/meetings/future", token)
    dates = sorted({m["meetingDt"] for m in meetings["payload"] if m.get("meetingDt")})
    if not dates:
        raise FedWatchError("CME returned no future meetings")
    forecasts = [api_get("/forecasts?" + urlencode({"meetingDt": dt}), token) for dt in dates[:count]]
    return {"fetched_at": datetime.now(timezone.utc).isoformat(), "source": API_BASE,
            "meetings_raw": meetings, "forecasts": forecasts}


def _rows(payload):
    rows = payload.get("payload", [])
    if not rows:
        raise FedWatchError("Empty forecast payload")
    return rows[0]


def _probabilities(row):
    values = {}
    for item in row.get("rateRange", []):
        p = item.get("probability")
        if p is None:
            continue
        p = float(p)
        if not math.isfinite(p) or not 0 <= p <= 1:
            raise FedWatchError("Invalid forecast probability")
        values[(int(item["lowerRt"]), int(item["upperRt"]))] = p
    if not values or not math.isclose(sum(values.values()), 1.0, abs_tol=0.005):
        raise FedWatchError("Forecast probabilities do not sum to one")
    return values


def validate_snapshot(snapshot, previous, expected_reporting_date):
    """Caller supplies calendar-verified date. Fetch time does not prove freshness."""
    expected = date.fromisoformat(expected_reporting_date)
    rows = [_rows(fc) for fc in snapshot.get("forecasts", [])]
    if not rows or len({row["meetingDt"] for row in rows}) != len(rows):
        raise FedWatchError("Missing or duplicate meeting forecasts")
    requested = sorted({m["meetingDt"] for m in snapshot["meetings_raw"]["payload"]})
    if [r["meetingDt"] for r in rows] != requested[:len(rows)]:
        raise FedWatchError("Forecasts do not match requested meetings")
    for row in rows:
        if date.fromisoformat(row["reportingDt"]) != expected:
            raise FedWatchError("Unexpected reporting date; calendar/freshness review required")
        _probabilities(row)
    prior = _rows(previous["forecasts"][0] if "forecasts" in previous else previous)
    front = rows[0]
    if front["meetingDt"] != prior["meetingDt"]:
        raise FedWatchError("Front meeting rolled; matched historical baseline required")
    if date.fromisoformat(front["reportingDt"]) <= date.fromisoformat(prior["reportingDt"]):
        raise FedWatchError("Reporting date did not advance versus baseline")
    old, new = _probabilities(prior), _probabilities(front)
    deltas = [{"lowerRt": lo, "upperRt": hi, "prior": old.get((lo, hi), 0),
               "current": new.get((lo, hi), 0), "delta": new.get((lo, hi), 0) - old.get((lo, hi), 0)}
              for lo, hi in sorted(old.keys() | new.keys())
              if abs(new.get((lo, hi), 0) - old.get((lo, hi), 0)) > 1e-8]
    if not deltas:
        raise FedWatchError("Identical probability fields versus baseline")
    return {"status": "LIVE", "meetingDt": front["meetingDt"],
            "prior_reportingDt": prior["reportingDt"], "reportingDt": front["reportingDt"],
            "baseline_note": "Last supplied observed payload, not necessarily previous session",
            "expected_reporting_date": expected_reporting_date,
            "probability_sum": sum(new.values()), "field_deltas": deltas}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--expected-reporting-date", required=True)
    parser.add_argument("--handle", default=None)
    args = parser.parse_args()
    if args.output.resolve() == args.previous.resolve() or args.output.exists():
        parser.error("Use a new output path; do not overwrite the comparison baseline")
    previous = json.loads(args.previous.read_text())
    snapshot = {}
    try:
        snapshot = fetch_snapshot(args.handle)
        integrity = validate_snapshot(snapshot, previous, args.expected_reporting_date)
        snapshot.update(status="LIVE", integrity=integrity)
    except (FedWatchError, ValueError, KeyError, TypeError) as error:
        snapshot.update(status="DEGRADED", reason=str(error))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(snapshot, indent=2) + "\n")
    print(json.dumps({k: v for k, v in snapshot.items()
                      if k in ("status", "reason", "integrity", "fetched_at")}, indent=2))
    return 0 if snapshot["status"] == "LIVE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
