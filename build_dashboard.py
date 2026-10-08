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
    """Hourly temperatures as two parallel lists: London wall-clock datetimes (naive) and deg C.

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
            if tz.upper() == "UTC":
                t = t.replace(tzinfo=timezone.utc).astimezone(LONDON).replace(tzinfo=None)
            rows.append((t, v))
    rows.sort()
    return [t for t, _ in rows], [v for _, v in rows]


def min_temp(times, temps, start, end):
    """Lowest hourly reading with start <= time <= end, or None if there is none."""
    i, j = bisect.bisect_left(times, start), bisect.bisect_right(times, end)
    return round(min(temps[i:j]), 1) if j > i else None


def day_temps(d, wdefs, times, temps):
    """Lowest temperature in each window of post day d, then over the whole 24 h before 08:30."""
    base = datetime.combine(d, datetime.min.time())
    out = []
    for w in wdefs:
        s = base + timedelta(days=w["startOff"], hours=int(w["start"][:2]), minutes=int(w["start"][3:]))
        e = base + timedelta(days=w["endOff"], hours=int(w["end"][:2]), minutes=int(w["end"][3:]))
        out.append(min_temp(times, temps, s, e))
    end = base + POST_TIME
    out.append(min_temp(times, temps, end - timedelta(days=1), end))
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
    seasons, ignored = {}, 0
    for d in sorted(days):
        y = season_start_year(d)
        if y is None:
            ignored += 1
            continue
        row = [d.isoformat()]
        for p in PROFILES:
            row += [num(days[d][p][k]) for k in window_keys]  # raises KeyError if a window is missing
        row += day_temps(d, wdefs, times, temps)  # lowest temperature per window, then over the whole 24 h
        seasons.setdefault(y, []).append(row)
    payload = {
        "windows": wdefs,
        "profiles": PROFILES,
        "hasTemps": bool(times),
        "tempThrough": times[-1].strftime("%Y-%m-%d") if times else None,
        "seasons": [
            {"id": str(y), "label": f"{y}–{str(y + 1)[2:]}", "rows": seasons[y]}
            for y in sorted(seasons, reverse=True)  # newest season first
        ],
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
  --bg:#ffffff; --fg:#1d2433; --muted:#5d667a; --line:#d9dde6; --panel:#f4f5f8;
  --accent:#2f66d0; --on-accent:#ffffff; --u:#3a6fd8; --a:#e8741a; --t:#8fb0d9;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#14171f; --fg:#e8eaf0; --muted:#9aa3b5; --line:#2c3243; --panel:#1c212c;
    --accent:#6c9aff; --on-accent:#0d1220; --u:#6c9aff; --a:#ff9a4d; --t:#4d6a94;
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1280px;margin:0 auto;padding:16px 16px 12px}
h1{font-size:1.35rem;margin:0 0 2px}
.sub{color:var(--muted);margin:0 0 14px;font-size:.88rem}
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
.rangeslider-mask-min,.rangeslider-mask-max{fill:var(--muted)!important;fill-opacity:.28!important}
footer{color:var(--muted);font-size:.78rem;margin-top:6px}
noscript{display:block;padding:12px;color:var(--muted)}
</style>
</head>
<body>
<main>
  <h1>Barbican underfloor heating</h1>
  <p class="sub" id="sub"></p>
  <section class="latest" id="latest" aria-labelledby="latest-h"></section>
  <div class="controls">
    <div class="ctl"><label for="season">Heating season</label><select id="season"></select></div>
    <div class="ctl"><span class="lab">Profile</span>
      <div class="seg" id="profile" role="group" aria-label="Profile">
        <button type="button" data-v="Unbiased" aria-pressed="false">Unbiased</button>
        <button type="button" data-v="Adjusted" aria-pressed="false">Adjusted</button>
        <button type="button" data-v="both" aria-pressed="true">Both</button>
      </div></div>
    <div class="ctl"><span class="lab">Units</span>
      <div class="seg" id="units" role="group" aria-label="Units">
        <button type="button" data-v="min" aria-pressed="true">Minutes</button>
        <button type="button" data-v="pct" aria-pressed="false">% of window</button>
      </div></div>
    <div class="ctl"><label for="from">From</label><input type="date" id="from"></div>
    <div class="ctl"><label for="to">To</label><input type="date" id="to"></div>
  </div>
  <div id="chart"></div>
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
const state = { season: DATA.seasons[0].id, profile: 'both', units: 'min' };
const getSeason = () => DATA.seasons.filter(s => s.id === state.season)[0];
const bounds = S => ({ first: ms(S.rows[0][0]), last: ms(S.rows[S.rows.length - 1][0]) });
const wholeRange = S => { const b = bounds(S); return [stamp(b.first - 12*HOUR), stamp(b.last + 12*HOUR)]; };

function build(){
  const S = getSeason(), pct = state.units === 'pct';
  const showU = state.profile !== 'Adjusted', showA = state.profile !== 'Unbiased', both = showU && showA;
  const cu = css('--u'), ca = css('--a'), ct = css('--t');
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

  const hasT = DATA.hasTemps, nW = W.length, iT = 1 + 2 * nW;   // row = [date, U x nW, A x nW, tempMin x nW, tempMin of the whole 24 h]

  // ---- windows: lowest temperature as a bar spanning the window (behind); heating minutes as dots joined by lines
  const bx = [], bw = [], bt = [], bcd = [];
  const lx = [], lu = [], la = [], lcU = [], lcA = [];
  let prev = null;
  S.rows.forEach(r => {
    const d0 = ms(r[0]);
    if (prev !== null && d0 - prev > DAY) { lx.push(stamp((prev + d0) / 2)); lu.push(null); la.push(null); lcU.push([]); lcA.push([]); }  // missing days = a break
    prev = d0;
    W.forEach((w, k) => {
      const s = d0 + w.startOff * DAY + hmMs(w.start), e = d0 + w.endOff * DAY + hmMs(w.end);
      const sameDay = dstr(s) === dstr(e);
      const when = niceT(s) + ' → ' + (sameDay ? hhmm(e) : niceT(e));
      const mid = stamp((s + e) / 2);
      bx.push(mid); bw.push((e - s) * 0.96); bt.push(r[iT + k]); bcd.push([w.label, when]);
      const u = r[1 + k], a = r[1 + nW + k];
      lx.push(mid); lu.push(val(u, w.len)); la.push(val(a, w.len));
      const note = m => m > w.len ? ' ⚠ longer than the window' : '';
      lcU.push([w.label, when, w.len, Math.round(100 * u / w.len), note(u), u]);
      lcA.push([w.label, when, w.len, Math.round(100 * a / w.len), note(a), a]);
    });
  });

  // ---- daily totals: one point per calendar day (at the 08:30 post), empty days left as gaps
  const bd = bounds(S), byDate = {};
  S.rows.forEach(r => { byDate[ms(r[0])] = r; });
  const tx = [], tu = [], ta = [], tcU = [], tcA = [], dbx = [], dbt = [], dbc = [];
  const cdTot = (t, tot, ws) => [niceD(t), (tot / 60).toFixed(1), tot, Math.round(100 * tot / SUM_LEN)]
    .concat(ws, ws.map((m, k) => Math.round(100 * m / W[k].len)));
  for (let t = bd.first; t <= bd.last; t += DAY) {
    tx.push(stamp(t + 8*HOUR + 30*MIN));
    const r = byDate[t];
    if (r) {
      const uw = r.slice(1, 1 + nW), aw = r.slice(1 + nW, 1 + 2*nW);
      const ut = sum(uw), at = sum(aw);
      tu.push(val(ut, SUM_LEN)); ta.push(val(at, SUM_LEN));
      tcU.push(cdTot(t, ut, uw)); tcA.push(cdTot(t, at, aw));
      dbx.push(stamp(t - 3*HOUR - 30*MIN)); dbt.push(r[iT + nW]); dbc.push([niceD(t)]);   // the 24 h before 08:30 are centred on 20:30 the evening before
    } else { tu.push(null); ta.push(null); tcU.push([]); tcA.push([]); }
  }
  const tRange = vals => {   // temperature axis: zero (or lower) at the bottom, headroom above so the bars stay in the background
    const v = vals.filter(x => x !== null && x !== undefined);
    if (!v.length) return undefined;
    const lo = Math.min(0, Math.floor(Math.min.apply(null, v)) - 1), mx = Math.max.apply(null, v);
    return [lo, Math.ceil(mx + (mx - lo) * 0.7)];
  };
  const trW = tRange(bt), trD = tRange(dbt);
  const HT_TW = '<b>%{customdata[0]} window</b><br>%{customdata[1]}<br>Lowest temperature <b>%{y:.1f} °C</b><extra></extra>';
  const HT_TD = '<b>%{customdata[0]}</b><br>Lowest temperature in the 24 h before 08:30: <b>%{y:.1f} °C</b><extra></extra>';

  const data = [];
  if (hasT) {
    data.push({ type: 'bar', name: 'Lowest temperature', legendgroup: 'T', x: bx, y: bt, width: bw, customdata: bcd,
      marker: { color: ct, opacity: 0.55 }, hovertemplate: HT_TW, xaxis: 'x', yaxis: 'y' });
    data.push({ type: 'bar', name: 'Lowest temperature', legendgroup: 'T', showlegend: false, x: dbx, y: dbt, width: dbx.map(() => DAY * 0.92), customdata: dbc,
      marker: { color: ct, opacity: 0.55 }, hovertemplate: HT_TD, xaxis: 'x2', yaxis: 'y2' });
  }
  if (showU) data.push({ type: 'scatter', mode: 'lines+markers', name: 'Unbiased', legendgroup: 'U', x: lx, y: lu, customdata: lcU, connectgaps: false,
      line: { color: cu, width: 1.6 }, marker: { size: 6, color: cu }, hovertemplate: HT_WIN, xaxis: 'x', yaxis: 'y3' });
  if (showA) data.push({ type: 'scatter', mode: 'lines+markers', name: 'Adjusted', legendgroup: 'A', x: lx, y: la, customdata: lcA, connectgaps: false,
      line: { color: ca, width: 1.6 }, marker: { size: 6, color: ca }, hovertemplate: HT_WIN, xaxis: 'x', yaxis: 'y3' });
  if (showU) data.push({ type: 'scatter', mode: 'lines+markers', name: 'Unbiased', legendgroup: 'U', showlegend: false,
      x: tx, y: tu, customdata: tcU, connectgaps: false, line: { color: cu, width: 2.2 }, marker: { size: 5, color: cu },
      hovertemplate: HT_TOT, xaxis: 'x2', yaxis: 'y4' });
  if (showA) data.push({ type: 'scatter', mode: 'lines+markers', name: 'Adjusted', legendgroup: 'A', showlegend: false,
      x: tx, y: ta, customdata: tcA, connectgaps: false, line: { color: ca, width: 1.8 }, marker: { size: 5, color: ca },
      hovertemplate: HT_TOT, xaxis: 'x2', yaxis: 'y4' });

  const fg = css('--fg'), grid = css('--line'), panel = css('--panel');
  const D1 = [0.40, 1], D2 = [0, 0.31];
  // temperature axes (right) sit underneath; the heating axes (left) overlay them so the dots draw over the bars
  const tAxis = (dom, anchor, rng, title) => ({ domain: dom, anchor: anchor, side: 'right', range: rng, visible: hasT, showgrid: false,
    zeroline: !!(rng && rng[0] < 0), zerolinecolor: grid, fixedrange: true, ticksuffix: ' °C', title: { text: title } });
  const layout = {
    uirevision: state.season + '|' + state.profile + '|' + state.units,
    barmode: 'overlay', bargap: 0, hovermode: 'closest', dragmode: 'zoom',
    margin: { l: 66, r: hasT ? 74 : 16, t: 36, b: 10 },
    paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
    font: { color: fg, size: 12, family: 'system-ui,-apple-system,"Segoe UI",Roboto,sans-serif' },
    hoverlabel: { bgcolor: panel, bordercolor: grid, font: { color: fg } },
    legend: { orientation: 'h', x: 0, y: 1, yanchor: 'bottom' },
    xaxis: { type: 'date', anchor: 'y', gridcolor: grid, linecolor: grid, showticklabels: false },
    xaxis2: { type: 'date', anchor: 'y2', matches: 'x', gridcolor: grid, linecolor: grid,
      rangeslider: { visible: true, thickness: 0.09, bgcolor: panel, bordercolor: grid, borderwidth: 1 } },
    yaxis: tAxis(D1, 'x', trW, 'Lowest temperature in window'),
    yaxis3: { domain: D1, anchor: 'x', overlaying: 'y', side: 'left', rangemode: 'tozero', gridcolor: grid, zeroline: false, fixedrange: true,
      title: { text: pct ? 'Heating as % of window length' : 'Minutes heating in window' }, ticksuffix: pct ? '%' : '' },
    yaxis2: tAxis(D2, 'x2', trD, 'Lowest temp, 24 h'),
    yaxis4: { domain: D2, anchor: 'x2', overlaying: 'y2', side: 'left', rangemode: 'tozero', gridcolor: grid, zeroline: false, fixedrange: true,
      title: { text: pct ? 'Daily total (% of ' + SUM_LEN + ' min)' : 'Daily total (min)' }, ticksuffix: pct ? '%' : '' }
  };
  return { data: data, layout: layout, S: S };
}

function renderLatest(){
  const L = DATA.latest, el = $('latest');
  const dayT = ms(L.date), tot = L.u;
  const mins = m => m + ' min', hrs = m => (m / 60).toFixed(1) + ' h', pc = (m, n) => Math.round(100 * m / n) + '%';
  const sgn = d => d === 0 ? 'same as' : (d > 0 ? '▲ ' + d + ' min more than' : '▼ ' + (-d) + ' min fewer than');
  const deg = t => t === null || t === undefined ? '–' : t.toFixed(1) + ' °C';
  const best = L.windows.slice().sort((a, b) => b.u - a.u)[0];
  const age = Math.round((axisMs(DATA.built.slice(0, 10)) - dayT) / DAY);
  const parts = [];
  parts.push('The heating ran for <b>' + mins(L.u) + ' (' + hrs(L.u) + ')</b> on the Unbiased profile and <b>' + mins(L.a) + ' (' + hrs(L.a) + ')</b> on the Adjusted profile, '
    + pc(L.u, SUM_LEN) + ' and ' + pc(L.a, SUM_LEN) + ' of the ' + SUM_LEN + ' minutes available.');
  parts.push(best.u > 0 ? 'Most of it came in the ' + best.label + ' window (' + mins(best.u) + ' Unbiased).' : 'It did not run in any window.');
  if (L.t !== null && L.t !== undefined) parts.push('The lowest temperature was <b>' + deg(L.t) + '</b>' + (L.prev && L.prev.t !== null ? ' (' + deg(L.prev.t) + ' the day before)' : '') + '.');
  if (L.avg) parts.push('The previous ' + L.avg.n + ' days averaged ' + mins(L.avg.u) + ' (Unbiased) and ' + mins(L.avg.a) + ' (Adjusted) a day.');
  const tile = (k, v, d) => '<div class="tile"><div class="k">' + k + '</div><div class="v">' + v + '</div><div class="d">' + d + '</div></div>';
  const tiles = [
    tile('Unbiased, total', mins(L.u), hrs(L.u) + ' · ' + pc(L.u, SUM_LEN) + ' of all windows' + (L.prev ? '<br>' + sgn(L.u - L.prev.u) + ' the day before' : '')),
    tile('Adjusted, total', mins(L.a), hrs(L.a) + ' · ' + pc(L.a, SUM_LEN) + ' of all windows' + (L.prev ? '<br>' + sgn(L.a - L.prev.a) + ' the day before' : ''))
  ];
  if (DATA.hasTemps) tiles.push(tile('Lowest temperature', deg(L.t), L.prev ? 'day before: ' + deg(L.prev.t) : 'in the 24 h to 08:30'));
  if (L.avg) tiles.push(tile('Previous ' + L.avg.n + '-day average', L.avg.u + ' / ' + L.avg.a + ' min', 'Unbiased / Adjusted, per day'));
  const rows = L.windows.map(w => '<tr><td>' + w.label + '</td><td>' + mins(w.u) + ' (' + pc(w.u, w.len) + ')</td><td>' + mins(w.a) + ' (' + pc(w.a, w.len) + ')</td>'
    + (DATA.hasTemps ? '<td>' + deg(w.t) + '</td>' : '') + '</tr>').join('');
  el.innerHTML = '<h2 id="latest-h">Latest 24 hours · ' + niceD(dayT) + '</h2>'
    + '<p class="when">' + niceT(dayT - DAY + 8*HOUR + 30*MIN) + ' → ' + niceT(dayT + 8*HOUR + 30*MIN)
    + (age > 2 ? ' · <span class="warn">this post is ' + age + ' days old</span>' : '')
    + (L.inSeason ? '' : ' · <span class="warn">outside the Oct–Apr season, so it is not in the charts below</span>') + '</p>'
    + '<p>' + parts.join(' ') + '</p><div class="tiles">' + tiles.join('') + '</div>'
    + '<table><thead><tr><th>Window</th><th>Unbiased</th><th>Adjusted</th>' + (DATA.hasTemps ? '<th>Lowest temp</th>' : '') + '</tr></thead><tbody>' + rows + '</tbody></table>';
}

function render(reset){
  const fig = build();
  const keep = (!reset && gd._fullLayout && gd._fullLayout.xaxis && gd._fullLayout.xaxis.range) ? gd._fullLayout.xaxis.range.slice() : null;
  fig.layout.xaxis.range = keep || wholeRange(fig.S);
  const cfg = { responsive: true, displaylogo: false, modeBarButtonsToRemove: ['lasso2d', 'select2d'] };
  if (reset) {
    // a fresh plot per season, so "Reset axes" / double-click return to THIS season's full range
    Plotly.newPlot(gd, fig.data, fig.layout, cfg).then(function(){ gd.on('plotly_relayout', syncInputs); });
  } else {
    Plotly.react(gd, fig.data, fig.layout, cfg);
  }
  const b = bounds(fig.S);
  $('sub').textContent = fig.S.label + ' heating season · ' + fig.S.rows.length + ' days of data (' + niceD(b.first) + ' to ' + niceD(b.last) +
    ') · windows: ' + W.map(w => w.label).join(', ') + ' · each day covers the 24 h before 08:30' +
    (DATA.hasTemps ? ' · grey-blue bars: lowest hourly temperature (°C, right axis) in each window / in the 24 h' : '') +
    (state.units === 'pct' ? ' · 100% = the full window (' + W.map(w => w.label + ' = ' + w.len + ' min').join(', ') + '); daily total 100% = ' + SUM_LEN + ' min' : '');
  syncInputs();
}

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
    render(false);
  }));
}
wireSeg('profile', 'profile');
wireSeg('units', 'units');
$('from').addEventListener('change', applyDates);
$('to').addEventListener('change', applyDates);
if (window.matchMedia) window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => render(false));

$('foot').textContent = 'Source: Barbican Underfloor Heating blog (Atom feed) · data through ' + DATA.dataThrough + (DATA.hasTemps ? ' · temperatures: Open-Meteo, to ' + DATA.tempThrough : '') +
  ' · page built ' + DATA.built +
  ' · Oct–Apr only; May–Sep posts are ignored';
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
