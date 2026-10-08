#!/usr/bin/env python3
"""
rba_update.py

Keeps rba-data.json current. That file feeds the "Where rates have been
heading" section on the homepage (stat cards, step chart, decisions table).

How it works:
  1. Reads the RBA's published cash rate changes, first from the statistics
     table CSV (A2), then from the cash rate web page if the CSV fails.
  2. Merges any decision not already in rba-data.json. History is never
     rewritten, and every new entry must chain correctly from the previous
     cash rate. If anything looks off, the script exits non-zero and leaves
     the file alone, so the site keeps showing the last good data.
  3. Leaves the hand-maintained fields (meetings, avg_new_variable) untouched.

Dates: the RBA lists the EFFECTIVE date (the day after the Board meets). The
site shows the DECISION date, so one day is subtracted.

Usage:
    pip install requests
    python rba_update.py                       # update rba-data.json in place
    python rba_update.py --dry-run             # show what would change
"""

import argparse
import csv
import io
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone

import requests

A2_CSV_URL = "https://www.rba.gov.au/statistics/tables/csv/a2-data.csv"
CASH_RATE_PAGE_URL = "https://www.rba.gov.au/statistics/cash-rate/"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; homeloaninterestrates.au rate updater)"}
TIMEOUT = 30

SERIES_CHANGE = "ARBAMPCCCR"      # "Change in Cash Rate Target"
SERIES_NEW_RATE = "ARBAMPCNCRT"   # "New Cash Rate Target"
DATE_FORMATS = ("%d-%b-%Y", "%d-%b-%y", "%d/%m/%Y", "%Y-%m-%d", "%d %b %Y", "%d %B %Y")
TOLERANCE = 0.006                 # rounding slack when checking rate chains


def parse_date(text):
    text = (text or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def to_float(text):
    text = (text or "").strip().replace("\u2212", "-").replace("\u2013", "-").replace("+", "")
    try:
        return float(text)
    except ValueError:
        return None


def parse_a2_csv(text):
    """RBA table A2: a few metadata rows (including 'Series ID'), then data rows
    of effective date, change, new cash rate target."""
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    change_col, rate_col = 1, 2
    for row in rows:
        if row and row[0].strip().lower() == "series id":
            ids = [c.strip().upper() for c in row]
            if SERIES_CHANGE in ids and SERIES_NEW_RATE in ids:
                change_col, rate_col = ids.index(SERIES_CHANGE), ids.index(SERIES_NEW_RATE)
            break
    out = []
    for row in rows:
        if len(row) <= max(change_col, rate_col):
            continue
        d, change, rate = parse_date(row[0]), to_float(row[change_col]), to_float(row[rate_col])
        if d and change is not None and rate is not None:
            out.append((d, change, rate))
    return out


_PAGE_ROW = re.compile(
    r"(\d{1,2}\s+[A-Z][a-z]{2,8}\s+\d{4})\s*</t[dh]>\s*<t[dh][^>]*>\s*"
    r"([+\-\u2212\u2013]?\d+\.\d+)\s*</t[dh]>\s*<t[dh][^>]*>\s*(\d+\.\d+)"
)


def parse_cash_rate_page(html):
    """Fallback: the table on rba.gov.au/statistics/cash-rate/ (Effective Date,
    Change % points, Cash rate target %). Holds appear as 0.00."""
    out = []
    for d_txt, change_txt, rate_txt in _PAGE_ROW.findall(html):
        d, change, rate = parse_date(d_txt), to_float(change_txt), to_float(rate_txt)
        if d and change is not None and rate is not None:
            out.append((d, change, rate))
    return out


def fetch_observed():
    errors = []
    for name, url, parser in (("A2 CSV", A2_CSV_URL, parse_a2_csv),
                              ("cash rate page", CASH_RATE_PAGE_URL, parse_cash_rate_page)):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            rows = parser(resp.text)
            if rows:
                print(f"Read {len(rows)} rows from the RBA {name}.")
                return rows
            errors.append(f"{name}: fetched but no rows could be parsed")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
    raise RuntimeError("Could not read RBA data. " + "; ".join(errors))


def merge(data, observed, today=None):
    """Return (new_data, added). Raises ValueError if anything doesn't chain."""
    today = today or datetime.now(timezone.utc).date()
    decisions = [dict(d) for d in data["decisions"]]
    last_date = date.fromisoformat(decisions[-1]["date"]) if decisions else date.fromisoformat(data["baseline_date"])
    rate = round(decisions[-1]["rate"] if decisions else data["baseline_rate"], 2)
    known = {d["date"]: d for d in decisions}
    added = []

    for eff, change, new_rate in sorted(observed):
        if abs(change) < 0.001:
            continue                                     # a hold, nothing to record
        decision = eff - timedelta(days=1)
        key = decision.isoformat()
        if key in known:                                 # already stored: must agree
            if abs(known[key]["rate"] - new_rate) > TOLERANCE:
                raise ValueError(f"{key}: stored rate {known[key]['rate']} but RBA says {new_rate}")
            continue
        if decision <= last_date:
            continue                                     # older history we deliberately don't touch
        if decision > today:
            raise ValueError(f"{key} is in the future")
        if abs((rate + change) - new_rate) > TOLERANCE:
            raise ValueError(f"{key}: {rate} + {change} does not equal reported {new_rate}")
        entry = {"date": key, "change": round(change, 2), "rate": round(new_rate, 2)}
        decisions.append(entry)
        added.append(entry)
        rate, last_date = entry["rate"], decision

    out = dict(data)
    out["decisions"] = decisions
    return out, added


def validate(data):
    rate = data["baseline_rate"]
    prev = data["baseline_date"]
    for d in data["decisions"]:
        if d["date"] <= prev:
            raise ValueError(f"{d['date']} is out of order")
        if abs(rate + d["change"] - d["rate"]) > TOLERANCE:
            raise ValueError(f"{d['date']}: chain broken ({rate} {d['change']:+} != {d['rate']})")
        rate, prev = d["rate"], d["date"]
    if not (0 <= rate <= 15):
        raise ValueError(f"implausible current cash rate {rate}")


def main():
    ap = argparse.ArgumentParser(description="Merge new RBA decisions into rba-data.json")
    ap.add_argument("--file", default="rba-data.json")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(args.file) as f:
        data = json.load(f)
    validate(data)

    try:
        merged, added = merge(data, fetch_observed())
        validate(merged)
    except (RuntimeError, ValueError) as e:
        print(f"ERROR: {e}\nrba-data.json left unchanged.", file=sys.stderr)
        sys.exit(1)

    if not added:
        print("No new RBA decisions.")
        return
    for a in added:
        print(f"New decision: {a['date']} {a['change']:+.2f} -> {a['rate']:.2f}%")
    if args.dry_run:
        return
    merged["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(args.file, "w") as f:
        json.dump(merged, f, indent=2)
        f.write("\n")
    print(f"Updated {args.file}.")


if __name__ == "__main__":
    main()
