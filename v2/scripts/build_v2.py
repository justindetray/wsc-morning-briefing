#!/usr/bin/env python3
"""
Morning Briefing v2 - HTML builder.

Inputs (all in v2/data/):
  tape.json, fedwatch.json, briefing_economic.json, status.json
  calendar.json, overnight.json, research_manifest.json

Output:
  v2/build/index.html  (single page, target <= 15KB)

Rules:
  - Render only what's LIVE. DEGRADED fields show "— NO DATA" with reason.
  - Never substitute a stale value. Never template.
  - Per-field source-payload timestamps already in the JSON; the build trusts them.
  - Single masthead build stamp = now(). Per-tile timestamps shown only when DEGRADED.
  - If overnight.json is empty/null on key arrays, paragraph is omitted.
  - If calendar.json last_curated > 7 days old, calendar gets a DEGRADED banner.
"""

import json
import re
import sys
import html as html_mod
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA = REPO_ROOT / "v2" / "data"
BUILD = REPO_ROOT / "v2" / "build"
BUILD.mkdir(parents=True, exist_ok=True)

ET = ZoneInfo("America/New_York")

def jload(name):
    p = DATA / name
    if not p.exists():
        return None
    return json.loads(p.read_text())

def fmt_num(v, places=2):
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{places}f}"
    except (TypeError, ValueError):
        return str(v)

def fmt_delta(pct):
    if pct is None:
        return ""
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.2f}%"

def delta_class(pct):
    if pct is None:
        return "delta neutral"
    return "delta up" if pct >= 0 else "delta down"

def safe_iso_to_et(iso_str):
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ET).strftime("%H:%M ET")
    except Exception:
        return iso_str[:16]

def safe_iso_to_et_full(iso_str):
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ET).strftime("%b %d %H:%M ET")
    except Exception:
        return iso_str[:16]

# ---------- tape tile ----------

TAPE_ORDER = ["ES", "NQ", "SPX", "NDX", "SOX", "DJI", "VIX", "US10Y", "US2Y",
              "WTI", "BRENT", "GOLD", "BTC", "EURJPY", "AUDJPY"]

# Keys that must be present in tape.json on every run. The tape renderer only
# emits keys it finds, so an omitted key vanishes with no visible trace - the
# same silent-fallback trap that hid the missing client_frame_short. Warn loudly
# instead of failing: a missing diagnostic tile must not kill a build that is
# otherwise sound. ES/NQ/SPX/NDX are deliberately excluded because which of them
# is present legitimately varies with the session (futures pre-open vs cash).
#   SOX    - added 2026-08-19 (lesson #156-F): a -4.98% semiconductor print was
#            the actual cause of an Asian session we mis-attributed to JGB yields.
#   EURJPY - added 2026-08-19 (lesson #156-G): dollar-free yen crosses. USD/JPY
#   AUDJPY   alone cannot distinguish a yen funding squeeze from dollar weakness.
REQUIRED_TAPE_KEYS = ["SOX", "VIX", "US10Y", "US2Y", "EURJPY", "AUDJPY"]


def md_inline(s):
    """Escape HTML, then convert **bold** and *italic* markdown. Display-only.

    Regex-based so that unmatched delimiters stay literal rather than
    producing half-formatted output. Bold runs first so that ** is never
    misread as two single asterisks.
    """
    out = html_mod.escape(s or "")
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out, flags=re.S)
    out = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", out, flags=re.S)
    return out

def md_para(s):
    """Render a multi-paragraph string as <p> blocks with inline markdown.

    Blank-line-separated paragraphs in the source JSON were previously
    collapsed into a single run-on block because md_inline is inline-only.
    Splits on one-or-more blank lines; drops empty segments so trailing
    newlines do not emit an empty <p>.
    """
    segs = [seg.strip() for seg in re.split(r"\n\s*\n", s or "")]
    segs = [seg for seg in segs if seg]
    return "".join(f"<p>{md_inline(seg)}</p>" for seg in segs)
TILE_FORMAT = {
    "ES":    {"places": 2, "suffix": ""},
    "NQ":    {"places": 2, "suffix": ""},
    "SPX":   {"places": 2, "suffix": ""},
    "NDX":   {"places": 2, "suffix": ""},
    "SOX":   {"places": 2, "suffix": ""},
    "DJI":   {"places": 2, "suffix": ""},
    "VIX":   {"places": 2, "suffix": ""},
    "US10Y": {"places": 3, "suffix": "%"},
    "US2Y":  {"places": 3, "suffix": "%"},
    "WTI":   {"places": 2, "suffix": ""},
    "BRENT": {"places": 2, "suffix": ""},
    "GOLD":  {"places": 2, "suffix": ""},
    "BTC":    {"places": 0, "suffix": ""},
    "EURJPY": {"places": 2, "suffix": ""},
    "AUDJPY": {"places": 2, "suffix": ""},
}

def render_tile(key, field):
    fmt = TILE_FORMAT.get(key, {"places": 2, "suffix": ""})
    if field.get("status") not in ("LIVE", "CLOSED"):
        return f"""<div class="tile dead">
  <div class="sym">{key}</div>
  <div class="val">— NO DATA</div>
  <div class="meta">{html_mod.escape(field.get('reason') or 'unavailable')}</div>
</div>"""
    val = fmt_num(field["value"], fmt["places"]) + fmt["suffix"]
    delta = fmt_delta(field.get("delta_pct"))
    dcls = delta_class(field.get("delta_pct"))
    prev = field.get("prev_close")
    prev_str = f"prev {fmt_num(prev, fmt['places'])}{fmt['suffix']}" if prev is not None else ""
    return f"""<div class="tile">
  <div class="sym">{key}</div>
  <div class="val">{val}</div>
  <div class="{dcls}">{delta}</div>
  <div class="meta">{prev_str}</div>
</div>"""

# ---------- time sorting ----------

def time_key(t):
    """Sort key for display times like '8:30 AM', '~3:30 PM', '12:01 AM', 'all day'.
    Returns minutes past midnight ET. Unparseable values sort last."""
    if not t:
        return 10 ** 6
    m = re.search(r"(\d{1,2}):(\d{2})\s*([AaPp])\.?[Mm]", str(t))
    if not m:
        return 10 ** 6
    hh, mm, ap = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    if hh == 12:
        hh = 0
    if ap == "p":
        hh += 12
    return hh * 60 + mm


# ---------- on-deck (today's calendar events) ----------

def render_on_deck(calendar, today_et):
    if not calendar:
        return '<div class="muted">Calendar unavailable.</div>'
    today_str = today_et.isoformat()
    macro_today = [e for e in calendar.get("macro", []) if e.get("date") == today_str]
    earn_today = [e for e in calendar.get("earnings", []) if e.get("date") == today_str]
    if not macro_today and not earn_today:
        return '<div class="muted">No scheduled releases.</div>'
    lines = []
    for e in sorted(macro_today, key=lambda x: time_key(x.get("time_et"))):
        cons = f" — cons {html_mod.escape(e['consensus'])}" if e.get("consensus") else ""
        imp = e.get("importance") or "low"
        imp_cls = f" imp-{imp}" if imp in ("high", "medium") else ""
        note = f' <span class="note">({html_mod.escape(e["note"])})</span>' if e.get("note") else ""
        tstr = e.get("time_et")
        ev = e.get("event") or ""
        if tstr:
            lines.append(f'<li class="ev{imp_cls}"><b>{tstr}</b> — {html_mod.escape(ev)}{cons}{note}</li>')
        else:
            lines.append(f'<li class="ev{imp_cls}">{html_mod.escape(ev)}{cons}{note}</li>')
    if earn_today:
        pre = [e for e in earn_today if e.get("session") == "pre-market"]
        post = [e for e in earn_today if e.get("session") in ("after-hours", "after-close")]
        if pre:
            lines.append('<li class="ev"><b>Pre-mkt earnings:</b> ' +
                ", ".join(f'{html_mod.escape(e["ticker"])} ({html_mod.escape(e["name"])})' for e in pre) + '</li>')
        if post:
            lines.append('<li class="ev"><b>After-close earnings:</b> ' +
                ", ".join(f'{html_mod.escape(e["ticker"])} ({html_mod.escape(e["name"])})' for e in post) + '</li>')
    return "<ul class=\"ondeck\">" + "".join(lines) + "</ul>"

# ---------- overnight paragraph ----------

def render_overnight(overnight):
    if not overnight:
        return ""
    parts = []
    asia = overnight.get("asia_close", [])
    if asia:
        bits = []
        for a in asia:
            d = a.get("delta_pct")
            if d is None:
                continue
            sign = "+" if d >= 0 else ""
            level_str = f" to {fmt_num(a.get('level'), 0)}" if a.get("level") else ""
            bits.append(f"{html_mod.escape(a['market'])} {sign}{d:.2f}%{level_str}")
        if bits:
            parts.append("Asia: " + ", ".join(bits) + ".")
    europe = overnight.get("europe_open", [])
    if europe:
        bits = []
        for e in europe:
            d = e.get("delta_pct")
            if d is None:
                continue
            sign = "+" if d >= 0 else ""
            bits.append(f"{html_mod.escape(e['market'])} {sign}{d:.2f}%")
        if bits:
            parts.append("Europe open: " + ", ".join(bits) + ".")
    moves = overnight.get("single_name_moves", [])
    if moves:
        bits = []
        for m in moves:
            d = m.get("delta_pct")
            sign = "+" if (d or 0) >= 0 else ""
            venue = f" ({html_mod.escape(m['venue'])})" if m.get("venue") else ""
            if d is None:
                bits.append(f"{html_mod.escape(m['ticker'])}{venue}")
            else:
                bits.append(f"{html_mod.escape(m['ticker'])} {sign}{d:.2f}%{venue}")
        if bits:
            parts.append("Single names: " + "; ".join(bits) + ".")
    blocks = ""
    if parts:
        blocks += '<p class="overnight">' + " ".join(parts) + "</p>"
    geo = overnight.get("geo_headlines", [])
    if geo:
        items = "".join(f"<li>{md_inline(g)}</li>" for g in geo)
        blocks += f'<ul class="geo">{items}</ul>'
    # Two-version client frame (added run #154 at Justin's request):
    #   client_frame_short -> the 2-3 sentence version for a client who calls
    #   client_frame       -> the fuller version for one who wants the mechanics
    # Backward compatible: with no short version present, render the single
    # legacy CLIENT FRAME block exactly as before.
    cf_short = overnight.get("client_frame_short")
    cf = overnight.get("client_frame")
    if cf_short:
        blocks += (
            '<div class="clientframe cf-short">'
            '<div class="cf-h">THE SHORT ANSWER &middot; IF A CLIENT CALLS</div>'
            f'{md_para(cf_short)}</div>'
        )
        if cf:
            blocks += (
                '<div class="clientframe">'
                '<div class="cf-h">THE FULLER PICTURE &middot; IF THEY WANT THE MECHANICS</div>'
                f'{md_para(cf)}</div>'
            )
    elif cf:
        blocks += f'<div class="clientframe"><div class="cf-h">CLIENT FRAME</div>{md_para(cf)}</div>'
    return blocks

# ---------- FedWatch table ----------

def render_fedwatch(fw):
    if not fw or fw.get("status") != "LIVE":
        reason = (fw or {}).get("reason", "unavailable")
        return f'<div class="fw-dead">FedWatch unavailable: {html_mod.escape(reason)}</div>'
    meetings = fw.get("meetings", [])
    if not meetings:
        return '<div class="fw-dead">FedWatch returned no meetings.</div>'
    rows = ['<table class="fw"><thead><tr><th>Meeting</th><th>Hold</th><th>+25</th><th>+50</th><th>+75</th><th>+100</th><th>Cum hike</th></tr></thead><tbody>']
    for m in meetings:
        if "hold" not in m:
            continue
        rows.append(
            f"<tr><td>{html_mod.escape(m['meetingDt'])}</td>"
            f"<td>{m['hold']*100:.1f}</td>"
            f"<td>{m['hike_25']*100:.1f}</td>"
            f"<td>{m['hike_50']*100:.1f}</td>"
            f"<td>{m['hike_75']*100:.1f}</td>"
            f"<td>{m['hike_100']*100:.1f}</td>"
            f"<td><b>{m['cumulative_hike']*100:.1f}</b></td></tr>"
        )
    rows.append("</tbody></table>")
    rep = meetings[0].get("reportingDt")
    if rep:
        rows.append(f'<div class="fw-meta">CME cohort as of {html_mod.escape(rep)}</div>')
    return "".join(rows)

# ---------- Calendar (5-day) ----------

def render_calendar(calendar, today_et):
    if not calendar:
        return '<div class="muted">Calendar unavailable.</div>'
    # Staleness check
    last_cur = calendar.get("last_curated")
    stale_banner = ""
    if last_cur:
        try:
            dt = datetime.fromisoformat(last_cur)
            if (datetime.now(dt.tzinfo or timezone.utc) - dt).days > 7:
                stale_banner = '<div class="degraded">Calendar curation stale (>7 days). Update v2/data/calendar.json.</div>'
        except Exception:
            pass
    macro = calendar.get("macro", [])
    earn = calendar.get("earnings", [])
    if not macro and not earn:
        return stale_banner + '<div class="muted">No events curated.</div>'
    out = [stale_banner, '<table class="cal"><thead><tr><th>Date</th><th>Time ET</th><th>Event</th><th>Consensus / Note</th></tr></thead><tbody>']
    combined = []
    for e in macro:
        combined.append({
            "date": e.get("date") or "",
            "time": e.get("time_et") or "",
            "event": e.get("event") or "",
            "cons": e.get("consensus") or e.get("note") or "",
            "imp": e.get("importance") or "low",
        })
    for e in earn:
        combined.append({
            "date": e.get("date") or "",
            "time": e.get("session") or "",
            "event": f"{e.get('ticker','?')} ({e.get('name','?')}) earnings",
            "cons": e.get("note") or "",
            "imp": e.get("importance") or "low",
        })
    combined.sort(key=lambda x: (x["date"] or "", time_key(x["time"]), x["time"] or ""))
    for c in combined:
        imp_cls = f" imp-{c['imp']}" if c['imp'] in ("high", "medium") else ""
        out.append(
            f'<tr class="{imp_cls.strip()}">'
            f'<td>{html_mod.escape(c["date"] or "")}</td>'
            f'<td>{html_mod.escape(c["time"])}</td>'
            f'<td>{html_mod.escape(c["event"])}</td>'
            f'<td class="muted">{html_mod.escape(c["cons"])}</td>'
            f'</tr>'
        )
    out.append("</tbody></table>")
    return "".join(out)

# ---------- Research links ----------

def render_research(manifest):
    if not manifest or not manifest.get("docs"):
        return '<div class="muted">No research docs in manifest.</div>'
    items = []
    for d in manifest["docs"][:7]:
        items.append(
            f'<li><span class="rdate">{html_mod.escape(d.get("date",""))}</span> '
            f'<a href="{html_mod.escape(d.get("drive_url",""))}">{html_mod.escape(d.get("title",""))}</a> '
            f'<span class="rtoken">{html_mod.escape(d.get("headline_token",""))}</span></li>'
        )
    return "<ul class=\"research\">" + "".join(items) + "</ul>"

# ---------- DEGRADED banner ----------

def render_degraded_banner(tape, fedwatch, economic, calendar):
    fails = []
    for k, v in tape.items():
        if v.get("status") not in ("LIVE", "CLOSED"):
            fails.append(f"{k} ({v.get('reason','?')})")
    if fedwatch and fedwatch.get("status") != "LIVE":
        fails.append(f"FedWatch ({fedwatch.get('reason','?')})")
    if not fails:
        return ""
    return f'<div class="degraded">DEGRADED fields this build: {html_mod.escape(", ".join(fails))}</div>'

# ---------- CSS (kept small) ----------

CSS = """
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:#0b0c0d;color:#e8e8e8;font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:18px 22px 60px}
.masthead{display:flex;justify-content:space-between;align-items:baseline;border-bottom:1px solid #222;padding-bottom:10px;margin-bottom:14px}
.masthead h1{margin:0;font-size:18px;font-weight:600;letter-spacing:.02em}
.masthead .stamp{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;color:#888}
.degraded{background:#3a1e1e;border:1px solid #6e2828;color:#ffb4b4;padding:8px 12px;border-radius:4px;margin:8px 0;font-size:12px;font-family:ui-monospace,Menlo,monospace}
.section{margin:18px 0 8px}
.section h2{font-size:13px;text-transform:uppercase;letter-spacing:.12em;color:#999;margin:0 0 10px;font-weight:600}
.tape{display:grid;grid-template-columns:repeat(8,1fr);gap:6px;margin-bottom:14px}
.tile{background:#161718;border:1px solid #242425;border-radius:4px;padding:8px 10px;min-height:80px}
.tile.dead{background:#1f1414;border-color:#3a2020}
.tile .sym{font-size:11px;color:#888;letter-spacing:.08em;font-weight:600}
.tile .val{font-size:18px;font-weight:600;margin-top:3px;font-variant-numeric:tabular-nums}
.tile .delta{font-size:12px;margin-top:2px;font-variant-numeric:tabular-nums}
.tile .delta.up{color:#4ade80}
.tile .delta.down{color:#f87171}
.tile .delta.neutral{color:#888}
.tile .meta{font-size:10px;color:#666;margin-top:3px;font-family:ui-monospace,Menlo,monospace}
.ondeck{list-style:none;padding:0;margin:0}
.ondeck .ev{padding:4px 0;border-bottom:1px solid #1c1d1e;font-size:13px}
.ondeck .ev:last-child{border-bottom:none}
.ondeck .ev.imp-high{color:#fafafa}
.ondeck .ev.imp-medium{color:#cccccc}
.ondeck .ev .note{color:#888;font-size:12px}
.overnight{background:#131415;border-left:3px solid #444;padding:10px 14px;border-radius:0 4px 4px 0;color:#cccccc;font-size:13px;margin:8px 0 10px}
.geo{margin:0 0 14px;padding:0 0 0 18px;list-style:disc}
.geo li{color:#cfcfcf;font-size:13px;line-height:1.55;margin:0 0 8px}
.geo strong{color:#ffffff;font-weight:600}
.clientframe{background:#101418;border-left:3px solid #3d6ea5;padding:12px 16px;border-radius:0 4px 4px 0;color:#cccccc;font-size:13px;line-height:1.6;margin:0 0 16px}
.clientframe strong{color:#ffffff;font-weight:600}
.clientframe p{margin:0 0 10px}
.clientframe p:last-child{margin-bottom:0}
.cf-h{color:#7fb0e0;font-size:10px;letter-spacing:.12em;margin-bottom:6px}
.cf-short{background:#111a22;border-left-color:#6fa8dc;color:#e8e8e8;font-size:15px;line-height:1.65;margin-bottom:10px}
.cf-short .cf-h{color:#9ccbf0}
.fw{width:100%;border-collapse:collapse;font-size:12px;font-variant-numeric:tabular-nums}
.fw th,.fw td{padding:5px 8px;text-align:right;border-bottom:1px solid #1c1d1e}
.fw th:first-child,.fw td:first-child{text-align:left;font-weight:600}
.fw thead th{color:#888;font-weight:500;text-transform:uppercase;letter-spacing:.06em;font-size:10px}
.fw-meta{font-size:10px;color:#666;margin-top:6px;font-family:ui-monospace,Menlo,monospace}
.fw-dead{padding:8px;color:#f87171;font-size:12px;background:#1f1414;border-radius:4px}
.cal{width:100%;border-collapse:collapse;font-size:12px}
.cal th,.cal td{padding:6px 8px;border-bottom:1px solid #1c1d1e;text-align:left}
.cal th{color:#888;font-weight:500;text-transform:uppercase;letter-spacing:.06em;font-size:10px}
.cal tr.imp-high td{background:#181818}
.cal .muted{color:#888}
.research{list-style:none;padding:0;margin:0}
.research li{padding:5px 0;font-size:12px}
.research li a{color:#9cb8ff;text-decoration:none}
.research li a:hover{text-decoration:underline}
.research .rdate{color:#888;font-family:ui-monospace,Menlo,monospace;margin-right:8px}
.research .rtoken{color:#666;font-size:10px;margin-left:8px;font-family:ui-monospace,Menlo,monospace}
.muted{color:#888;font-size:12px;padding:6px 0}
@media (max-width:760px){
  .tape{grid-template-columns:repeat(4,1fr)}
}
@media (max-width:480px){
  .tape{grid-template-columns:repeat(2,1fr)}
}
"""

# ---------- main ----------

def main():
    tape = jload("tape.json") or {}
    fedwatch = jload("fedwatch.json") or {}
    economic = jload("briefing_economic.json") or {}
    status = jload("status.json") or {}
    calendar = jload("calendar.json") or {}
    overnight = jload("overnight.json") or {}
    research = jload("research_manifest.json") or {}

    # Sanity: if all tape DEGRADED, refuse to write.
    # CLOSED is a healthy state, not a degraded one: on weekends and market
    # holidays every field is legitimately CLOSED (prior-session settle), so
    # counting only LIVE made this guard misfire on any non-trading day.
    # Abort only when no field carries a trustworthy status at all.
    if tape:
        healthy = sum(1 for v in tape.values()
                      if v.get("status") in ("LIVE", "CLOSED"))
        if healthy == 0:
            print("[build_v2] ABORT: no LIVE or CLOSED tape fields "
                  "(all DEGRADED/unknown)", file=sys.stderr)
            sys.exit(2)

    # Loud warning for required tape keys that were never fetched. Not fatal:
    # the renderer would otherwise omit them silently and the omission would be
    # invisible in the shipped page.
    missing_required = [k for k in REQUIRED_TAPE_KEYS if k not in tape]
    if missing_required:
        print(f"[build_v2] WARNING: required tape keys absent from tape.json: "
              f"{', '.join(missing_required)} - these tiles will NOT render",
              file=sys.stderr)

    now_utc = datetime.now(timezone.utc)
    now_et = now_utc.astimezone(ET)
    today_et = now_et.date()

    # Render tiles for keys present in tape.json (in TAPE_ORDER); on weekends ES/NQ absent, on weekdays SPX/NDX absent
    tiles_html = "".join(render_tile(k, tape[k]) for k in TAPE_ORDER if k in tape)
    on_deck_html = render_on_deck(calendar, today_et)
    overnight_html = render_overnight(overnight)
    fedwatch_html = render_fedwatch(fedwatch)
    calendar_html = render_calendar(calendar, today_et)
    research_html = render_research(research)
    degraded_html = render_degraded_banner(tape, fedwatch, economic, calendar)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WSC Morning Briefing — {now_et.strftime('%a %b %d %Y')}</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">

<div class="masthead">
  <h1>WSC Morning Briefing — {now_et.strftime('%A, %B %-d, %Y')}</h1>
  <span class="stamp">Built {now_et.strftime('%H:%M ET')} · v2</span>
</div>

{degraded_html}

<div class="section">
  <h2>Tape</h2>
  <div class="tape">{tiles_html}</div>
</div>

<div class="section">
  <h2>On Deck Today</h2>
  {on_deck_html}
</div>

<div class="section">
  <h2>Overnight</h2>
  {overnight_html or '<div class="muted">No overnight summary available.</div>'}
</div>

<div class="section">
  <h2>FedWatch</h2>
  {fedwatch_html}
</div>

<div class="section">
  <h2>5-Day Calendar</h2>
  {calendar_html}
</div>

<div class="section">
  <h2>Research</h2>
  {research_html}
</div>

</div>
</body>
</html>
"""

    out = BUILD / "index.html"
    out.write_text(html)
    size = out.stat().st_size
    print(f"[build_v2] wrote {out.relative_to(REPO_ROOT)}  {size:,} bytes")
    if size > 25000:
        print(f"[build_v2] WARNING: size {size} exceeds 25KB soft limit")

if __name__ == "__main__":
    main()
