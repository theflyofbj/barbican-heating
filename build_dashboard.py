#!/usr/bin/env python3
"""Build the interactive underfloor-heating dashboard as ONE self-contained HTML page.

Reads heating_history.csv (long format: post_date, window_start, window_end, profile, minutes)
and temperature_history.csv (hourly: time_utc, temp_c), keeps only heating-season dates
(1 Oct - 30 Apr, judged by the post date), groups them by season and writes index.html.

    python build_dashboard.py                        # -> index.html, expects plotly-basic.min.js next to it
    python build_dashboard.py --plotly-js FILE.js    # embed a local Plotly build instead (one file, works offline)
    python build_dashboard.py --plotly-cdn           # load Plotly from a CDN instead

Only the standard library is needed (plus the system time-zone database; `pip install tzdata` on Windows).
"""
import argparse
import csv
import json
import bisect
import math
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

PLOTLY_CDN = "https://cdn.jsdelivr.net/npm/plotly.js-basic-dist-min@2.35.2/plotly-basic.min.js"
PLOTLY_LOCAL = "plotly-basic.min.js"
PROFILES = ["Unbiased", "Adjusted"]
LONDON = ZoneInfo("Europe/London")
POST_TIME = timedelta(hours=8, minutes=30)
SEASON_FIRST_MONTH, SEASON_LAST_MONTH = 10, 4  # October .. April

# Bias correction of Open-Meteo to the City of London Barbican climate sensors (fitted on ~4,000 hourly readings,
# Oct 2024 - Oct 2026): corrected = b0 + b1*T + b2*clip(2 - T, 0, 4) + daily harmonics of the local hour.
BIAS_BETA = [0.4928, 0.9540, 0.5654, 0.2136, 0.1732, -0.0957, -0.0963]

# "Load A" switch-on curves from the archive Centron chart, per window (minutes after the window starts, threshold degC).
# Heating is expected while the outside temperature is below the curve.
LOAD_A = {"13:00": [(0, -1.0), (120, 10.4), (180, 4.5)],
          "20:30": [(0, -1.0), (210, 12.7), (300, 6.8)],
          "02:30": [(0, 5.7), (210, 15.0), (300, 11.1)]}


def season_start_year(d):
    """Season runs Oct of year Y to Apr of Y+1. Returns Y, or None for May-Sep dates."""
    if d.month >= SEASON_FIRST_MONTH:
        return d.year
    if d.month <= SEASON_LAST_MONTH:
        return d.year - 1
    return None


def load(csv_path):
    days = {}  # post_date -> {profile: {window_key: minutes}}
    keys = set()
    with open(csv_path, newline="") as f:
        for r in csv.DictReader(f):
            d = date.fromisoformat(r["post_date"])
            s = datetime.strptime(r["window_start"], "%Y-%m-%d %H:%M")
            e = datetime.strptime(r["window_end"], "%Y-%m-%d %H:%M")
            # window identity: clock times plus day offsets relative to the post date
            key = (s.strftime("%H:%M"), e.strftime("%H:%M"), (s.date() - d).days, (e.date() - d).days)
            keys.add(key)
            days.setdefault(d, {}).setdefault(r["profile"], {})[key] = float(r["minutes"])
    window_keys = sorted(keys, key=lambda k: (k[2], k[0]))
    return days, window_keys


def window_defs(window_keys):
    out = []
    for start, end, s_off, e_off in window_keys:
        sh, sm = map(int, start.split(":"))
        eh, em = map(int, end.split(":"))
        length = (e_off * 1440 + eh * 60 + em) - (s_off * 1440 + sh * 60 + sm)
        out.append({"label": f"{start}–{end}", "start": start, "end": end,
                    "startOff": s_off, "endOff": e_off, "len": length})
    return out


def num(v):
    return int(v) if float(v).is_integer() else v


TIME_FORMATS = ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%y %H:%M"]


def parse_time(text):
    text = text.strip().replace("Z", "").split("+")[0]
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    raise ValueError(f"unrecognised time stamp: {text!r}")


def load_temps(path, tz="UTC"):
    """Hourly temperatures as two parallel lists: UTC epoch seconds and deg C.

    The file needs a time column first and a temperature column second (any header names).
    tz says what the times are: "UTC" (default) or "London" (local clock time, already what the blog uses).
    """
    if not path or not Path(path).exists():
        return [], []
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.reader(f):
            if len(r) < 2 or r[1].strip() == "":
                continue
            try:
                t, v = parse_time(r[0]), float(r[1])
            except ValueError:
                continue  # header line or unusable row
            t = t.replace(tzinfo=timezone.utc) if tz.upper() == "UTC" else t.replace(tzinfo=LONDON)
            rows.append((t.timestamp(), v))
    rows.sort()
    return [t for t, _ in rows], [v for _, v in rows]


def correct_temps(times, temps):
    """Apply BIAS_BETA to hourly Open-Meteo temperatures (times are UTC epoch seconds)."""
    out = []
    for t, v in zip(times, temps):
        h = datetime.fromtimestamp(t, LONDON).hour
        w = 2 * math.pi * h / 24
        x = [1.0, v, min(max(2 - v, 0.0), 4.0), math.cos(w), math.sin(w), math.cos(2 * w), math.sin(2 * w)]
        out.append(round(sum(b * xi for b, xi in zip(BIAS_BETA, x)), 2))
    return out


def interp(xs, ys, x):
    if x <= xs[0]:
        return ys[0]
    for i in range(1, len(xs)):
        if x <= xs[i]:
            return ys[i - 1] + (ys[i] - ys[i - 1]) * (x - xs[i - 1]) / (xs[i] - xs[i - 1])
    return ys[-1]


def window_start(d, w):
    return datetime.combine(d, datetime.min.time()) + timedelta(days=w["startOff"], hours=int(w["start"][:2]), minutes=int(w["start"][3:]))


def load_a_minutes(d, w, times, temps):
    """Minutes the Load A curve would heat in window w of post day d (None without temperatures)."""
    pts = LOAD_A.get(w["start"])
    if not pts or not times:
        return None
    t0 = epoch(window_start(d, w))
    if t0 < times[0] or t0 + 60 * w["len"] > times[-1]:
        return None
    n = 0
    for m in range(w["len"]):
        t = t0 + 60 * (m + 0.5)
        i = bisect.bisect_right(times, t)
        if times[i] - times[i - 1] > 7200:
            return None
        tv = temps[i - 1] + (temps[i] - temps[i - 1]) * (t - times[i - 1]) / (times[i] - times[i - 1])
        n += tv < interp([q[0] for q in pts], [q[1] for q in pts], m + 0.5)
    return n


def day_records(days, window_keys, wdefs, times, temps):
    """Per post date: Adjusted minutes per window, Unbiased per window, Load A minutes per window (None without temps)."""
    out = {}
    for d in sorted(days):
        a = [num(days[d]["Adjusted"][k]) for k in window_keys]
        u = [num(days[d]["Unbiased"][k]) for k in window_keys]
        p = [load_a_minutes(d, w, times, temps) for w in wdefs]
        out[d.isoformat()] = a + u + p
    return out


def hourly_local(times, temps):
    """Hourly temperatures on the London wall clock: a start time and one value per hour (x10, None for gaps)."""
    if not times:
        return None
    loc = {}
    for t, v in zip(times, temps):
        k = datetime.fromtimestamp(t, LONDON).replace(tzinfo=None, minute=0, second=0)
        loc.setdefault(k, v)            # the repeated hour when the clocks go back keeps its first reading
    t0, t1 = min(loc), max(loc)
    vals, k = [], t0
    while k <= t1:
        v = loc.get(k)
        vals.append(None if v is None else round(v * 10))
        k += timedelta(hours=1)
    return {"start": t0.strftime("%Y-%m-%d %H:%M"), "v": vals}


def latest_day(days, window_keys, wdefs, times, temps):
    """Everything the 24-hour panel at the top needs: hourly temperatures, the Load A curves, actual and expected minutes."""
    d = max(days)
    base = datetime.combine(d, datetime.min.time())
    a, b = base - timedelta(days=1) + timedelta(hours=8), base + timedelta(hours=9)
    series = []
    if times:
        i = bisect.bisect_left(times, epoch(a))
        while i < len(times) and times[i] <= epoch(b):
            series.append([datetime.fromtimestamp(times[i], LONDON).strftime("%Y-%m-%d %H:%M"), temps[i]])
            i += 1
    wins = []
    for w, k in zip(wdefs, window_keys):
        s = window_start(d, w)
        pts = LOAD_A.get(w["start"])
        wins.append({"label": w["label"], "start": s.strftime("%Y-%m-%d %H:%M"), "len": w["len"],
                     "end": (s + timedelta(minutes=w["len"])).strftime("%Y-%m-%d %H:%M"),
                     "a": num(days[d]["Adjusted"][k]), "u": num(days[d]["Unbiased"][k]),
                     "pred": load_a_minutes(d, w, times, temps),
                     "curve": [[(s + timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M"), v] for m, v in pts] if pts else None,
                     "t0": temp_at(times, temps, s), "t1": temp_at(times, temps, s + timedelta(minutes=w["len"]))})
    # how the system has compared with Load A: this season so far, and the same calendar month in earlier seasons
    cur = season_start_year(d)
    recent = []
    for w, k in zip(wdefs, window_keys):
        sa = se = ma = me = 0.0; sn = 0
        for x in days:
            y = season_start_year(x)
            if y is None:
                continue
            p = load_a_minutes(x, w, times, temps)
            if p is None:
                continue
            if y == cur and x <= d:
                sa += days[x]["Adjusted"][k]; se += p; sn += 1
            elif y != cur and x.month == d.month:
                ma += days[x]["Adjusted"][k]; me += p
        recent.append({"label": w["label"], "seasonActual": round(sa), "seasonExpected": round(se), "seasonDays": sn,
                       "monthActual": round(ma), "monthExpected": round(me)})
    return {"date": d.isoformat(), "series": series, "windows": wins, "recent": recent,
            "post": (base + POST_TIME).strftime("%Y-%m-%d %H:%M")}


def epoch(local_dt):
    """Epoch seconds of a London wall-clock time."""
    return local_dt.replace(tzinfo=LONDON).timestamp()


def temp_at(times, temps, local_dt):
    """Temperature at a London wall-clock time, interpolated between the two surrounding hourly readings."""
    t = epoch(local_dt)
    i = bisect.bisect_right(times, t)
    if i == 0 or i == len(times) or times[i] - times[i - 1] > 7200:
        return None
    a, b = times[i - 1], times[i]
    return round(temps[i - 1] + (temps[i] - temps[i - 1]) * (t - a) / (b - a), 1)


def mean_temp(times, temps, start, end):
    """Average of the hourly readings in (start, end]; None unless nearly all hours are present."""
    a, b = epoch(start), epoch(end)
    i, j = bisect.bisect_right(times, a), bisect.bisect_right(times, b)
    n_expected = round((b - a) / 3600)
    return round(sum(temps[i:j]) / (j - i), 1) if j - i >= 0.8 * n_expected else None


def day_temps(d, wdefs, times, temps):
    """Temperature at the start of each window of post day d, then the average over the 24 h before 08:30."""
    base = datetime.combine(d, datetime.min.time())
    out = []
    for w in wdefs:
        s = base + timedelta(days=w["startOff"], hours=int(w["start"][:2]), minutes=int(w["start"][3:]))
        out.append(temp_at(times, temps, s))
    end = base + POST_TIME
    out.append(mean_temp(times, temps, end - timedelta(days=1), end))
    return out


def end_temps(d, wdefs, times, temps):
    """Temperature at the end of each window of post day d."""
    base = datetime.combine(d, datetime.min.time())
    return [temp_at(times, temps, base + timedelta(days=w["endOff"], hours=int(w["end"][:2]), minutes=int(w["end"][3:])))
            for w in wdefs]


def latest_summary(days, window_keys, wdefs, times, temps):
    """The most recent post (whatever the month), the day before it, and the average of the 7 days before."""
    def tot(d, p):
        return num(sum(days[d][p][k] for k in window_keys))
    d = max(days)
    wt = day_temps(d, wdefs, times, temps)
    out = {
        "date": d.isoformat(), "inSeason": season_start_year(d) is not None,
        "windows": [{"label": w["label"], "len": w["len"], "u": num(days[d]["Unbiased"][k]), "a": num(days[d]["Adjusted"][k]), "t": wt[i]}
                    for i, (w, k) in enumerate(zip(wdefs, window_keys))],
        "u": tot(d, "Unbiased"), "a": tot(d, "Adjusted"), "t": wt[-1], "prev": None, "avg": None,
    }
    pd_ = d - timedelta(days=1)
    if pd_ in days:
        out["prev"] = {"date": pd_.isoformat(), "u": tot(pd_, "Unbiased"), "a": tot(pd_, "Adjusted"),
                       "t": day_temps(pd_, wdefs, times, temps)[-1]}
    last7 = [x for x in (d - timedelta(days=i) for i in range(1, 8)) if x in days]
    if last7:
        out["avg"] = {"n": len(last7), "u": round(sum(tot(x, "Unbiased") for x in last7) / len(last7)),
                      "a": round(sum(tot(x, "Adjusted") for x in last7) / len(last7))}
    return out


def build_payload(csv_path, temp_path=None, temp_tz="UTC", correct=True):
    days, window_keys = load(csv_path)
    times, temps = load_temps(temp_path, temp_tz)
    if correct and times:
        temps = correct_temps(times, temps)   # every temperature in the app is the bias-corrected one
    wdefs = window_defs(window_keys)
    capped = []   # readings longer than their window cannot be real: cap them at the window length
    for d, profs in days.items():
        for p_, vals in profs.items():
            for k, w in zip(window_keys, wdefs):
                if vals[k] > w["len"]:
                    capped.append((d.isoformat(), p_, w["label"], num(vals[k])))
                    vals[k] = float(w["len"])
    seasons, ignored = {}, 0
    for d in sorted(days):
        y = season_start_year(d)
        if y is None:
            ignored += 1
            continue
        row = [d.isoformat()]
        for p in PROFILES:
            row += [num(days[d][p][k]) for k in window_keys]  # raises KeyError if a window is missing
        row += day_temps(d, wdefs, times, temps)  # temperature at the start of each window, then the 24 h average
        row += end_temps(d, wdefs, times, temps)  # then the temperature at the end of each window
        seasons.setdefault(y, []).append(row)
    payload = {
        "windows": wdefs,
        "profiles": PROFILES,
        "hasTemps": bool(times),
        "tempThrough": datetime.fromtimestamp(times[-1], LONDON).strftime("%Y-%m-%d") if times else None,
        "seasons": [
            {"id": str(y), "label": f"{y}–{str(y + 1)[2:]}", "rows": seasons[y]}
            for y in sorted(seasons, reverse=True)  # newest season first
        ],
        "cappedCount": len(capped),
        "cappedDays": len({c[0] for c in capped}),
        "latest": latest_summary(days, window_keys, wdefs, times, temps),
        "days": day_records(days, window_keys, wdefs, times, temps),
        "temps": hourly_local(times, temps),
        "loadA": {k: [list(p) for p in v] for k, v in LOAD_A.items()},
        "tempCorrected": bool(correct and times),
        "dataThrough": max(days).isoformat(),
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }
    return payload, ignored


TEMPLATE = r'''<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Barbican Underfloor Heating</title>
<style>
:root{
  color-scheme:light;
  --bg:#ffffff; --fg:#1f2937; --muted:#5b6472; --line:#e4e7ec; --panel:#f6f7f9;
  --accent:#1f3a5f; --on-accent:#ffffff;
  --u:#3f7fcf; --a:#ee8a24; --on:#111111; --off:#9aa1ad; --offline:#d3d7de;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1280px;margin:0 auto;padding:16px 16px 12px}
h1{font-size:1.35rem;margin:0 0 2px}
.controls{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:flex-end;margin-bottom:8px}
.ctl{display:flex;flex-direction:column;gap:3px}
.ctl>label,.ctl>.lab{font-size:.72rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
select,input[type=date],button{font:inherit;color:var(--fg);background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:6px 10px;min-height:34px}
button{cursor:pointer}
button:hover,select:hover,input:hover{border-color:var(--accent)}
:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.seg{display:inline-flex}
.seg button{border-radius:0;margin-left:-1px}
.seg button:first-child{border-radius:6px 0 0 6px;margin-left:0}
.seg button:last-child{border-radius:0 6px 6px 0}
.seg button[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.latest{border:1px solid var(--line);background:var(--panel);border-radius:10px;padding:14px 16px;margin:0 0 16px}
.latest h2{font-size:1.25rem;font-weight:500;margin:0 0 8px}
.latest h2 .rng{font-size:1.02rem;font-weight:700;margin-left:4px;white-space:nowrap}
.latest .when{color:var(--muted);font-size:.82rem;margin:0 0 8px}
.latest p{margin:6px 0}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px;margin:12px 0}
.tile{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px 12px}
.tile .k{font-size:.72rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.tile .v{font-size:1.5rem;font-weight:650;line-height:1.25}
.tile .d{font-size:.8rem;color:var(--muted)}
.latest table{border-collapse:collapse;width:100%;font-size:.88rem}
.latest th,.latest td{text-align:left;padding:4px 10px 4px 0;border-bottom:1px solid var(--line)}
.latest th{font-size:.72rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;font-weight:500}
.latest tr:last-child td{border-bottom:0}
.latest .warn{color:var(--muted);font-style:italic}
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--line);margin:8px 0 12px;flex-wrap:wrap}
.tabs button{border:0;border-bottom:3px solid transparent;border-radius:0;background:none;padding:8px 14px;font-weight:600;color:var(--muted);min-height:40px}
.tabs button[aria-selected=true]{color:var(--accent);border-bottom-color:var(--accent)}
.tabs button:hover{color:var(--fg)}
.shared{display:flex;justify-content:flex-end;margin:-6px 0 8px}
.ctl-inline{flex-direction:row;align-items:center;gap:8px}
.daynav{display:flex;align-items:center;gap:8px;margin:0 0 12px;flex-wrap:wrap}
.daynav button{min-width:40px;font-size:1rem}
.daynav button:disabled{opacity:.35;cursor:default}
.daynav .hint{color:var(--muted);font-size:.8rem}
.day{display:grid;grid-template-columns:minmax(0,1fr) 290px;gap:14px;align-items:start;margin-top:4px}
@media (max-width:860px){.day{grid-template-columns:1fr}}
.day24{width:100%;min-height:380px}
.dm{display:flex;flex-direction:column;gap:8px}
.dm-tot,.dm-w{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px 12px}
.dm-tot .k{font-size:.72rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.dm-tot .v{font-size:1.6rem;font-weight:650;line-height:1.25}
.dm .d{font-size:.78rem;color:var(--muted)}
.dm-w{display:grid;grid-template-columns:10px 1fr auto;gap:8px;align-items:center}
.dm-w .dot{width:10px;height:10px;border-radius:2px}
.dm-w .lab{font-weight:600;font-size:.88rem}
.dm-w .big{font-size:1.25rem;font-weight:650}
.latest .disc{font-size:.85rem;color:var(--fg);margin:10px 0 0}
#chart{width:100%;height:min(86vh,880px);min-height:640px}
.opt{display:inline-flex;align-items:center;gap:6px;color:var(--muted);font-size:.78rem;cursor:pointer;margin:6px 0 2px}
.opt input{accent-color:#7b8494;margin:0;cursor:pointer}
footer{color:var(--muted);font-size:.78rem;margin-top:6px}
noscript{display:block;padding:12px;color:var(--muted)}
</style>
</head>
<body>
<main>
  <h1>Barbican underfloor heating</h1>
  <nav class="tabs" role="tablist" aria-label="Views">
    <button type="button" role="tab" id="tb-latest" aria-controls="tab-latest" aria-selected="true">Latest</button>
    <button type="button" role="tab" id="tb-day" aria-controls="tab-day" aria-selected="false" tabindex="-1">Explore a day</button>
    <button type="button" role="tab" id="tb-trends" aria-controls="tab-trends" aria-selected="false" tabindex="-1">Long-term</button>
  </nav>
  <div class="shared">    <div class="ctl ctl-inline"><span class="lab">Units</span>
      <div class="seg" id="units" role="group" aria-label="Units">
        <button type="button" data-v="pct" aria-pressed="true">% of window</button>
        <button type="button" data-v="min" aria-pressed="false">Minutes</button>
      </div></div>
  </div>
  <section class="tabp" id="tab-latest" role="tabpanel" aria-labelledby="tb-latest"><div class="latest" id="latest"></div></section>
  <section class="tabp" id="tab-day" role="tabpanel" aria-labelledby="tb-day" hidden>
    <div class="daynav"><button type="button" id="dprev" aria-label="Previous day">◀</button><input type="date" id="dpick" aria-label="Choose a day"><button type="button" id="dnext" aria-label="Next day">▶</button><span class="hint">or use the ← → keys</span></div>
    <div class="latest" id="dayx"></div>
  </section>
  <section class="tabp" id="tab-trends" role="tabpanel" aria-labelledby="tb-trends" hidden>
  <div class="controls">
    <div class="ctl"><label for="season">Heating season</label><select id="season"></select></div>
    <div class="ctl"><label for="month">Month</label><select id="month"></select></div>
    <div class="ctl"><label for="from">From</label><input type="date" id="from"></div>
    <div class="ctl"><label for="to">To</label><input type="date" id="to"></div>
  </div>
  <div id="chart"></div>
  </section>
  <label class="opt"><input type="checkbox" id="unb" autocomplete="off"> Include unbiased data</label>
  <footer id="foot"></footer>
  <noscript>This dashboard needs JavaScript.</noscript>
</main>
__PLOTLY_TAG__
<script>
(function(){
'use strict';
const DATA = __PAYLOAD__;
const DAY = 86400000, HOUR = 3600000, MIN = 60000;
const $ = id => document.getElementById(id);
const gd = $('chart');
const p2 = n => String(n).padStart(2, '0');
const DOW = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

// Naive local date-times are handled as UTC milliseconds, so clock changes never shift anything.
const ms = (iso, hm) => { const a = iso.split('-').map(Number), b = (hm || '00:00').split(':').map(Number); return Date.UTC(a[0], a[1]-1, a[2], b[0], b[1]); };
const hmMs = t => { const b = t.split(':').map(Number); return (b[0]*60 + b[1]) * MIN; };
const dstr = t => { const d = new Date(t); return d.getUTCFullYear() + '-' + p2(d.getUTCMonth()+1) + '-' + p2(d.getUTCDate()); };
const hhmm = t => { const d = new Date(t); return p2(d.getUTCHours()) + ':' + p2(d.getUTCMinutes()); };
const stamp = t => dstr(t) + ' ' + hhmm(t);
const dayName = t => { const d = new Date(t); return DOW[d.getUTCDay()] + ' ' + d.getUTCDate() + ' ' + MON[d.getUTCMonth()]; };
const niceT = t => dayName(t) + ' ' + hhmm(t);
const niceD = t => dayName(t) + ' ' + new Date(t).getUTCFullYear();
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const axisMs = v => { if (typeof v === 'number') return v; let s = String(v).replace(' ', 'T'); if (s.indexOf('T') < 0) s += 'T00:00'; return Date.parse(s + 'Z'); };
const sum = a => a.reduce((p, c) => p + c, 0);

const W = DATA.windows;
const SUM_LEN = sum(W.map(w => w.len));
const state = { season: DATA.seasons[0].id, unbiased: false, units: 'pct' };
const getSeason = () => DATA.seasons.filter(s => s.id === state.season)[0];
const bounds = S => ({ first: ms(S.rows[0][0]), last: ms(S.rows[S.rows.length - 1][0]) });
// months of a season: each starts at noon on the last day of the previous month, the edge between two post days
const MON3 = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
function seasonMonths(S){
  const seen = [];
  S.rows.forEach(r => { const k = r[0].slice(0, 7); if (seen.indexOf(k) < 0) seen.push(k); });
  return seen.map(k => { const y = +k.slice(0, 4), m = +k.slice(5, 7); const a = Date.UTC(y, m - 1, 1) - 12*HOUR, b = Date.UTC(y, m, 1) - 12*HOUR;
    return { key: k, label: MON3[m - 1], full: MON3[m - 1] + ' ' + y, t: a, a: a, b: b }; });
}
function monthMarks(S){ return seasonMonths(S); }
const monthRange = (S, key) => { const b = bounds(S), m = seasonMonths(S).filter(x => x.key === key)[0];
  return [stamp(Math.max(m.a, b.first - 12*HOUR)), stamp(Math.min(m.b, b.last + 12*HOUR))]; };
const wholeRange = S => { const b = bounds(S); return [stamp(b.first - 12*HOUR), stamp(b.last + 12*HOUR)]; };

function build(){
  const S = getSeason(), pct = state.units === 'pct';
  const showU = state.unbiased, showA = true, both = showU;     // the standard figures are always shown; unbiased only when ticked
  // one colour per window (the same in every row of the upper plot); the unbiased bars use a lighter shade of it.
  // The daily totals below use their own colour so they cannot be mistaken for any one window.
  const WCOL = { '13:00': '#f0b429', '20:30': '#d8512e', '02:30': '#3f7fcf' }, FALL = ['#f0b429', '#d8512e', '#3f7fcf', '#7a5cc7'];
  const tint = (hex, f) => { const n = parseInt(hex.slice(1), 16); return 'rgb(' + [n >> 16, (n >> 8) & 255, n & 255].map(c => Math.round(c + (255 - c) * f)).join(',') + ')'; };
  const wcol = W.map((w, k) => WCOL[w.start] || FALL[k % FALL.length]);
  const DCOL = '#1f8a70';
  const con = css('--on'), coff = css('--off'), coffl = css('--offline');
  const val = (m, len) => pct ? 100 * m / len : m;

  // ---- hover texts (minutes view / percentage view)
  const HT_WIN = pct
    ? '<b>%{customdata[0]} window</b><br>%{customdata[1]}<br><b>%{customdata[3]}%</b> of the window (%{customdata[5]} of %{customdata[2]} min)%{customdata[4]}<extra>%{fullData.name}</extra>'
    : '<b>%{customdata[0]} window</b><br>%{customdata[1]}<br><b>%{customdata[5]} min</b> of %{customdata[2]} (%{customdata[3]}%)%{customdata[4]}<extra>%{fullData.name}</extra>';
  const HT_TOT = pct
    ? '<b>%{customdata[0]}</b><br>Total <b>%{customdata[3]}%</b> of all windows (%{customdata[2]} of ' + SUM_LEN + ' min)<br>' +
      W.map((w, k) => w.label + ': %{customdata[' + (4 + W.length + k) + ']}% (%{customdata[' + (4 + k) + ']} min)').join('<br>') + '<extra>%{fullData.name}</extra>'
    : '<b>%{customdata[0]}</b><br>Total <b>%{customdata[2]} min</b> (%{customdata[1]} h)<br>' +
      W.map((w, k) => w.label + ': %{customdata[' + (4 + k) + ']} min').join('<br>') + '<extra>%{fullData.name}</extra>';

  const hasT = DATA.hasTemps, nW = W.length, iT = 1 + 2 * nW;   // row = [date, U x nW, A x nW, temp at window start x nW, mean temp over the 24 h, temp at window end x nW]
  // "heating on" always refers to the standard figures, so the temperature line does not change when unbiased data is added
  const onFn = (u, a) => a > 0;
  const stateTxt = (u, a) => (a > 0 ? 'Heating on: ' + a + ' min' : 'Heating off') + (showU ? ' · unbiased ' + u + ' min' : '');

  // ---- windows: heating minutes as bars (zero = no bar); temperature at the window start as a line, grey/red by heating off/on
  const wb = { c: [], span: [], u: [], a: [], cdU: [], cdA: [], k: [] }, wp = [];
  S.rows.forEach(r => {
    const d0 = ms(r[0]);
    W.forEach((w, k) => {
      const s = d0 + w.startOff * DAY + hmMs(w.start), e = d0 + w.endOff * DAY + hmMs(w.end);
      const sameDay = dstr(s) === dstr(e);
      const when = niceT(s) + ' → ' + (sameDay ? hhmm(e) : niceT(e));
      const u = r[1 + k], a = r[1 + nW + k];
      wb.c.push(stamp((s + e) / 2)); wb.span.push(e - s); wb.k.push(k);
      wb.u.push(u > 0 ? val(u, w.len) : null); wb.a.push(a > 0 ? val(a, w.len) : null);
      wb.cdU.push([w.label, when, w.len, Math.round(100 * u / w.len), '', u]);
      wb.cdA.push([w.label, when, w.len, Math.round(100 * a / w.len), '', a]);
      const tv = r[iT + k];
      const te = r[iT + nW + 1 + k], ok = x => x !== null && x !== undefined;
      if (ok(tv) || ok(te)) {
        const f = x => x.toFixed(1), tx = ok(tv) && ok(te) ? 'start ' + f(tv) + ' °C → end ' + f(te) + ' °C' : f(ok(tv) ? tv : te) + ' °C (' + (ok(tv) ? 'start' : 'end') + ' of window)';
        wp.push({ t: s, c: (s + e) / 2, span: e - s, v: ok(tv) ? tv : null, ve: ok(te) ? te : null, on: onFn(u, a), cd: [w.label, when, stateTxt(u, a), tx] });
      }
    });
  });
  wp.sort((p, q) => p.t - q.t);

  // ---- daily totals: one bar per day covering the 24 h before the 08:30 post; empty days left out
  const bd = bounds(S), byDate = {};
  S.rows.forEach(r => { byDate[ms(r[0])] = r; });
  const db = { c: [], u: [], a: [], cdU: [], cdA: [] }, dp = [];
  const cdTot = (t, tot, ws) => [niceD(t), (tot / 60).toFixed(1), tot, Math.round(100 * tot / SUM_LEN)]
    .concat(ws, ws.map((m, k) => Math.round(100 * m / W[k].len)));
  for (let t = bd.first; t <= bd.last; t += DAY) {
    const r = byDate[t];
    if (!r) continue;
    const uw = r.slice(1, 1 + nW), aw = r.slice(1 + nW, 1 + 2*nW), ut = sum(uw), at = sum(aw);
    const c = t - 3*HOUR - 30*MIN;                     // the 24 h before 08:30 are centred on 20:30 the evening before
    db.c.push(stamp(c)); db.u.push(ut > 0 ? val(ut, SUM_LEN) : null); db.a.push(at > 0 ? val(at, SUM_LEN) : null);
    db.cdU.push(cdTot(t, ut, uw)); db.cdA.push(cdTot(t, at, aw));
    const tv = r[iT + nW];
    if (tv !== null && tv !== undefined) dp.push({ t: c, v: tv, on: onFn(ut, at), cd: [niceD(t), stateTxt(ut, at)] });
  }
  curSpans = { w: wb.span, d: db.c.map(() => DAY) };

  const tRange = pts => {   // temperature axis: exactly the coldest to warmest reading shown, plus 1 degree each side
    const v = pts.map(p => p.v);
    if (!v.length) return undefined;
    return [Math.floor(Math.min.apply(null, v)) - 1, Math.ceil(Math.max.apply(null, v)) + 1];
  };
  const trW = tRange(wp.map(p => ({ v: p.v !== null ? p.v : p.ve })).concat(wp.filter(p => p.ve !== null).map(p => ({ v: p.ve })))), trD = tRange(dp);
  const HT_TW = '<b>%{customdata[0]} window</b><br>%{customdata[1]}<br>Temperature at the start of the window: <b>%{y:.1f} °C</b><br>%{customdata[2]}<extra></extra>';
  const HT_TWS = '<b>%{customdata[0]} window</b><br>%{customdata[1]}<br>Temperature: <b>%{customdata[3]}</b><br>%{customdata[2]}<extra></extra>';
  const HT_TD = '<b>%{customdata[0]}</b><br>Average temperature over the 24 h before 08:30: <b>%{y:.1f} °C</b><br>%{customdata[1]}<extra></extra>';

  // temperature line: each stretch takes the colour of the point it starts from (heating on / off in that window or day)
  const segs = (pts, gap) => {
    const on = { x: [], y: [] }, off = { x: [], y: [] };
    for (let i = 0; i + 1 < pts.length; i++) {
      const p = pts[i], q = pts[i + 1];
      if (q.t - p.t > gap) continue;                         // missing days = a break
      const o = p.on ? on : off;
      o.x.push(stamp(p.t), stamp(q.t), null); o.y.push(p.v, q.v, null);
    }
    return { on: on, off: off };
  };
  const lineTr = (name, grp, col, sg, show, xa, ya, on) => ({ type: 'scatter', mode: 'lines', name: name, legendgroup: grp, showlegend: show,
    x: sg.x, y: sg.y, line: { color: col, width: 1.4 }, hoverinfo: 'skip', connectgaps: false, xaxis: xa, yaxis: ya, meta: { kind: 'tline', on: on, day: xa === 'x2' } });
  const ptTr = (grp, col, pts, ht, xa, ya, on) => ({ type: 'scatter', mode: 'markers', name: grp, legendgroup: grp, showlegend: false,
    x: pts.map(p => stamp(p.t)), y: pts.map(p => p.v), customdata: pts.map(p => p.cd),
    marker: { color: col, size: 4, line: { width: 0 } }, hovertemplate: ht, xaxis: xa, yaxis: ya, meta: { kind: 'tpt', on: on, day: xa === 'x2' } });
  const barTr = (name, grp, col, x, y, cd, ht, xa, ya, day, idx, n, show) => ({ type: 'bar', name: name, legendgroup: grp, showlegend: false,
    x: x, y: y, customdata: cd, marker: { color: col, opacity: 0.9, line: { width: 0 } }, hovertemplate: ht, xaxis: xa, yaxis: ya,
    meta: { kind: 'bar', day: day, idx: idx, n: n } });

  const nSer = both ? 2 : 1, data = [];
  const bd0 = bounds(S);
  // legend: one swatch per window (empty traces, only there to label the colours)
  W.forEach((w, k) => data.push({ type: 'scatter', mode: 'markers', name: w.label, legendgroup: 'W' + k, showlegend: true, x: [stamp(bd0.first)], y: [null], marker: { color: wcol[k], symbol: 'square', size: 11 }, hoverinfo: 'skip', xaxis: 'x', yaxis: 'y' }));
  if (showU) data.push({ type: 'scatter', mode: 'markers', name: 'Unbiased (lighter shade)', legendgroup: 'U', showlegend: true, x: [stamp(bd0.first)], y: [null], marker: { color: tint('#7b8494', 0.55), symbol: 'square', size: 11 }, hoverinfo: 'skip', xaxis: 'x', yaxis: 'y' });
  data.push(barTr('Heating', 'A', wb.k.map(k => wcol[k]), wb.c, wb.a, wb.cdA, HT_WIN, 'x', 'y', false, 0, nSer, true));
  if (showU) data.push(barTr('Unbiased', 'U', wb.k.map(k => tint(wcol[k], 0.55)), wb.c, wb.u, wb.cdU, HT_WIN, 'x', 'y', false, 1, nSer, true));
  data.push(barTr('Heating', 'A', DCOL, db.c, db.a, db.cdA, HT_TOT, 'x2', 'y2', true, 0, nSer, false));
  if (showU) data.push(barTr('Unbiased', 'U', tint(DCOL, 0.55), db.c, db.u, db.cdU, HT_TOT, 'x2', 'y2', true, 1, nSer, false));
  if (hasT) {
    const sw = segs(wp, 20*HOUR), sd = segs(dp, 36*HOUR);
    data.push(lineTr('Temperature, heating on', 'Ton', con, sw.on, true, 'x', 'y3', true));
    data.push(lineTr('Temperature, heating off', 'Toff', coffl, sw.off, true, 'x', 'y3', false));
    data.push(lineTr('Temperature, heating on', 'Ton', con, sd.on, false, 'x2', 'y4', true));
    data.push(lineTr('Temperature, heating off', 'Toff', coffl, sd.off, false, 'x2', 'y4', false));
    data.push(ptTr('Toff', coff, wp.filter(p => !p.on), HT_TW, 'x', 'y3', false));
    data.push(ptTr('Ton', con, wp.filter(p => p.on), HT_TW, 'x', 'y3', true));
    data.push(ptTr('Toff', coff, dp.filter(p => !p.on), HT_TD, 'x2', 'y4', false));
    data.push(ptTr('Ton', con, dp.filter(p => p.on), HT_TD, 'x2', 'y4', true));
    // zoomed in: each window gets its own start-to-end line across the width of its bar (black = heating on), or one grey dot at its centre
    data.push({ type: 'scatter', mode: 'lines+markers', name: 'Temperature, heating on', legendgroup: 'Ton', showlegend: true, x: [], y: [], connectgaps: false,
      line: { color: con, width: 2 }, marker: { color: con, size: 5, line: { width: 0 } }, hovertemplate: HT_TWS, xaxis: 'x', yaxis: 'y3', meta: { kind: 'wseg', on: true } });
    data.push({ type: 'scatter', mode: 'markers', name: 'Temperature, heating off', legendgroup: 'Toff', showlegend: true, x: [], y: [],
      marker: { color: coff, size: 4, line: { width: 0 } }, hovertemplate: HT_TWS, xaxis: 'x', yaxis: 'y3', meta: { kind: 'wseg', on: false } });
  }

  const fg = css('--fg'), grid = css('--line'), panel = css('--panel');
  const D1 = [0.42, 1], D2 = [0.07, 0.34];
  // heating axes (left, bars) are the base; the temperature axes (right) overlay them so the line draws over the bars
  const tAxis = (dom, anchor, over, rng, title) => ({ domain: dom, anchor: anchor, overlaying: over, side: 'right', range: rng, visible: hasT, showgrid: false,
    zeroline: !!(rng && rng[0] < 0), zerolinecolor: '#9db4d6', zerolinewidth: 1, fixedrange: true, ticksuffix: ' °C', title: { text: title } });
  const hAxis = (dom, anchor, title) => ({ domain: dom, anchor: anchor, side: 'left', rangemode: 'tozero', gridcolor: grid, zeroline: false, fixedrange: true,
    title: { text: title }, ticksuffix: pct ? '%' : '' });
  const layout = {
    uirevision: state.season + '|' + state.unbiased + '|' + state.units,
    barmode: 'overlay', hovermode: 'closest', dragmode: 'zoom',
    margin: { l: 66, r: hasT ? 74 : 16, t: 36, b: 10 },
    paper_bgcolor: '#ffffff', plot_bgcolor: '#ffffff',
    font: { color: fg, size: 12, family: 'system-ui,-apple-system,"Segoe UI",Roboto,sans-serif' },
    hoverlabel: { bgcolor: '#ffffff', bordercolor: grid, font: { color: fg } },
    legend: { orientation: 'h', x: 0, y: 1, yanchor: 'bottom' },
    xaxis: { type: 'date', anchor: 'y', gridcolor: grid, linecolor: grid, showticklabels: false },
    xaxis2: { type: 'date', anchor: 'y2', matches: 'x', gridcolor: grid, linecolor: grid },
    yaxis: hAxis(D1, 'x', pct ? 'Heating as % of window length' : 'Minutes heating in window'),
    yaxis2: hAxis(D2, 'x2', pct ? 'Daily total (% of ' + SUM_LEN + ' min)' : 'Daily total (min)'),
    yaxis3: tAxis(D1, 'x', 'y', trW, 'Temperature at start of window'),
    yaxis4: tAxis(D2, 'x2', 'y2', trD, 'Average temp, 24 h'),
    shapes: monthMarks(S).map(m => ({ type: 'line', xref: 'x', yref: 'paper', x0: stamp(m.t), x1: stamp(m.t), y0: 0, y1: 1, line: { color: '#b8bec8', width: 1, dash: 'dot' }, layer: 'below' })),
    annotations: monthMarks(S).map(m => ({ x: stamp(m.t), xref: 'x', y: 0.995, yref: 'paper', xanchor: 'left', yanchor: 'top', text: ' ' + m.label, showarrow: false, font: { size: 11, color: '#7b8494' } }))
  };
  return { data: data, layout: layout, S: S, wp: wp, n: nSer, tv: { wx: wp.map(p => p.t), wv: wp.map(p => p.v), wxe: wp.filter(p => p.ve !== null).map(p => p.t), wve: wp.filter(p => p.ve !== null).map(p => p.ve), dx: dp.map(p => p.t), dv: dp.map(p => p.v) } };
}

// ===================== one-day panel (used by the "Latest" and "Explore a day" tabs) =====================
const WCOLS = { '13:00': '#f0b429', '20:30': '#d8512e', '02:30': '#3f7fcf' };
const colOfW = w => WCOLS[w.start] || '#7a5cc7';
const DAYS = DATA.days, DAY_KEYS = Object.keys(DATA.days).sort();
const TT = DATA.temps ? { t0: axisMs(DATA.temps.start), v: DATA.temps.v } : null;
const tempAtHour = t => { if (!TT) return null; const i = Math.round((t - TT.t0) / HOUR); const v = TT.v[i]; return v === null || v === undefined ? null : v / 10; };
const hourlyBetween = (a, b) => { const out = []; for (let t = Math.ceil(a / HOUR) * HOUR; t <= b; t += HOUR) { const v = tempAtHour(t); if (v !== null) out.push([t, v]); } return out; };
const inSeasonDate = iso => { const m = +iso.slice(5, 7); return m >= 10 || m <= 4; };
const seasonOf = iso => { const y = +iso.slice(0, 4), m = +iso.slice(5, 7); return m >= 10 ? y : (m <= 4 ? y - 1 : null); };

function dayData(iso){
  const r = DAYS[iso], nW = W.length, d0 = ms(iso);
  const wins = W.map((w, k) => { const s = d0 + w.startOff * DAY + hmMs(w.start); return { def: w, label: w.label, len: w.len, start: s, end: s + w.len * MIN, a: r[k], u: r[nW + k], pred: r[2 * nW + k] }; });
  const post = d0 + 8 * HOUR + 30 * MIN;
  const S = hourlyBetween(post - DAY - 30 * MIN, post + 30 * MIN);
  const inDay = S.filter(p => p[0] > post - DAY && p[0] <= post);
  const tAt = t => { if (!S.length) return null; if (t <= S[0][0]) return S[0][1]; for (let i = 1; i < S.length; i++) if (t <= S[i][0]) return S[i-1][1] + (S[i][1] - S[i-1][1]) * (t - S[i-1][0]) / (S[i][0] - S[i-1][0]); return S[S.length-1][1]; };
  wins.forEach(w => { w.t0 = S.length ? tAt(w.start) : null; w.t1 = S.length ? tAt(w.end) : null; });
  return { iso, post, S, tAt, wins, tmean: inDay.length >= 20 ? sum(inDay.map(p => p[1])) / inDay.length : null };
}

// actual minutes as a share of Load A: this season up to the day, and the same month in other seasons
function loadAContext(iso){
  const nW = W.length, se = seasonOf(iso), mo = iso.slice(5, 7);
  return W.map((w, k) => {
    let sa = 0, sp = 0, sn = 0, ma = 0, mp = 0;
    DAY_KEYS.forEach(d => {
      const r = DAYS[d], p = r[2 * nW + k], s2 = seasonOf(d);
      if (p === null || s2 === null) return;
      if (s2 === se && d <= iso) { sa += r[k]; sp += p; sn++; }
      else if (s2 !== se && d.slice(5, 7) === mo) { ma += r[k]; mp += p; }
    });
    return { label: w.label, sa, sp, sn, ma, mp };
  });
}

function dayPanel(el, iso, plotId, isLatest){
  const D = dayData(iso), pct = state.units === 'pct', inc = state.unbiased, dayT = ms(iso);
  const mins = m => Math.round(m) + ' min', pc = (m, n) => Math.round(100 * m / n) + '%';
  const deg = t => t === null || t === undefined ? '–' : t.toFixed(1) + ' °C';
  const sgnMin = d => (d > 0 ? '+' : d < 0 ? '−' : '±') + Math.abs(Math.round(d)) + ' min';
  const wins = D.wins, totA = sum(wins.map(w => w.a)), totU = sum(wins.map(w => w.u));
  const totP = wins.every(w => w.pred !== null) ? sum(wins.map(w => w.pred)) : null;
  const notes = [];
  if (isLatest) { const age = Math.round((axisMs(DATA.built.slice(0, 10)) - dayT) / DAY); if (age > 2) notes.push('the latest data is ' + age + ' days old'); }
  if (!inSeasonDate(iso)) notes.push('outside the Oct–Apr heating season');
  if (!D.S.length) notes.push('no temperature data for this day');

  const big = (m, n) => pct ? pc(m, n) : mins(m), small = (m, n) => pct ? mins(m) + ' of ' + n : pc(m, n) + ' of ' + n + ' min';
  let dm = '<div class="dm-tot"><div class="k">Heating, all windows</div><div class="v">' + big(totA, SUM_LEN) + '</div>'
    + '<div class="d">' + small(totA, SUM_LEN) + (inc ? ' · unbiased ' + mins(totU) : '') + '</div>'
    + (totP !== null ? '<div class="d">Load A expected ' + mins(totP) + ' (actual ' + sgnMin(totA - totP) + ')</div>' : '')
    + (D.tmean !== null ? '<div class="d">Average outside temperature ' + deg(D.tmean) + '</div>' : '') + '</div>';
  dm += wins.map(w => {
    const dif = w.pred === null ? '' : ' · Load A ' + mins(w.pred) + (w.pred !== w.a ? ' (' + sgnMin(w.a - w.pred) + ')' : ', matched');
    return '<div class="dm-w"><span class="dot" style="background:' + colOfW(w.def) + '"></span><div><div class="lab">' + w.label + '</div>'
      + '<div class="d">' + (pct ? mins(w.a) + ' of ' + w.len : pc(w.a, w.len) + ' of the window') + dif + (inc ? ' · unbiased ' + mins(w.u) : '') + '</div>'
      + '<div class="d">' + deg(w.t0) + ' → ' + deg(w.t1) + '</div></div><div class="big">' + (pct ? pc(w.a, w.len) : mins(w.a)) + '</div></div>';
  }).join('');

  let disc = '';
  if (inSeasonDate(iso)) {
    const R = loadAContext(iso), ratio = (a, e) => Math.round(100 * a / e) + '%';
    const MONTHS = ['January','February','March','April','May','June','July','August','September','October','November','December'];
    const ses = R.filter(r => r.sp > 30).map(r => r.label + ' ' + ratio(r.sa, r.sp)), typ = R.filter(r => r.mp > 300).map(r => r.label + ' ' + ratio(r.ma, r.mp));
    if (totP !== null) disc += 'In these 24 hours the system heated ' + (totA === totP ? 'exactly what Load A gives' : Math.abs(totA - totP) + ' min ' + (totA < totP ? 'less' : 'more') + ' than Load A would (' + Math.round(100 * totA / Math.max(totP, 1)) + '% of it)') + '. ';
    if (ses.length) disc += 'Season to this date (' + R[0].sn + ' days), actual as a share of Load A: ' + ses.join(', ') + '. ';
    if (typ.length) disc += 'In ' + MONTHS[+iso.slice(5, 7) - 1] + ' in other seasons: ' + typ.join(', ') + '. ';
    disc += 'The system usually runs below Load A after warm or sunny afternoons, most of all in spring.';
  }
  el.innerHTML = '<h2>' + (isLatest ? 'Most recent heating window' : 'Heating on ' + niceD(dayT)) + ' <br> <b class="rng">' + niceT(dayT - DAY + 8*HOUR + 30*MIN) + ' to ' + niceT(dayT + 8*HOUR + 30*MIN) + '</b></h2>'
    + (notes.length ? '<p class="when"><span class="warn">' + notes.join(' · ') + '</span></p>' : '')
    + '<div class="day"><div id="' + plotId + '" class="day24" role="img" aria-label="Outside temperature over these 24 hours against the Load A heating curves"></div><div class="dm">' + dm + '</div></div>'
    + (disc ? '<p class="disc">' + disc + '</p>' : '');
  drawDay(plotId, D);
}

function drawDay(plotId, D){
  const gd2 = $(plotId); if (!gd2) return;
  const S = D.S, start = D.post - DAY - 30 * MIN, end = D.post + 30 * MIN;
  const tint = (hex, a) => { const n = parseInt(hex.slice(1), 16); return 'rgba(' + (n >> 16) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + a + ')'; };
  const data = [], shapes = [], ann = [];
  let lo = Infinity, hi = -Infinity;
  S.forEach(p => { lo = Math.min(lo, p[1]); hi = Math.max(hi, p[1]); });
  D.wins.forEach(w => {
    const c = colOfW(w.def), ws = w.start, we = w.end, pts = DATA.loadA[w.def.start];
    shapes.push({ type: 'rect', xref: 'x', yref: 'paper', x0: stamp(ws), x1: stamp(we), y0: 0.25, y1: 1, fillcolor: tint(c, 0.07), line: { width: 0 }, layer: 'below' });
    ann.push({ x: stamp((ws + we) / 2), y: 1.0, xref: 'x', yref: 'paper', yanchor: 'bottom', text: '<b>' + w.label + '</b>', showarrow: false, font: { size: 11, color: c } });
    if (pts) {
      const cx = pts.map(p => ws + p[0] * MIN), cy = pts.map(p => p[1]);
      cy.forEach(v => { lo = Math.min(lo, v); hi = Math.max(hi, v); });
      if (S.length) {   // shading = when Load A expects heating (outside temperature below the curve)
        const px = [], py = []; let run = null;
        const flush = () => { if (run && run.length > 1) { run.forEach(q => { px.push(stamp(q[0])); py.push(q[1]); }); run.slice().reverse().forEach(q => { px.push(stamp(q[0])); py.push(q[2]); }); px.push(null); py.push(null); } run = null; };
        for (let t = ws; t <= we; t += MIN) {
          let k = 1; while (k < cx.length - 1 && t > cx[k]) k++;
          const cv = cy[k-1] + (cy[k] - cy[k-1]) * (t - cx[k-1]) / (cx[k] - cx[k-1]), tv = D.tAt(t);
          if (tv !== null && tv < cv) (run = run || []).push([t, cv, tv]); else flush();
        }
        flush();
        if (px.length) data.push({ type: 'scatter', mode: 'lines', x: px, y: py, fill: 'toself', fillcolor: tint(c, 0.28), line: { width: 0 }, hoverinfo: 'skip', showlegend: false });
      }
      data.push({ type: 'scatter', mode: 'lines', x: cx.map(stamp), y: cy, line: { color: c, width: 2.4 }, showlegend: false, hovertemplate: 'Load A switch-on threshold %{y:.1f} °C<extra>' + w.label + '</extra>' });
      [[cx[0], cy[0]], [cx[cx.length-1], cy[cy.length-1]]].forEach(e => data.push({ type: 'scatter', mode: 'lines', x: [stamp(e[0]), stamp(e[0])], y: [-50, e[1]], line: { color: c, width: 1, dash: 'dot' }, hoverinfo: 'skip', showlegend: false }));
    }
    shapes.push({ type: 'rect', xref: 'x', yref: 'y2', x0: stamp(ws), x1: stamp(ws + w.a * MIN), y0: 1.12, y1: 1.82, fillcolor: c, line: { width: 0 } });
    if (w.pred !== null) shapes.push({ type: 'rect', xref: 'x', yref: 'y2', x0: stamp(ws), x1: stamp(ws + w.pred * MIN), y0: 0.18, y1: 0.88, fillcolor: tint(c, 0.25), line: { color: c, width: 1.2 } });
    shapes.push({ type: 'line', xref: 'x', yref: 'y2', x0: stamp(we), x1: stamp(we), y0: 0.05, y1: 1.95, line: { color: '#9aa1ad', width: 1, dash: 'dot' } });
    ann.push({ x: stamp(ws + w.a * MIN), y: 1.47, xref: 'x', yref: 'y2', xanchor: 'left', text: ' ' + w.a, showarrow: false, font: { size: 10, color: '#1f2937' } });
    if (w.pred !== null) ann.push({ x: stamp(ws + w.pred * MIN), y: 0.53, xref: 'x', yref: 'y2', xanchor: 'left', text: ' ' + w.pred, showarrow: false, font: { size: 10, color: '#5b6472' } });
  });
  if (S.length) data.push({ type: 'scatter', mode: 'lines+markers', x: S.map(p => stamp(p[0])), y: S.map(p => p[1]), line: { color: '#111', width: 2 }, marker: { size: 4, color: '#111' },
    name: 'Outside temperature (corrected)', hovertemplate: '%{x|%H:%M} · <b>%{y:.1f} °C</b><extra>outside</extra>' });
  data.push({ type: 'scatter', mode: 'lines', x: [stamp(start)], y: [null], line: { color: '#7b8494', width: 2.4 }, name: 'Load A switch-on curve' });
  data.push({ type: 'scatter', mode: 'markers', x: [stamp(start)], y: [null], marker: { symbol: 'square', size: 11, color: 'rgba(123,132,148,0.35)' }, name: 'Heating expected by Load A' });
  if (!isFinite(lo)) { lo = 0; hi = 15; }
  const layout = {
    height: 380, margin: { l: 44, r: 10, t: 26, b: 34 }, paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: '#ffffff', dragmode: false, hovermode: 'closest',
    font: { color: css('--fg'), size: 11, family: 'system-ui,-apple-system,"Segoe UI",Roboto,sans-serif' },
    hoverlabel: { bgcolor: '#ffffff', bordercolor: css('--line'), font: { color: css('--fg') } },
    legend: { orientation: 'h', x: 0, y: -0.09, yanchor: 'top', font: { size: 10.5 } },
    xaxis: { type: 'date', range: [stamp(start), stamp(end)], fixedrange: true, tickformat: '%H:%M', dtick: (gd2.clientWidth < 600 ? 4 : 2) * HOUR, tickangle: 0, gridcolor: css('--line'), anchor: 'y2' },
    yaxis: { domain: [0.25, 1], range: [Math.floor(Math.min(lo, -1)) - 1, Math.ceil(hi) + 1], fixedrange: true, ticksuffix: '°', gridcolor: css('--line'), zeroline: true, zerolinecolor: '#c9ced6' },
    yaxis2: { domain: [0, 0.17], range: [0, 2], fixedrange: true, tickvals: [0.53, 1.47], ticktext: ['Load A', 'Actual'], showgrid: false, zeroline: false, tickfont: { size: 10 } },
    shapes: shapes, annotations: ann
  };
  Plotly.react(gd2, data, layout, { displayModeBar: false, scrollZoom: false, doubleClick: false, responsive: true });
}

// ===================== tabs =====================
const TABS = ['latest', 'day', 'trends'];
let curTab = 'latest', trendsDrawn = false, exploreDate = DAY_KEYS[DAY_KEYS.length - 1];
function renderLatest(){ if (curTab === 'latest') dayPanel($('latest'), DAY_KEYS[DAY_KEYS.length - 1], 'day24-latest', true); else if (curTab === 'day') renderExplore(); }
function renderExplore(){
  const i = DAY_KEYS.indexOf(exploreDate);
  $('dpick').value = exploreDate; $('dprev').disabled = i <= 0; $('dnext').disabled = i >= DAY_KEYS.length - 1;
  dayPanel($('dayx'), exploreDate, 'day24-explore', false);
}
function setExplore(iso){
  // snap to the nearest date that has a post
  if (!DAYS[iso]) { const later = DAY_KEYS.find(d => d >= iso); iso = later || DAY_KEYS[DAY_KEYS.length - 1]; }
  exploreDate = iso; renderExplore(); syncHash();
}
function syncHash(){ const h = curTab === 'day' ? '#day=' + exploreDate : '#' + curTab; if (location.hash !== h) history.replaceState(null, '', h); }
function showTab(t){
  curTab = t;
  TABS.forEach(x => { const on = x === t; $('tab-' + x).hidden = !on; const b = $('tb-' + x); b.setAttribute('aria-selected', String(on)); b.tabIndex = on ? 0 : -1; });
  if (t === 'trends') { if (!trendsDrawn) { trendsDrawn = true; render(true); } else Plotly.Plots.resize(gd); }
  else renderLatest();
  syncHash();
}

function render(reset){
  const fig = build();
  const keep = (!reset && gd._fullLayout && gd._fullLayout.xaxis && gd._fullLayout.xaxis.range) ? gd._fullLayout.xaxis.range.slice() : null;
  fig.layout.xaxis.range = keep || wholeRange(fig.S);
  curTV = fig.tv; curWP = fig.wp; curN = fig.n;
  const st = styleFor(fig.layout.xaxis.range);   // bar widths, line widths and dot sizes follow the zoom level
  fig.data.forEach(t => applyStyle(t, st));
  curStyle = st.key;
  const fit = tempFit(fig.layout.xaxis.range);   // temperature axes follow the dates on screen
  if (fit && fit.w) fig.layout.yaxis3.range = fit.w;
  if (fit && fit.d) fig.layout.yaxis4.range = fit.d;
  const cfg = { responsive: true, displaylogo: false, modeBarButtonsToRemove: ['lasso2d', 'select2d'] };
  if (reset) {
    // a fresh plot per season, so "Reset axes" / double-click return to THIS season's full range
    Plotly.newPlot(gd, fig.data, fig.layout, cfg).then(function(){ gd.on('plotly_relayout', onRelayout); fixArrows(); });
  } else {
    Plotly.react(gd, fig.data, fig.layout, cfg).then(fixArrows);
  }
  syncInputs();
}

let curTV = null, curWP = [], curN = 1, curStyle = '', curSpans = { w: [], d: [] };
// Bars are drawn at least 2 px wide (so a whole season stays visible) and never wider than their slot; lines get thinner
// and dots smaller as more days are shown.
function styleFor(xr){
  const el = gd._fullLayout && gd._fullLayout.xaxis && gd._fullLayout.xaxis._length;
  const w = el || Math.max(300, gd.clientWidth - 140);
  const days = Math.max(1, (axisMs(xr[1]) - axisMs(xr[0])) / DAY), ppd = w / days;   // days on screen, pixels per day
  const st = { detail: days <= 31, lw: days > 31 ? 0.9 : 1.6, msz: ppd < 8 ? 2.5 : (ppd < 24 ? 3.5 : 5), minMs: 1.5 * DAY / ppd };
  st.key = (st.detail ? 'd' : 'c') + st.lw + '|' + st.msz + '|' + Math.round(st.minMs / (0.25 * HOUR));
  return st;
}
function barGeom(m, st){
  const spans = m.day ? curSpans.d : curSpans.w, cap = m.day ? DAY : 5.8 * HOUR;
  const w = spans.map(sp => Math.min(Math.max(sp * 0.92, st.minMs * m.n), cap));
  if (m.n === 1) return { width: w, offset: w.map(x => -x / 2) };
  return { width: w.map(x => x * 0.47), offset: w.map(x => m.idx === 0 ? -x / 2 : x * 0.03) };   // standard and unbiased side by side
}
// start-to-end temperature across each window's bar (heating on), or one centred dot (heating off); positions follow the bar width
function wsegData(on, st){
  const x = [], y = [], cd = [], sym = [], ok = v => v !== null && v !== undefined;
  curWP.forEach(p => {
    if (p.on !== on) return;
    if (on && ok(p.v) && ok(p.ve)) {
      const half = Math.min(Math.max(p.span * 0.92, st.minMs * curN), 5.8 * HOUR) * 0.45;
      x.push(stamp(p.c - half), stamp(p.c + half), null); y.push(p.v, p.ve, null); cd.push(p.cd, p.cd, p.cd); sym.push('circle', 'arrow-wide', 'circle');
    } else {
      x.push(stamp(p.c)); y.push(ok(p.v) && ok(p.ve) ? (p.v + p.ve) / 2 : (ok(p.v) ? p.v : p.ve)); cd.push(p.cd); sym.push('circle');
    }
  });
  return { x: x, y: y, cd: cd, sym: sym };
}
// arrow heads point along each line as it appears on screen, so their angle is worked out from the current pixel scales
function arrowAngles(t){
  const fl = gd._fullLayout, xa = fl && fl.xaxis, ya = fl && fl.yaxis3;
  if (!xa || !ya || !ya.range) return t.x.map(() => 0);
  const kx = xa._length / (axisMs(xa.range[1]) - axisMs(xa.range[0])), ky = ya._length / (ya.range[1] - ya.range[0]);
  return t.x.map((x, i) => {
    if (!i || !t.marker.symbol || t.marker.symbol[i] !== 'arrow-wide') return 0;
    const sx = (axisMs(x) - axisMs(t.x[i - 1])) * kx, sy = -(t.y[i] - t.y[i - 1]) * ky;   // screen vector, y downwards
    return Math.atan2(sx, -sy) * 180 / Math.PI;                                          // clockwise from "up"
  });
}
function fixArrows(){
  if (!gd.data) return;
  gd.data.forEach((t, i) => { if (t.meta && t.meta.kind === 'wseg' && t.meta.on && t.visible !== false) Plotly.restyle(gd, { 'marker.angle': [arrowAngles(t)] }, [i]); });
}
function applyStyle(t, st){
  const m = t.meta || {};
  if (m.kind === 'wseg') {
    const g = wsegData(m.on, st);
    t.x = g.x; t.y = g.y; t.customdata = g.cd; t.visible = st.detail;
    t.marker.size = m.on ? g.sym.map(q => q === 'arrow-wide' ? st.msz + 5 : st.msz + 1) : Math.max(2, st.msz);
    if (m.on) { t.marker.symbol = g.sym; t.marker.angle = g.x.map(() => 0); } if (m.on) t.line.width = st.lw * 1.5;
  }
  else if (!m.day && (m.kind === 'tline' || m.kind === 'tpt') && st.detail) t.visible = false;
  else if (m.kind === 'tline') t.line.width = m.on ? st.lw * 1.5 : st.lw * 0.8;
  else if (m.kind === 'tpt') t.marker.size = m.on ? st.msz + 1.5 : Math.max(1.5, st.msz - 1);
  else if (m.kind === 'bar') { const g = barGeom(m, st); t.width = g.width; t.offset = g.offset; }
}
function restyleAll(){
  const xr = gd._fullLayout && gd._fullLayout.xaxis && gd._fullLayout.xaxis.range;
  if (!xr || !gd.data) return;
  const st = styleFor(xr);
  if (st.key === curStyle) return;
  curStyle = st.key;
  gd.data.forEach((t, i) => {
    const m = t.meta || {};
    if (m.kind === 'wseg') {
      const g = wsegData(m.on, st), u = { x: [g.x], y: [g.y], customdata: [g.cd], visible: st.detail, 'marker.size': m.on ? g.sym.map(q => q === 'arrow-wide' ? st.msz + 5 : st.msz + 1) : Math.max(2, st.msz) };
      if (m.on) { u['marker.symbol'] = [g.sym]; u['marker.angle'] = [g.x.map(() => 0)]; }
      if (m.on) u['line.width'] = st.lw * 1.5;
      Plotly.restyle(gd, u, [i]);
    }
    else if (!m.day && (m.kind === 'tline' || m.kind === 'tpt')) {
      Plotly.restyle(gd, { visible: !st.detail }, [i]);
      if (!st.detail) Plotly.restyle(gd, m.kind === 'tline' ? { 'line.width': m.on ? st.lw * 1.5 : st.lw * 0.8 } : { 'marker.size': m.on ? st.msz + 1.5 : Math.max(1.5, st.msz - 1) }, [i]);
    }
    else if (m.kind === 'tline') Plotly.restyle(gd, { 'line.width': m.on ? st.lw * 1.5 : st.lw * 0.8 }, [i]);
    else if (m.kind === 'tpt') Plotly.restyle(gd, { 'marker.size': m.on ? st.msz + 1.5 : Math.max(1.5, st.msz - 1) }, [i]);
    else if (m.kind === 'bar') { const g = barGeom(m, st); Plotly.restyle(gd, { width: [g.width], offset: [g.offset] }, [i]); }
  });
}
function fitOne(xs, vs, a, b){
  let lo = Infinity, hi = -Infinity;
  for (let i = 0; i < xs.length; i++) {
    const v = vs[i];
    if (v !== null && v !== undefined && xs[i] >= a && xs[i] <= b) { if (v < lo) lo = v; if (v > hi) hi = v; }
  }
  return isFinite(lo) ? [Math.floor(lo) - 1, Math.ceil(hi) + 1] : null;   // coldest to warmest on screen, plus 1 degree each side
}
function tempFit(xr){
  if (!curTV || !DATA.hasTemps || !xr) return null;
  const a = axisMs(xr[0]), b = axisMs(xr[1]);
  const det = (b - a) / DAY <= 31, wx = det ? curTV.wx.concat(curTV.wxe) : curTV.wx, wv = det ? curTV.wv.concat(curTV.wve) : curTV.wv;
  return { w: fitOne(wx, wv, a, b), d: fitOne(curTV.dx, curTV.dv, a, b) };
}
function onRelayout(ev){
  syncInputs();
  if (!ev || !Object.keys(ev).some(k => k.indexOf('xaxis') === 0)) return;   // our own y-axis updates must not loop
  const fit = tempFit(gd._fullLayout.xaxis.range), upd = {};
  if (fit && fit.w) upd['yaxis3.range'] = fit.w;
  if (fit && fit.d) upd['yaxis4.range'] = fit.d;
  const done = Object.keys(upd).length ? Plotly.relayout(gd, upd) : Promise.resolve();
  restyleAll();
  done.then(fixArrows);
}
let rsz = null;
window.addEventListener('resize', () => { clearTimeout(rsz); rsz = setTimeout(restyleAll, 250); });

function syncInputs(){
  const r = gd._fullLayout && gd._fullLayout.xaxis && gd._fullLayout.xaxis.range;
  if (!r) return;
  const a = axisMs(r[0]);
  $('from').value = dstr(a + 12*HOUR);   // ranges start at noon, the edge between two post days
  $('to').value = dstr(axisMs(r[1]) - MIN);
  const S = getSeason(), r0 = r.map(axisMs), same = (x, y) => Math.abs(axisMs(x[0]) - r0[0]) < HOUR && Math.abs(axisMs(x[1]) - r0[1]) < HOUR;
  let v = same(wholeRange(S)) ? '' : 'custom';
  seasonMonths(S).forEach(m => { if (same(monthRange(S, m.key))) v = m.key; });
  $('month').value = v;
}
function fillMonths(){
  const S = getSeason(), ms_ = $('month');
  ms_.innerHTML = '<option value="">Whole season</option>' + seasonMonths(S).map(m => '<option value="' + m.key + '">' + m.full + '</option>').join('')
    + '<option value="custom" hidden>Custom range</option>';
  ms_.value = '';
}

function applyDates(){
  const f = $('from').value, t = $('to').value;
  if (!f || !t) return;
  const a = ms(f), b = ms(t) + DAY;
  if (b > a) Plotly.relayout(gd, { 'xaxis.range': [stamp(a), stamp(b)] });
}

// ---- wire up the controls
const sel = $('season');
DATA.seasons.forEach(s => { const o = document.createElement('option'); o.value = s.id; o.textContent = s.label + ' (' + s.rows.length + ' days)'; sel.appendChild(o); });
sel.value = state.season;
fillMonths();
sel.addEventListener('change', () => { state.season = sel.value; fillMonths(); if (trendsDrawn) render(true); });
$('month').addEventListener('change', () => {
  const v = $('month').value, S = getSeason();
  if (v === 'custom' || !trendsDrawn) return;
  Plotly.relayout(gd, { 'xaxis.range': v ? monthRange(S, v) : wholeRange(S) });
});
function wireSeg(id, key){
  const btns = Array.prototype.slice.call($(id).querySelectorAll('button'));
  btns.forEach(btn => btn.addEventListener('click', () => {
    state[key] = btn.getAttribute('data-v');
    btns.forEach(x => x.setAttribute('aria-pressed', String(x === btn)));
    renderLatest();
    if (trendsDrawn) render(false);
  }));
}
$('unb').checked = false;
$('unb').addEventListener('change', () => { state.unbiased = $('unb').checked; renderLatest(); if (trendsDrawn) render(false); });
wireSeg('units', 'units');
$('from').addEventListener('change', applyDates);
$('to').addEventListener('change', applyDates);

$('foot').textContent = 'Source: Barbican Underfloor Heating blog (Atom feed) · data through ' + DATA.dataThrough + (DATA.hasTemps ? ' · temperatures: Open-Meteo, to ' + DATA.tempThrough : '') +
  ' · page built ' + DATA.built + (DATA.tempCorrected ? ' · temperatures bias-corrected to the City of London Barbican sensors' : '') +
  ' · Oct–Apr only; May–Sep posts are ignored' +
  (DATA.cappedCount ? ' · ' + DATA.cappedCount + ' readings (' + DATA.cappedDays + ' days) above their window length are capped at the window length' : '');
TABS.forEach(t => $('tb-' + t).addEventListener('click', () => showTab(t)));
document.querySelector('.tabs').addEventListener('keydown', e => {   // arrow keys move between tabs
  if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
  const i = TABS.indexOf(curTab), j = (i + (e.key === 'ArrowRight' ? 1 : TABS.length - 1)) % TABS.length;
  showTab(TABS[j]); $('tb-' + TABS[j]).focus(); e.preventDefault();
});
$('dpick').min = DAY_KEYS[0]; $('dpick').max = DAY_KEYS[DAY_KEYS.length - 1];
$('dpick').addEventListener('change', () => { if ($('dpick').value) setExplore($('dpick').value); });
const step = k => { const i = DAY_KEYS.indexOf(exploreDate) + k; if (i >= 0 && i < DAY_KEYS.length) setExplore(DAY_KEYS[i]); };
$('dprev').addEventListener('click', () => step(-1));
$('dnext').addEventListener('click', () => step(1));
document.addEventListener('keydown', e => {
  if (curTab !== 'day' || e.target.closest('input,select,textarea,.tabs')) return;
  if (e.key === 'ArrowLeft') { step(-1); e.preventDefault(); } else if (e.key === 'ArrowRight') { step(1); e.preventDefault(); }
});
// open the tab (and day) named in the address, e.g. #trends or #day=2025-01-12
(function(){ const h = location.hash.slice(1);
  if (h.indexOf('day=') === 0) { exploreDate = DAYS[h.slice(4)] ? h.slice(4) : exploreDate; showTab('day'); }
  else showTab(TABS.indexOf(h) >= 0 ? h : 'latest'); })();
})();
</script>
</body>
</html>
'''


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", default="heating_history.csv")
    ap.add_argument("--temps", default="temperature_history.csv")
    ap.add_argument("--temps-tz", default="UTC", choices=["UTC", "London"], help="what the times in the temperature file are (default UTC)")
    ap.add_argument("--no-temp-correction", action="store_true", help="use raw Open-Meteo temperatures instead of the bias-corrected ones")
    ap.add_argument("--out", default="index.html")
    ap.add_argument("--plotly-js", help="path to a local plotly JS bundle to embed in the page")
    ap.add_argument("--plotly-cdn", action="store_true", help="load Plotly from a CDN instead of plotly-basic.min.js next to the page")
    args = ap.parse_args()

    payload, ignored = build_payload(args.csv, args.temps, args.temps_tz, correct=not args.no_temp_correction)
    if args.plotly_js:
        js = Path(args.plotly_js).read_text(encoding="utf-8").replace("</script", "<\\/script")
        plotly_tag = f"<script>{js}</script>"
    elif args.plotly_cdn:
        plotly_tag = f'<script src="{PLOTLY_CDN}" crossorigin="anonymous"></script>'
    else:
        plotly_tag = f'<script src="{PLOTLY_LOCAL}"></script>'

    data_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE.replace("__PLOTLY_TAG__", plotly_tag).replace("__PAYLOAD__", data_json)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    n_days = sum(len(s["rows"]) for s in payload["seasons"])
    print(f"wrote {out} ({out.stat().st_size / 1024:.0f} KB): {len(payload['seasons'])} seasons, "
          f"{n_days} days kept, {ignored} days outside Oct-Apr ignored")


if __name__ == "__main__":
    main()
