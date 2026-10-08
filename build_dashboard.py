#!/usr/bin/env python3
"""Build the interactive underfloor-heating dashboard as ONE self-contained HTML page.

Reads heating_history.csv (long format: post_date, window_start, window_end, profile, minutes),
keeps only heating-season dates (1 Oct - 30 Apr, judged by the post date), groups them by season
and writes docs/index.html, ready for GitHub Pages.

    python build_dashboard.py                        # CSV -> docs/index.html, Plotly loaded from a CDN
    python build_dashboard.py --plotly-js FILE.js    # embed a local Plotly build instead (works offline)

Only the standard library is needed.
"""
import argparse
import csv
import json
from datetime import date, datetime, timezone
from pathlib import Path

PLOTLY_CDN = "https://cdn.jsdelivr.net/npm/plotly.js-basic-dist-min@2.35.2/plotly-basic.min.js"
PROFILES = ["Unbiased", "Adjusted"]
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


def build_payload(csv_path):
    days, window_keys = load(csv_path)
    seasons, ignored = {}, 0
    for d in sorted(days):
        y = season_start_year(d)
        if y is None:
            ignored += 1
            continue
        row = [d.isoformat()]
        for p in PROFILES:
            row += [num(days[d][p][k]) for k in window_keys]  # raises KeyError if a window is missing
        seasons.setdefault(y, []).append(row)
    payload = {
        "windows": window_defs(window_keys),
        "profiles": PROFILES,
        "seasons": [
            {"id": str(y), "label": f"{y}–{str(y + 1)[2:]}", "rows": seasons[y]}
            for y in sorted(seasons, reverse=True)  # newest season first
        ],
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
  --accent:#2f66d0; --on-accent:#ffffff; --u:#3a6fd8; --a:#e8741a; --cap:#6f798d;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#14171f; --fg:#e8eaf0; --muted:#9aa3b5; --line:#2c3243; --panel:#1c212c;
    --accent:#6c9aff; --on-accent:#0d1220; --u:#6c9aff; --a:#ff9a4d; --cap:#aab3c5;
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
  const cu = css('--u'), ca = css('--a'), cc = css('--cap');
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

  // ---- windows: one bar per window, spanning its real time slot
  const x = [], wid = [], uy = [], ay = [], cdU = [], cdA = [];
  S.rows.forEach(r => {
    const d0 = ms(r[0]);
    W.forEach((w, k) => {
      const s = d0 + w.startOff * DAY + hmMs(w.start), e = d0 + w.endOff * DAY + hmMs(w.end);
      const sameDay = dstr(s) === dstr(e);
      const when = niceT(s) + ' → ' + (sameDay ? hhmm(e) : niceT(e));
      x.push(stamp((s + e) / 2)); wid.push(e - s);
      const u = r[1 + k], a = r[1 + W.length + k];
      uy.push(val(u, w.len)); ay.push(val(a, w.len));
      const note = m => m > w.len ? ' ⚠ longer than the window' : '';
      cdU.push([w.label, when, w.len, Math.round(100 * u / w.len), note(u), u]);
      cdA.push([w.label, when, w.len, Math.round(100 * a / w.len), note(a), a]);
    });
  });
  const wf = f => wid.map(v => v * f);

  // ---- daily totals: one point per calendar day, empty days left as gaps
  const bd = bounds(S), byDate = {};
  S.rows.forEach(r => { byDate[ms(r[0])] = r; });
  const tx = [], tu = [], ta = [], tcU = [], tcA = [];
  const cdTot = (t, tot, ws) => [niceD(t), (tot / 60).toFixed(1), tot, Math.round(100 * tot / SUM_LEN)]
    .concat(ws, ws.map((m, k) => Math.round(100 * m / W[k].len)));
  for (let t = bd.first; t <= bd.last; t += DAY) {
    tx.push(stamp(t + 8*HOUR + 30*MIN));
    const r = byDate[t];
    if (r) {
      const uw = r.slice(1, 1 + W.length), aw = r.slice(1 + W.length, 1 + 2*W.length);
      const ut = sum(uw), at = sum(aw);
      tu.push(val(ut, SUM_LEN)); ta.push(val(at, SUM_LEN));
      tcU.push(cdTot(t, ut, uw)); tcA.push(cdTot(t, at, aw));
    } else { tu.push(null); ta.push(null); tcU.push([]); tcA.push([]); }
  }

  const data = [];
  if (showU) data.push({ type: 'bar', name: 'Unbiased', legendgroup: 'U', x: x, y: uy, width: wf(0.96), customdata: cdU,
      marker: { color: cu, opacity: both ? 0.5 : 0.9, line: { color: cu, width: 1 } }, hovertemplate: HT_WIN, xaxis: 'x', yaxis: 'y' });
  if (showA) data.push({ type: 'bar', name: 'Adjusted', legendgroup: 'A', x: x, y: ay, width: wf(both ? 0.56 : 0.96), customdata: cdA,
      marker: { color: ca, opacity: 0.95 }, hovertemplate: HT_WIN, xaxis: 'x', yaxis: 'y' });
  if (showU) data.push({ type: 'scatter', mode: 'lines+markers', name: 'Unbiased', legendgroup: 'U', showlegend: false,
      x: tx, y: tu, customdata: tcU, connectgaps: false, line: { color: cu, width: 2.6 }, marker: { size: 5, color: cu },
      hovertemplate: HT_TOT, xaxis: 'x2', yaxis: 'y2' });
  if (showA) data.push({ type: 'scatter', mode: 'lines+markers', name: 'Adjusted', legendgroup: 'A', showlegend: false,
      x: tx, y: ta, customdata: tcA, connectgaps: false, line: { color: ca, width: 1.8 }, marker: { size: 5, color: ca },
      hovertemplate: HT_TOT, xaxis: 'x2', yaxis: 'y2' });

  const fg = css('--fg'), grid = css('--line'), panel = css('--panel');
  const layout = {
    uirevision: state.season + '|' + state.profile + '|' + state.units,
    barmode: 'overlay', bargap: 0, hovermode: 'closest', dragmode: 'zoom',
    margin: { l: 66, r: 16, t: 36, b: 10 },
    paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
    font: { color: fg, size: 12, family: 'system-ui,-apple-system,"Segoe UI",Roboto,sans-serif' },
    hoverlabel: { bgcolor: panel, bordercolor: grid, font: { color: fg } },
    legend: { orientation: 'h', x: 0, y: 1, yanchor: 'bottom' },
    xaxis: { type: 'date', anchor: 'y', gridcolor: grid, linecolor: grid, showticklabels: false },
    xaxis2: { type: 'date', anchor: 'y2', matches: 'x', gridcolor: grid, linecolor: grid,
      rangeslider: { visible: true, thickness: 0.09, bgcolor: panel, bordercolor: grid, borderwidth: 1 } },
    yaxis: { domain: [0.40, 1], anchor: 'x', rangemode: 'tozero', gridcolor: grid, zeroline: false, fixedrange: true,
      title: { text: pct ? 'Heating as % of window length' : 'Minutes heating in window' }, ticksuffix: pct ? '%' : '' },
    yaxis2: { domain: [0, 0.31], anchor: 'x2', rangemode: 'tozero', gridcolor: grid, zeroline: false, fixedrange: true,
      title: { text: pct ? 'Daily total (% of ' + SUM_LEN + ' min)' : 'Daily total (min)' }, ticksuffix: pct ? '%' : '' }
  };
  return { data: data, layout: layout, S: S };
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

$('foot').textContent = 'Source: Barbican Underfloor Heating blog (Atom feed) · data through ' + DATA.dataThrough + ' · page built ' + DATA.built +
  ' · Oct–Apr only; May–Sep posts are ignored';
render(true);
})();
</script>
</body>
</html>
'''


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", default="heating_history.csv")
    ap.add_argument("--out", default="docs/index.html")
    ap.add_argument("--plotly-js", help="path to a local plotly JS bundle to embed instead of loading it from a CDN")
    args = ap.parse_args()

    payload, ignored = build_payload(args.csv)
    if args.plotly_js:
        js = Path(args.plotly_js).read_text(encoding="utf-8").replace("</script", "<\\/script")
        plotly_tag = f"<script>{js}</script>"
    else:
        plotly_tag = f'<script src="{PLOTLY_CDN}" crossorigin="anonymous"></script>'

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
