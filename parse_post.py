"""Parse one 'Underfloor Heating Daily Time Overview' post into dated data points.

Each post gives minutes of heating for three windows (13:00-16:00, 20:30-01:30,
02:30-07:30) under two profiles (Unbiased, Adjusted) = 6 data points.
The post is assumed to be published at 08:30 London time on its date, and each
window is dated as the most recent occurrence that ended at or before that time.
"""
import re
from datetime import date, datetime, time, timedelta

POST_TIME = time(8, 30)  # assumed publish time (London local time)

WINDOW_LINE = re.compile(
    r"Profile A (Unbiased|Adjusted) - (\d\d):(\d\d) to (\d\d):(\d\d)"
    r" - Total Time = ([\d.]+) minutes"
)
TOTAL_LINE = re.compile(r"Profile A (Unbiased|Adjusted) - Total Time = ([\d.]+) minutes")


def window_for(post_dt, start_t, end_t):
    """Return (start, end) of the latest window occurrence ending at or before post_dt."""
    end = datetime.combine(post_dt.date(), end_t)
    if end > post_dt:
        end -= timedelta(days=1)
    span = (
        datetime.combine(date.min, end_t) - datetime.combine(date.min, start_t)
    ) % timedelta(days=1)
    return end - span, end


def parse_post(text, post_date):
    post_dt = datetime.combine(post_date, POST_TIME)
    rows = []
    for m in WINDOW_LINE.finditer(text):
        profile, sh, sm, eh, em, minutes = m.groups()
        start, end = window_for(post_dt, time(int(sh), int(sm)), time(int(eh), int(em)))
        assert end <= post_dt, f"window ends after post time: {end}"
        rows.append((start, end, profile, float(minutes)))

    # Cross-check against the totals stated in the post
    for profile, stated in TOTAL_LINE.findall(text):
        got = sum(r[3] for r in rows if r[2] == profile)
        assert abs(got - float(stated)) < 0.01, f"{profile}: windows sum to {got}, post says {stated}"

    return sorted(rows, key=lambda r: (r[0], r[2] != "Unbiased"))


SAMPLE_8_OCT = """
Profile A Unbiased - 13:00 to 16:00 - Total Time = 0.00 minutes
Profile A Unbiased - 20:30 to 01:30 - Total Time = 80.00 minutes
Profile A Unbiased - 02:30 to 07:30 - Total Time = 219.00 minutes
Profile A Unbiased - Total Time = 299.00 minutes

Profile A Adjusted - 13:00 to 16:00 - Total Time = 0.00 minutes
Profile A Adjusted - 20:30 to 01:30 - Total Time = 80.00 minutes
Profile A Adjusted - 02:30 to 07:30 - Total Time = 219.00 minutes
Profile A Adjusted - Total Time = 299.00 minutes
"""

if __name__ == "__main__":
    rows = parse_post(SAMPLE_8_OCT, date(2026, 10, 8))
    print("window_start,window_end,profile,minutes")
    for start, end, profile, minutes in rows:
        print(f"{start:%Y-%m-%d %H:%M},{end:%Y-%m-%d %H:%M},{profile},{minutes:g}")
    print(f"\n{len(rows)} data points; totals check passed")
