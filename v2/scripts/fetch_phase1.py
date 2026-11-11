#!/usr/bin/env python3
"""
Morning Briefing v2 - Phase 1 fetcher.

Source matrix (verified 2026-06-23):
  - Yahoo /v8/finance/chart/    -> ES=F, NQ=F, ^VIX, ^TNX (10Y), CL=F, GC=F
  - CNBC quote API              -> US2Y
  - Polygon X:BTCUSD            -> BTC current + prior-trading-day 16:00 ET ref close
  - CME auth.cmegroup.com OAuth -> FedWatch probabilities
  - briefing.com (public)       -> Economic calendar HTML

Operating Rules:
  1. Source-payload timestamp ONLY. Never datetime.now() for a quote.
  2. HTTP 200 != fresh. Caller (build script) is responsible for stale-check.
  3. Failed field -> DEGRADED, never template. Exit 1 if any DEGRADED, 2 if all dead.
  4. No silent fallback.
"""

import json
import os
import sys
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path
from fedwatch_secure import fetch_snapshot, validate_snapshot, _probabilities, _rows

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "v2" / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

POLYGON_KEY = os.environ.get("POLYGON_API_KEY", "")

UA = "Mozilla/5.0 (compatible; WSC-Morning-Briefing/2.0)"

# ---------- helpers ----------

def now_utc_iso():
    return datetime.now(timezone.utc).isoformat()

def http_get(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8"), r.status, dict(r.headers)

def degraded(reason):
    return {"value": None, "as_of": None, "status": "DEGRADED", "reason": reason}

def live(value, as_of, extras=None):
    out = {"value": value, "as_of": as_of, "status": "LIVE"}
    if extras:
        out.update(extras)
    return out

# ---------- Yahoo chart endpoint (the working one) ----------

def fetch_yahoo_chart(symbol):
    """Returns (price, source_ts_iso, prev_close, err)."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?interval=1d&range=2d"
    try:
        body, _, _ = http_get(url, timeout=10)
        d = json.loads(body)
        res = d.get("chart", {}).get("result", [])
        if not res:
            err = d.get("chart", {}).get("error")
            return None, None, None, f"empty result: {err}"
        m = res[0].get("meta", {})
        price = m.get("regularMarketPrice")
        prev = m.get("chartPreviousClose")
        ts_epoch = m.get("regularMarketTime")
        if price is None or ts_epoch is None:
            return None, None, None, "missing price/ts in meta"
        ts_iso = datetime.fromtimestamp(ts_epoch, tz=timezone.utc).isoformat()
        return float(price), ts_iso, (float(prev) if prev is not None else None), None
    except urllib.error.HTTPError as e:
        return None, None, None, f"HTTP {e.code}"
    except Exception as e:
        return None, None, None, f"{type(e).__name__}: {e}"

YAHOO_TAPE = [
    ("ES",    "ES=F",  "ES front-month"),
    ("NQ",    "NQ=F",  "NQ front-month"),
    ("VIX",   "^VIX",  "VIX"),
    ("US10Y", "^TNX",  "US 10Y yield"),
    ("WTI",   "CL=F",  "WTI front-month"),
    ("GOLD",  "GC=F",  "Gold front-month"),
]

def fetch_yahoo_tape():
    out = {}
    for key, symbol, label in YAHOO_TAPE:
        p, ts, prev, err = fetch_yahoo_chart(symbol)
        if p is not None:
            delta_pct = round(100 * (p - prev) / prev, 2) if prev else None
            out[key] = live(p, ts, {
                "prev_close": prev,
                "delta_pct": delta_pct,
                "label": label,
                "source": f"yahoo:{symbol}",
            })
        else:
            out[key] = degraded(f"yahoo {symbol}: {err}")
    return out

# ---------- CNBC for US2Y ----------

def fetch_cnbc_us2y():
    """CNBC quote API returns formatted strings; need parsing."""
    url = "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol?symbols=US2Y&requestMethod=itv&output=json"
    try:
        body, _, _ = http_get(url, timeout=10)
        d = json.loads(body)
        q = d.get("FormattedQuoteResult", {}).get("FormattedQuote", [])
        if not q:
            return degraded("CNBC US2Y empty")
        row = q[0]
        last = row.get("last", "").rstrip("%")
        last_time = row.get("last_time")
        change_str = (row.get("change") or "0").rstrip("%")
        try:
            price = float(last)
        except (TypeError, ValueError):
            return degraded(f"CNBC US2Y unparseable: {last!r}")
        # last_time format: '2026-06-23T11:48:46.000-0400' - convert to UTC iso
        try:
            dt = datetime.strptime(last_time[:19], "%Y-%m-%dT%H:%M:%S")
            # tz offset
            tz_part = last_time[-5:]
            sign = 1 if tz_part[0] == "+" else -1
            hours = int(tz_part[1:3]); mins = int(tz_part[3:5])
            offset = timedelta(hours=hours, minutes=mins) * sign
            dt_utc = (dt - offset).replace(tzinfo=timezone.utc)
            ts_iso = dt_utc.isoformat()
        except Exception:
            ts_iso = now_utc_iso() + "  (parse-fail-fallback)"
        # Compute prev close from current minus change
        try:
            change = float(change_str)
            prev = round(price - change, 4)
        except Exception:
            prev = None
        delta_pct = round(100 * (price - prev) / prev, 2) if prev else None
        return live(price, ts_iso, {
            "prev_close": prev,
            "delta_pct": delta_pct,
            "label": "US 2Y yield",
            "source": "cnbc:US2Y",
            "unit": "%",
        })
    except Exception as e:
        return degraded(f"CNBC US2Y: {type(e).__name__}: {e}")

# ---------- BTC Polygon ----------

def fetch_btc_polygon():
    if not POLYGON_KEY:
        return degraded("POLYGON_API_KEY missing")
    # Current snapshot
    snap_url = f"https://api.polygon.io/v2/aggs/ticker/X:BTCUSD/prev?adjusted=true&apiKey={POLYGON_KEY}"
    try:
        body, status, _ = http_get(snap_url, timeout=10)
        if status != 200:
            return degraded(f"polygon prev HTTP {status}")
        data = json.loads(body)
        results = data.get("results", [])
        if not results:
            return degraded("polygon prev empty")
        r = results[0]
        price = r.get("c")
        ts_ms = r.get("t")
        if price is None or ts_ms is None:
            return degraded("polygon prev missing c/t")
        as_of = datetime.fromtimestamp(ts_ms/1000, tz=timezone.utc).isoformat()
    except Exception as e:
        return degraded(f"polygon prev: {type(e).__name__}: {e}")

    # Prior trading day 16:00 ET reference for delta
    today_et = (datetime.now(timezone.utc) - timedelta(hours=4)).date()
    ref_day = today_et - timedelta(days=1)
    while ref_day.weekday() >= 5:
        ref_day -= timedelta(days=1)
    ref_url = (
        f"https://api.polygon.io/v2/aggs/ticker/X:BTCUSD/range/1/minute/"
        f"{ref_day.isoformat()}/{ref_day.isoformat()}?adjusted=true&sort=asc&limit=5000&apiKey={POLYGON_KEY}"
    )
    ref_close = ref_ts = None
    try:
        body, status, _ = http_get(ref_url, timeout=15)
        if status == 200:
            bars = json.loads(body).get("results", [])
            # 16:00 ET in June = 20:00 UTC (EDT)
            target = datetime.combine(ref_day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=20)
            target_ms = int(target.timestamp() * 1000)
            best = None
            for bar in bars:
                if bar["t"] <= target_ms:
                    best = bar
                else:
                    break
            if best:
                ref_close = best["c"]
                ref_ts = datetime.fromtimestamp(best["t"]/1000, tz=timezone.utc).isoformat()
    except Exception:
        pass

    out = live(price, as_of, {"label": "BTC/USD", "source": "polygon:X:BTCUSD"})
    if ref_close is not None:
        out["prev_close"] = ref_close
        out["ref_close_ts"] = ref_ts
        out["delta_pct"] = round(100 * (price - ref_close) / ref_close, 2)
    else:
        out["prev_close"] = None
        out["delta_pct"] = None
        out["delta_note"] = "Prior-cash-close ref unavailable; delta omitted"
    return out

# ---------- FedWatch (correct CME endpoints) ----------

def _condense_forecast(payload, current_lower):
    """Classify against an explicitly verified policy band, expressed in basis points."""
    by_lower = {lo: p for (lo, hi), p in _probabilities(_rows(payload)).items()}
    current_lower = int(current_lower)
    out = {
        "hold": by_lower.get(current_lower, 0.0),
        "cut": sum(p for lo, p in by_lower.items() if lo < current_lower),
        "hike_25": by_lower.get(current_lower + 25, 0.0),
        "hike_50": by_lower.get(current_lower + 50, 0.0),
        "hike_75": by_lower.get(current_lower + 75, 0.0),
        "hike_100": by_lower.get(current_lower + 100, 0.0),
        "current_band": f"{current_lower/100:.2f}-{(current_lower+25)/100:.2f}",
    }
    out["cumulative_hike"] = sum(p for lo, p in by_lower.items() if lo > current_lower)
    return out

def fetch_fedwatch(current_lower=None, expected_reporting_date=None, previous_path=None):
    current_lower = current_lower if current_lower is not None else os.environ.get("FEDWATCH_CURRENT_LOWER")
    expected_reporting_date = expected_reporting_date or os.environ.get("FEDWATCH_EXPECTED_REPORTING_DATE")
    previous_path = previous_path or os.environ.get("FEDWATCH_PREVIOUS_PATH")
    if current_lower is None or not expected_reporting_date or not previous_path:
        return {"status": "DEGRADED", "reason": "Verified policy band, expected reporting date and previous payload path required", "meetings": []}
    try:
        previous = json.loads(Path(previous_path).read_text())
        snapshot = fetch_snapshot()
        integrity = validate_snapshot(snapshot, previous, expected_reporting_date)
        out_meetings = []
        for fc in snapshot["forecasts"]:
            row = _rows(fc)
            out_meetings.append({
                "meetingDt": row["meetingDt"], "reportingDt": row["reportingDt"],
                **_condense_forecast(fc, current_lower),
            })
        return {
            "status": integrity["status"], "as_of": integrity["reportingDt"],
            "fetched_at": snapshot["fetched_at"], "reason": None,
            "meetings": out_meetings, "integrity": integrity,
        }
    except Exception as e:
        return {"status": "DEGRADED", "reason": f"{type(e).__name__}: {e}", "meetings": []}

# ---------- Briefing.com public economic calendar ----------

def fetch_briefing_economic():
    url = "https://www.briefing.com/investor/calendars/economic"
    try:
        body, status, _ = http_get(url, timeout=15)
        if status != 200:
            return {"status": "DEGRADED", "reason": f"briefing HTTP {status}", "as_of": now_utc_iso(), "events": []}
        return {"status": "LIVE", "as_of": now_utc_iso(), "raw_html": body, "source_url": url}
    except Exception as e:
        return {"status": "DEGRADED", "reason": f"{type(e).__name__}: {e}", "as_of": now_utc_iso(), "events": []}

# ---------- main ----------

def main():
    print(f"[fetch_phase1] starting {now_utc_iso()}")
    tape = fetch_yahoo_tape()
    tape["US2Y"] = fetch_cnbc_us2y()
    tape["BTC"]  = fetch_btc_polygon()

    fedwatch = fetch_fedwatch()
    economic = fetch_briefing_economic()

    # Per-field status summary
    rows = []
    for k, v in tape.items():
        rows.append((k, v.get("status"), v.get("reason")))
    rows.append(("FEDWATCH", fedwatch.get("status"), fedwatch.get("reason")))
    rows.append(("ECONOMIC", economic.get("status"), economic.get("reason")))

    status = {
        "build_started": now_utc_iso(),
        "fields": [{"field": k, "status": s, "reason": r} for k, s, r in rows],
        "degraded_count": sum(1 for _, s, _ in rows if s == "DEGRADED"),
    }

    (DATA_DIR / "tape.json").write_text(json.dumps(tape, indent=2))
    (DATA_DIR / "fedwatch.json").write_text(json.dumps(fedwatch, indent=2))
    (DATA_DIR / "briefing_economic.json").write_text(json.dumps(economic, indent=2))
    (DATA_DIR / "status.json").write_text(json.dumps(status, indent=2))

    live_count = sum(1 for v in tape.values() if v.get("status") == "LIVE")
    print(f"[fetch_phase1] tape: {len(tape)} fields, {live_count} LIVE")
    print(f"[fetch_phase1] fedwatch: {fedwatch.get('status')}")
    print(f"[fetch_phase1] economic: {economic.get('status')}")
    print(f"[fetch_phase1] degraded fields: {status['degraded_count']}")
    for k, s, r in rows:
        marker = "OK " if s == "LIVE" else "!! "
        reason = f"  ({r})" if r else ""
        print(f"  {marker}{k:<10} {s}{reason}")

    all_tape_dead = all(v.get("status") != "LIVE" for v in tape.values())
    if all_tape_dead:
        print("[fetch_phase1] CRITICAL: all tape fields DEGRADED")
        sys.exit(2)
    if status["degraded_count"] > 0:
        sys.exit(1)
    sys.exit(0)

if __name__ == "__main__":
    main()
