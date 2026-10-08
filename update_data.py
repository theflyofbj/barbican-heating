#!/usr/bin/env python3
"""Daily data update for the Barbican underfloor-heating dashboard.

1. Reads the blog's feed and merges new posts into heating_history.csv.
2. Downloads hourly air temperatures (Open-Meteo, central London) into temperature_history.csv.

    python update_data.py            # normal daily run: newest 150 posts + last ~30 days of temperature
    python update_data.py --full     # re-read every post on the blog and rebuild all temperatures

Standard library only. A failure in the temperature download never stops the heating update.
"""
import argparse
import csv
import html
import json
import re
import sys
import time as _time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

from parse_post import parse_post

BLOG = "https://barbicanunderfloorheating.blogspot.com"
PAGE = 150                      # Blogger returns at most 150 entries per request
LAT, LON = 51.52, -0.09         # Barbican, London
GENUINE_BLOG_TIMES = {"00:30", "01:00", "01:30"}   # blog clock (Pacific); real daily posts appear at these times
TEST_SIGNATURE = (0.0, 210.0, 217.0)               # test posts carry exactly these window minutes on both profiles
HEATING_HEADER = ["post_date", "window_start", "window_end", "profile", "minutes"]


# ----------------------------------------------------------------------------- HTTP

def fetch_json(url, tries=4):
    last = None
    for n in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "barbican-heating-dashboard/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as e:  # network hiccup or temporary server error: wait and retry
            last = e
            _time.sleep(2 * (n + 1))
    raise RuntimeError(f"could not fetch {url}: {last}")


# ----------------------------------------------------------------------------- blog posts

def post_text(content_html):
    t = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", content_html)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    return re.sub(r"[ \t\xa0]+", " ", t)


def fetch_entries(full):
    """Yield feed entries, newest first. Pages advance by the number actually returned."""
    start = 1
    while True:
        url = f"{BLOG}/feeds/posts/default?alt=json&max-results={PAGE}&start-index={start}"
        feed = fetch_json(url)["feed"]
        entries = feed.get("entry", [])
        for e in entries:
            yield e
        if not full or not entries:
            return
        start += len(entries)
        if start > int(feed["openSearch$totalResults"]["$t"]):
            return


def read_posts(full):
    """Return {post_date: [csv rows]} for genuine posts, plus a list of skipped posts."""
    best, skipped = {}, []
    for e in fetch_entries(full):
        stamp = e["published"]["$t"]          # e.g. 2026-10-08T00:30:00.001-07:00 (blog clock)
        d, hm = date.fromisoformat(stamp[:10]), stamp[11:16]
        try:
            rows = parse_post(post_text(e["content"]["$t"]), d)
        except AssertionError as err:
            skipped.append((d, hm, f"inconsistent post: {err}"))
            continue
        if len(rows) != 6:
            skipped.append((d, hm, f"{len(rows)} data points, expected 6 (not a daily overview?)"))
            continue
        vals = {p: [r[3] for r in rows if r[2] == p] for p in ("Unbiased", "Adjusted")}
        if all(tuple(v) == TEST_SIGNATURE for v in vals.values()) and hm not in GENUINE_BLOG_TIMES:
            skipped.append((d, hm, "probable test post"))
            continue
        out = [(d.isoformat(), s.strftime("%Y-%m-%d %H:%M"), en.strftime("%Y-%m-%d %H:%M"), p, f"{m:g}")
               for s, en, p, m in rows]
        if d not in best or stamp > best[d][0]:      # if a day has several genuine posts, keep the latest
            best[d] = (stamp, out)
    return {d: v[1] for d, v in best.items()}, skipped


def update_heating(path, full):
    existing = {}
    try:
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                existing.setdefault(r["post_date"], []).append(tuple(r[h] for h in HEATING_HEADER))
    except FileNotFoundError:
        pass
    fresh, skipped = read_posts(full)
    new_days, changed = [], []
    for d, rows in fresh.items():
        k = d.isoformat()
        if k not in existing:
            new_days.append(k)
        elif sorted(existing[k]) != sorted(rows):
            changed.append(k)
        existing[k] = rows
    flat = [r for rows in existing.values() for r in rows]
    flat.sort(key=lambda r: (r[1], r[3] != "Unbiased"))
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEATING_HEADER)
        w.writerows(flat)
    print(f"heating: {len(fresh)} posts read, {len(new_days)} new days {sorted(new_days)[-3:]}, "
          f"{len(changed)} changed {sorted(changed)[-3:]}, {len(skipped)} skipped; {len(existing)} days in {path}")
    for d, hm, why in skipped[:10]:
        print(f"  skipped {d} {hm}: {why}")
    return date.fromisoformat(min(existing)) if existing else None


# ----------------------------------------------------------------------------- temperature

def om_hourly(url):
    """Return {'YYYY-MM-DDTHH:MM' (UTC): temp} from an Open-Meteo response, skipping missing values."""
    h = fetch_json(url)["hourly"]
    return {t: v for t, v in zip(h["time"], h["temperature_2m"]) if v is not None}


def archive_url(a, b):
    q = urllib.parse.urlencode({"latitude": LAT, "longitude": LON, "start_date": a.isoformat(), "end_date": b.isoformat(),
                                "hourly": "temperature_2m", "timezone": "UTC"})
    return "https://archive-api.open-meteo.com/v1/archive?" + q


def forecast_url(past_days):
    q = urllib.parse.urlencode({"latitude": LAT, "longitude": LON, "past_days": past_days, "forecast_days": 1,
                                "hourly": "temperature_2m", "timezone": "UTC"})
    return "https://api.open-meteo.com/v1/forecast?" + q


def update_temps(path, first_post_date, full):
    have = {}
    try:
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                have[r["time_utc"]] = r["temp_c"]
    except FileNotFoundError:
        pass
    today = datetime.now(timezone.utc).date()
    lag = today - timedelta(days=8)                       # the archive is final to about a week ago
    start = (first_post_date or date(2021, 1, 1)) - timedelta(days=3)
    if full or not have:
        start_fetch = start
    else:
        start_fetch = max(start, date.fromisoformat(max(have)[:10]) - timedelta(days=30))
    got = {}
    # recent days: forecast service (covers the last ~week the archive does not have yet, and today up to now)
    got.update(om_hourly(forecast_url(14)))
    # older days: archive, in 1-year chunks; it overrides the forecast service where both exist
    a = start_fetch
    while a <= lag:
        b = min(lag, date(a.year, 12, 31))
        got.update(om_hourly(archive_url(a, b)))
        a = b + timedelta(days=1)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
    n_new = 0
    for t, v in got.items():
        if t > now:
            continue                                      # never keep forecast hours
        if have.get(t) != f"{v:g}":
            n_new += 1
        have[t] = f"{v:g}"
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time_utc", "temp_c"])
        for t in sorted(have):
            w.writerow([t, have[t]])
    print(f"temperature: {len(got)} hourly values fetched, {n_new} new or changed; {len(have)} hours in {path}, latest {max(have)}")


# ----------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--heating", default="heating_history.csv")
    ap.add_argument("--temps", default="temperature_history.csv")
    ap.add_argument("--full", action="store_true", help="re-read all blog posts and rebuild all temperatures")
    ap.add_argument("--no-feed", action="store_true")
    ap.add_argument("--no-temps", action="store_true")
    args = ap.parse_args()

    first = None
    if not args.no_feed:
        first = update_heating(args.heating, args.full)
    elif not args.no_temps:
        with open(args.heating, newline="") as f:
            first = min(date.fromisoformat(r["post_date"]) for r in csv.DictReader(f))
    if not args.no_temps:
        try:
            update_temps(args.temps, first, args.full)
        except Exception as e:
            print(f"WARNING: temperature update failed ({e}); keeping the existing file", file=sys.stderr)


if __name__ == "__main__":
    main()
