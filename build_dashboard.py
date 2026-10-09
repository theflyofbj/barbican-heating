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
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

PLOTLY_CDN = "https://cdn.jsdelivr.net/npm/plotly.js-basic-dist-min@2.35.2/plotly-basic.min.js"
PLOTLY_LOCAL = "plotly-basic.min.js"
PROFILES = ["Unbiased", "Adjusted"]
LONDON = ZoneInfo("Europe/London")
POST_TIME = timedelta(hours=8, minutes=30)
SEASON_FIRST_MONTH, SEASON_LAST_MONTH = 10, 4  # October .. April


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


def build_payload(csv_path, temp_path=None, temp_tz="UTC"):
    days, window_keys = load(csv_path)
    times, temps = load_temps(temp_path, temp_tz)
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
.latest h2{font-size:1.02rem;margin:0 0 2px}
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
  <section class="latest" id="latest" aria-labelledby="latest-h"></section>
  <div class="controls">
    <div class="ctl"><label for="season">Heating season</label><select id="season"></select></div>
    <div class="ctl"><span class="lab">Units</span>
      <div class="seg" id="units" role="group" aria-label="Units">
        <button type="button" data-v="min" aria-pressed="true">Minutes</button>
        <button type="button" data-v="pct" aria-pressed="false">% of window</button>
      </div></div>
    <div class="ctl"><label for="from">From</label><input type="date" id="from"></div>
    <div class="ctl"><label for="to">To</label><input type="date" id="to"></div>
  </div>
  <div id="chart"></div>
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
const state = { season: DATA.seasons[0].id, unbiased: false, units: 'min' };
const getSeason = () => DATA.seasons.filter(s => s.id === state.season)[0];
const bounds = S => ({ first: ms(S.rows[0][0]), last: ms(S.rows[S.rows.length - 1][0]) });
const wholeRange = S => { const b = bounds(S); return [stamp(b.first - 12*HOUR), stamp(b.last + 12*HOUR)]; };

function build(){
  const S = getSeason(), pct = state.units === 'pct';
  const showU = state.unbiased, showA = true, both = showU;     // the standard figures are always shown; unbiased only when ticked
  const cu = css('--u'), ca = css('--a'), con = css('--on'), coff = css('--off'), coffl = css('--offline');
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

  const hasT = DATA.hasTemps, nW = W.length, iT = 1 + 2 * nW;   // row = [date, U x nW, A x nW, temp at window start x nW, mean temp over the 24 h]
  // "heating on" always refers to the standard figures, so the temperature line does not change when unbiased data is added
  const onFn = (u, a) => a > 0;
  const stateTxt = (u, a) => (a > 0 ? 'Heating on: ' + a + ' min' : 'Heating off') + (showU ? ' · unbiased ' + u + ' min' : '');

  // ---- windows: heating minutes as bars (zero = no bar); temperature at the window start as a line, grey/red by heating off/on
  const wb = { c: [], span: [], u: [], a: [], cdU: [], cdA: [] }, wp = [];
  S.rows.forEach(r => {
    const d0 = ms(r[0]);
    W.forEach((w, k) => {
      const s = d0 + w.startOff * DAY + hmMs(w.start), e = d0 + w.endOff * DAY + hmMs(w.end);
      const sameDay = dstr(s) === dstr(e);
      const when = niceT(s) + ' → ' + (sameDay ? hhmm(e) : niceT(e));
      const u = r[1 + k], a = r[1 + nW + k];
      wb.c.push(stamp((s + e) / 2)); wb.span.push(e - s);
      wb.u.push(u > 0 ? val(u, w.len) : null); wb.a.push(a > 0 ? val(a, w.len) : null);
      wb.cdU.push([w.label, when, w.len, Math.round(100 * u / w.len), '', u]);
      wb.cdA.push([w.label, when, w.len, Math.round(100 * a / w.len), '', a]);
      const tv = r[iT + k];
      if (tv !== null && tv !== undefined) wp.push({ t: s, v: tv, on: onFn(u, a), cd: [w.label, when, stateTxt(u, a)] });
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
  const trW = tRange(wp), trD = tRange(dp);
  const HT_TW = '<b>%{customdata[0]} window</b><br>%{customdata[1]}<br>Temperature at the start of the window: <b>%{y:.1f} °C</b><br>%{customdata[2]}<extra></extra>';
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
    x: sg.x, y: sg.y, line: { color: col, width: 1.4 }, hoverinfo: 'skip', connectgaps: false, xaxis: xa, yaxis: ya, meta: { kind: 'tline', on: on } });
  const ptTr = (grp, col, pts, ht, xa, ya, on) => ({ type: 'scatter', mode: 'markers', name: grp, legendgroup: grp, showlegend: false,
    x: pts.map(p => stamp(p.t)), y: pts.map(p => p.v), customdata: pts.map(p => p.cd),
    marker: { color: col, size: 4, line: { width: 0 } }, hovertemplate: ht, xaxis: xa, yaxis: ya, meta: { kind: 'tpt', on: on } });
  const barTr = (name, grp, col, x, y, cd, ht, xa, ya, day, idx, n, show) => ({ type: 'bar', name: name, legendgroup: grp, showlegend: show,
    x: x, y: y, customdata: cd, marker: { color: col, opacity: 0.9, line: { width: 0 } }, hovertemplate: ht, xaxis: xa, yaxis: ya,
    meta: { kind: 'bar', day: day, idx: idx, n: n } });

  const nSer = both ? 2 : 1, data = [];
  data.push(barTr('Heating', 'A', ca, wb.c, wb.a, wb.cdA, HT_WIN, 'x', 'y', false, 0, nSer, true));
  if (showU) data.push(barTr('Unbiased', 'U', cu, wb.c, wb.u, wb.cdU, HT_WIN, 'x', 'y', false, 1, nSer, true));
  data.push(barTr('Heating', 'A', ca, db.c, db.a, db.cdA, HT_TOT, 'x2', 'y2', true, 0, nSer, false));
  if (showU) data.push(barTr('Unbiased', 'U', cu, db.c, db.u, db.cdU, HT_TOT, 'x2', 'y2', true, 1, nSer, false));
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
    yaxis4: tAxis(D2, 'x2', 'y2', trD, 'Average temp, 24 h')
  };
  return { data: data, layout: layout, S: S, tv: { wx: wp.map(p => p.t), wv: wp.map(p => p.v), dx: dp.map(p => p.t), dv: dp.map(p => p.v) } };
}

function renderLatest(){
  // The summary describes what the charts below show: the standard heating figures, plus the unbiased ones when that box is ticked.
  const L = DATA.latest, el = $('latest'), pct = state.units === 'pct', inc = state.unbiased;
  const dayT = ms(L.date);
  const mins = m => m + ' min', hrs = m => (m / 60).toFixed(1) + ' h', pc = (m, n) => Math.round(100 * m / n) + '%';
  const pcd = (m, n) => { const v = 100 * m / n; return (v < 10 ? v.toFixed(1) : Math.round(v)) + '%'; };
  const big = m => pct ? pcd(m, SUM_LEN) : mins(m);
  const amt = (m, n) => pct ? pc(m, n) + ' (' + mins(m) + ')' : mins(m) + ' (' + pc(m, n) + ')';     // lead with the unit chosen in the menu
  const sgn = d => d === 0 ? 'same as' : (pct ? (d > 0 ? '▲ ' + Math.round(100 * d / SUM_LEN) + ' percentage points more than' : '▼ ' + Math.round(-100 * d / SUM_LEN) + ' percentage points fewer than')
    : (d > 0 ? '▲ ' + d + ' min more than' : '▼ ' + (-d) + ' min fewer than'));
  const deg = t => t === null || t === undefined ? '–' : t.toFixed(1) + ' °C';
  const age = Math.round((axisMs(DATA.built.slice(0, 10)) - dayT) / DAY);
  const parts = [];
  parts.push(pct
    ? 'The heating ran for <b>' + pc(L.a, SUM_LEN) + '</b> of the ' + SUM_LEN + ' minutes available (' + mins(L.a) + ', ' + hrs(L.a) + (inc ? '; unbiased: ' + pc(L.u, SUM_LEN) + ', ' + mins(L.u) : '') + ').'
    : 'The heating ran for <b>' + mins(L.a) + ' (' + hrs(L.a) + ')</b>, ' + pc(L.a, SUM_LEN) + ' of the ' + SUM_LEN + ' minutes available' + (inc ? ' (unbiased: ' + mins(L.u) + ')' : '') + '.');
  const best = L.windows.slice().sort((a, b) => b.a - a.a)[0];
  parts.push(best.a > 0
    ? 'Most of it came in the ' + best.label + ' window: ' + (pct ? pc(best.a, best.len) + ' of the window, ' + mins(best.a) : mins(best.a) + ', ' + pc(best.a, best.len) + ' of the window') + (inc ? ' (unbiased: ' + mins(best.u) + ')' : '') + '.'
    : 'It did not run in any window.');
  if (L.t !== null && L.t !== undefined) parts.push('The average temperature over the period was <b>' + deg(L.t) + '</b>' + (L.prev && L.prev.t !== null ? ' (' + deg(L.prev.t) + ' the day before)' : '') + '.');
  if (L.avg) parts.push('The previous ' + L.avg.n + ' days averaged ' + big(L.avg.a) + ' a day' + (inc ? ' (unbiased: ' + big(L.avg.u) + ')' : '') + '.');
  const tile = (k, v, d) => '<div class="tile"><div class="k">' + k + '</div><div class="v">' + v + '</div><div class="d">' + d + '</div></div>';
  const total = (name, k) => tile(name, big(L[k]),
    (pct ? mins(L[k]) + ' · ' + hrs(L[k]) : hrs(L[k]) + ' · ' + pc(L[k], SUM_LEN) + ' of all windows') + (L.prev ? '<br>' + sgn(L[k] - L.prev[k]) + ' the day before' : ''));
  const tiles = [total('Heating, total', 'a')];
  if (inc) tiles.push(total('Unbiased, total', 'u'));
  if (DATA.hasTemps) tiles.push(tile('Average temperature', deg(L.t), L.prev ? 'day before: ' + deg(L.prev.t) : 'over the 24 h to 08:30'));
  if (L.avg) tiles.push(tile('Previous ' + L.avg.n + '-day average', (inc ? [L.avg.a, L.avg.u] : [L.avg.a]).map(v => big(v).replace(' min', '')).join(' / ') + (pct ? '' : ' min'),
    (inc ? 'Heating / Unbiased, per day' : 'per day')));
  const rows = L.windows.map(w => '<tr><td>' + w.label + '</td><td>' + amt(w.a, w.len) + '</td>' + (inc ? '<td>' + amt(w.u, w.len) + '</td>' : '')
    + (DATA.hasTemps ? '<td>' + deg(w.t) + '</td>' : '') + '</tr>').join('');
  el.innerHTML = '<h2 id="latest-h">Latest 24 hours · ' + niceD(dayT) + '</h2>'
    + '<p class="when">' + niceT(dayT - DAY + 8*HOUR + 30*MIN) + ' → ' + niceT(dayT + 8*HOUR + 30*MIN) + ' (most recent blog post)'
    + (age > 2 ? ' · <span class="warn">this post is ' + age + ' days old</span>' : '')
    + (L.inSeason ? '' : ' · <span class="warn">outside the Oct–Apr season, so it is not in the charts below</span>') + '</p>'
    + '<p>' + parts.join(' ') + '</p><div class="tiles">' + tiles.join('') + '</div>'
    + '<table><thead><tr><th>Window</th><th>Heating</th>' + (inc ? '<th>Unbiased</th>' : '') + (DATA.hasTemps ? '<th>Temp at start</th>' : '') + '</tr></thead><tbody>' + rows + '</tbody></table>';
}

function render(reset){
  const fig = build();
  const keep = (!reset && gd._fullLayout && gd._fullLayout.xaxis && gd._fullLayout.xaxis.range) ? gd._fullLayout.xaxis.range.slice() : null;
  fig.layout.xaxis.range = keep || wholeRange(fig.S);
  curTV = fig.tv;
  const st = styleFor(fig.layout.xaxis.range);   // bar widths, line widths and dot sizes follow the zoom level
  fig.data.forEach(t => applyStyle(t, st));
  curStyle = st.key;
  const fit = tempFit(fig.layout.xaxis.range);   // temperature axes follow the dates on screen
  if (fit && fit.w) fig.layout.yaxis3.range = fit.w;
  if (fit && fit.d) fig.layout.yaxis4.range = fit.d;
  const cfg = { responsive: true, displaylogo: false, modeBarButtonsToRemove: ['lasso2d', 'select2d'] };
  if (reset) {
    // a fresh plot per season, so "Reset axes" / double-click return to THIS season's full range
    Plotly.newPlot(gd, fig.data, fig.layout, cfg).then(function(){ gd.on('plotly_relayout', onRelayout); });
  } else {
    Plotly.react(gd, fig.data, fig.layout, cfg);
  }
  syncInputs();
}

let curTV = null, curStyle = '', curSpans = { w: [], d: [] };
// Bars are drawn at least 2 px wide (so a whole season stays visible) and never wider than their slot; lines get thinner
// and dots smaller as more days are shown.
function styleFor(xr){
  const el = gd._fullLayout && gd._fullLayout.xaxis && gd._fullLayout.xaxis._length;
  const w = el || Math.max(300, gd.clientWidth - 140);
  const days = Math.max(1, (axisMs(xr[1]) - axisMs(xr[0])) / DAY), ppd = w / days;   // days on screen, pixels per day
  const st = { lw: days > 31 ? 0.9 : 1.6, msz: ppd < 8 ? 2.5 : (ppd < 24 ? 3.5 : 5), minMs: 1.5 * DAY / ppd };
  st.key = st.lw + '|' + st.msz + '|' + Math.round(st.minMs / (0.25 * HOUR));
  return st;
}
function barGeom(m, st){
  const spans = m.day ? curSpans.d : curSpans.w, cap = m.day ? DAY : 5.8 * HOUR;
  const w = spans.map(sp => Math.min(Math.max(sp * 0.92, st.minMs * m.n), cap));
  if (m.n === 1) return { width: w, offset: w.map(x => -x / 2) };
  return { width: w.map(x => x * 0.47), offset: w.map(x => m.idx === 0 ? -x / 2 : x * 0.03) };   // standard and unbiased side by side
}
function applyStyle(t, st){
  const m = t.meta || {};
  if (m.kind === 'tline') t.line.width = m.on ? st.lw * 1.5 : st.lw * 0.8;
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
    if (m.kind === 'tline') Plotly.restyle(gd, { 'line.width': m.on ? st.lw * 1.5 : st.lw * 0.8 }, [i]);
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
  return { w: fitOne(curTV.wx, curTV.wv, a, b), d: fitOne(curTV.dx, curTV.dv, a, b) };
}
function onRelayout(ev){
  syncInputs();
  if (!ev || !Object.keys(ev).some(k => k.indexOf('xaxis') === 0)) return;   // our own y-axis updates must not loop
  const fit = tempFit(gd._fullLayout.xaxis.range), upd = {};
  if (fit && fit.w) upd['yaxis3.range'] = fit.w;
  if (fit && fit.d) upd['yaxis4.range'] = fit.d;
  if (Object.keys(upd).length) Plotly.relayout(gd, upd);
  restyleAll();
}
let rsz = null;
window.addEventListener('resize', () => { clearTimeout(rsz); rsz = setTimeout(restyleAll, 250); });

function syncInputs(){
  const r = gd._fullLayout && gd._fullLayout.xaxis && gd._fullLayout.xaxis.range;
  if (!r) return;
  let a = axisMs(r[0]);
  const first = bounds(getSeason()).first;
  if (a < first && a >= first - 12*HOUR) a = first;  // the 12 h of padding before day one is not a separate date
  $('from').value = dstr(a);
  $('to').value = dstr(axisMs(r[1]) - MIN);
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
sel.addEventListener('change', () => { state.season = sel.value; render(true); });
function wireSeg(id, key){
  const btns = Array.prototype.slice.call($(id).querySelectorAll('button'));
  btns.forEach(btn => btn.addEventListener('click', () => {
    state[key] = btn.getAttribute('data-v');
    btns.forEach(x => x.setAttribute('aria-pressed', String(x === btn)));
    renderLatest();
    render(false);
  }));
}
$('unb').checked = false;
$('unb').addEventListener('change', () => { state.unbiased = $('unb').checked; renderLatest(); render(false); });
wireSeg('units', 'units');
$('from').addEventListener('change', applyDates);
$('to').addEventListener('change', applyDates);

$('foot').textContent = 'Source: Barbican Underfloor Heating blog (Atom feed) · data through ' + DATA.dataThrough + (DATA.hasTemps ? ' · temperatures: Open-Meteo, to ' + DATA.tempThrough : '') +
  ' · page built ' + DATA.built +
  ' · Oct–Apr only; May–Sep posts are ignored' +
  (DATA.cappedCount ? ' · ' + DATA.cappedCount + ' readings (' + DATA.cappedDays + ' days) above their window length are capped at the window length' : '');
renderLatest();
render(true);
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
    ap.add_argument("--out", default="index.html")
    ap.add_argument("--plotly-js", help="path to a local plotly JS bundle to embed in the page")
    ap.add_argument("--plotly-cdn", action="store_true", help="load Plotly from a CDN instead of plotly-basic.min.js next to the page")
    args = ap.parse_args()

    payload, ignored = build_payload(args.csv, args.temps, args.temps_tz)
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
