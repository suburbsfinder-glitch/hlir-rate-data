#!/usr/bin/env python3
"""
cdr_rate_fetcher.py

Pulls home loan product data directly from every ADI's public,
unauthenticated Consumer Data Right (CDR) "Product Reference Data"
API, normalises it into one flat schema, and writes it to JSON.

No API keys, no accreditation, no licence. This only works for
ADIs (banks, credit unions, building societies) registered on the
official CDR Register, which is exactly the "ADI list" scope you
want, there's nothing extra to filter.

Usage:
    pip install requests
    python cdr_rate_fetcher.py --output rates.json
    python cdr_rate_fetcher.py --output rates.json --limit 5   # test run, first 5 banks only
    python cdr_rate_fetcher.py --inspect 3 --limit 10          # print raw CDR rate entries, writes nothing
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone

import requests

CDR_REGISTER_URL = "https://api.cdr.gov.au/cdr-register/v1/banking/data-holders/brands"
CDR_API_VERSION = "3"          # x-v header, per Consumer Data Standards
REQUEST_TIMEOUT = 15           # seconds, per-bank call
POLITENESS_DELAY = 0.5         # seconds between banks, be a good citizen
PRODUCT_CATEGORY = "RESIDENTIAL_MORTGAGES"

# Only plain fixed and variable rates go in the table. CDR also publishes
# discount, introductory and penalty rate entries that aren't a loan's rate.
KEEP_RATE_TYPES = {"FIXED", "VARIABLE"}

STAFF_NAME = re.compile(r"\b(staff|employees?|team members?)\b", re.I)
_ISO_DURATION = re.compile(r"^P(?:(\d+)Y)?(?:(\d+)M)?$")
_TEXT_YEARS = re.compile(r"\b(\d{1,2})\s*[- ]?\s*(?:years?|yrs?|y)\b", re.I)
_TEXT_MONTHS = re.compile(r"\b(\d{1,3})\s*[- ]?\s*(?:months?|mths?|mo)\b", re.I)


def normalise_period(value):
    """ISO 8601 duration -> 'P3Y' (whole years) or 'P18M'. None if unusable."""
    if not value or not isinstance(value, str):
        return None
    m = _ISO_DURATION.match(value.strip().upper())
    if not m:
        return None
    years, months = (int(x) if x else 0 for x in m.groups())
    total = years * 12 + months
    if total <= 0:
        return None
    return f"P{total // 12}Y" if total % 12 == 0 else f"P{total}M"


def period_from_text(*texts):
    """Last-resort guess from wording like '3 Year Fixed'. Years 1-10 only,
    so a '30 year loan term' is never mistaken for a fixed period."""
    for text in texts:
        if not text:
            continue
        m = _TEXT_YEARS.search(text)
        if m and 1 <= int(m.group(1)) <= 10:
            return f"P{int(m.group(1))}Y"
        m = _TEXT_MONTHS.search(text)
        if m and 6 <= int(m.group(1)) <= 120:
            return normalise_period(f"P{int(m.group(1))}M")
    return None


def fixed_period(rate, product_name):
    """Fixed-rate term for one lendingRates entry. The Consumer Data Standards
    keep it in additionalValue (ISO 8601 duration). Some data holders use other
    fields or only mention it in text, so try each in turn. Run the script with
    --inspect to see exactly what a bank sends."""
    if (rate.get("lendingRateType") or "").upper() != "FIXED":
        return None
    return (
        normalise_period(rate.get("additionalValue"))
        or normalise_period(rate.get("period"))
        or period_from_text(rate.get("additionalInfo"), product_name)
    )


def exclusion_reason(summary, detail):
    """Products the public can't apply for shouldn't headline a comparison
    table. Returns a reason string, or None to keep the product."""
    for e in detail.get("eligibility") or []:
        if str(e.get("eligibilityType", "")).upper() == "STAFF":
            return "staff (eligibility)"
    if STAFF_NAME.search(summary.get("name") or ""):
        return "staff (name)"
    return None


def collapse_rows(rows):
    """Drop exact duplicates and merge LVR tiers that share the same rate,
    keeping the highest max LVR (the loan is offered at that rate up to it)."""
    merged = {}
    for r in rows:
        key = (r["lender"], r["product_name"], r["rate_type"], r["loan_purpose"],
               r["repayment_type"], r["period"], r["interest_rate"], r["comparison_rate"])
        cur = merged.get(key)
        if cur is None:
            merged[key] = dict(r)
        elif (r["max_lvr"] or 0) > (cur["max_lvr"] or 0):
            cur["max_lvr"] = r["max_lvr"]
    return list(merged.values())


def find_anomalies(rows):
    """Comparison rates below the interest rate shouldn't happen for a normal
    loan. They're kept in the data but listed so a human can check them."""
    out = []
    for r in rows:
        try:
            if float(r["comparison_rate"]) < float(r["interest_rate"]) - 0.0005:
                out.append({"lender": r["lender"], "product_name": r["product_name"],
                            "interest_rate": r["interest_rate"], "comparison_rate": r["comparison_rate"]})
        except (TypeError, ValueError):
            continue
    return out


def get_banking_data_holders():
    """Fetch every registered banking brand and its public base URI
    from the official CDR Register. This list changes as banks join
    CDR or update infrastructure, so always fetch it fresh rather
    than hardcoding it."""
    resp = requests.get(CDR_REGISTER_URL, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    holders = []
    for entry in data.get("data", []):
        brand_name = entry.get("brandName") or entry.get("legalEntityName")
        base_uri = entry.get("publicBaseUri")
        if brand_name and base_uri:
            holders.append({"brand_name": brand_name, "base_uri": base_uri.rstrip("/")})
    return holders


def get_home_loan_products(base_uri):
    """List current residential mortgage products for one bank,
    following CDR pagination (links.next) until exhausted."""
    products = []
    url = f"{base_uri}/cds-au/v1/banking/products"
    params = {"product-category": PRODUCT_CATEGORY, "page-size": 100, "effective": "CURRENT"}
    headers = {"x-v": CDR_API_VERSION, "Accept": "application/json"}

    while url:
        resp = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        body = resp.json()
        products.extend(body.get("data", {}).get("products", []))

        next_url = body.get("links", {}).get("next")
        url = next_url
        params = None  # next_url already has query params baked in

    return products


def get_product_detail(base_uri, product_id):
    """Fetch full detail, lending rates, fees, eligibility, for one product."""
    url = f"{base_uri}/cds-au/v1/banking/products/{product_id}"
    headers = {"x-v": CDR_API_VERSION, "Accept": "application/json"}
    resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("data", {})


def normalize_product(brand_name, summary, detail):
    """Flatten CDR's nested schema into the shape the WordPress rate
    table expects. CDR allows multiple lendingRates entries per
    product (e.g. different LVR tiers), so this yields one row per
    rate tier rather than one row per product."""
    rows = []
    lending_rates = detail.get("lendingRates", [])
    fees = detail.get("fees", [])
    fee_summary = ", ".join(
        f["name"] for f in fees if f.get("amount") not in (None, "0", 0)
    ) or "No ongoing fees listed"

    # Friendly labels for CDR's featureType enum, only kept if present.
    feature_labels = {
        "OFFSET": "Offset",
        "REDRAW_FACILITY": "Redraw",
        "ADDITIONAL_REPAYMENTS": "Extra repayments",
        "SPLIT_ACCOUNT_FACILITY": "Split facility",
        "INTEREST_ONLY_REPAYMENT": "Interest only option",
    }
    features = [
        feature_labels.get(f.get("featureType"), f.get("featureType"))
        for f in detail.get("features", [])
        if f.get("featureType") in feature_labels
    ]
    is_refinance_named = "refinanc" in (summary.get("name") or "").lower()

    for rate in lending_rates:
        if (rate.get("lendingRateType") or "").upper() not in KEEP_RATE_TYPES:
            continue
        rows.append({
            "lender": brand_name,
            "product_name": summary.get("name"),
            "product_id": summary.get("productId"),
            "rate_type": rate.get("lendingRateType"),          # FIXED / VARIABLE
            "loan_purpose": rate.get("loanPurpose"),            # OWNER_OCCUPIED / INVESTMENT
            "repayment_type": rate.get("repaymentType"),        # PRINCIPAL_AND_INTEREST / INTEREST_ONLY
            "period": fixed_period(rate, summary.get("name")),  # "P3Y" etc, fixed-rate term, None if variable
            "interest_rate": rate.get("rate"),
            "comparison_rate": rate.get("comparisonRate"),
            "max_lvr": next(
                (t.get("maximumValue") for t in rate.get("tiers", []) if t.get("name") == "lvr"),
                None,
            ),
            "fees_summary": fee_summary,
            "features": features,
            # Heuristic only: CDR has no "refinance" loan purpose, this just
            # flags products whose own name mentions it, e.g. "... Refinance Offer".
            "is_refinance_named": is_refinance_named,
            "last_updated": detail.get("lastUpdated"),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        })
    return rows


def run(output_path, limit=None, inspect=0):
    print("Fetching banking data holders from the CDR Register...")
    holders = get_banking_data_holders()
    if limit:
        holders = holders[:limit]
    print(f"Found {len(holders)} banking brands to query.")

    all_rows = []
    failures = []
    excluded = []
    inspected = 0

    for holder in holders:
        brand = holder["brand_name"]
        base_uri = holder["base_uri"]
        try:
            products = get_home_loan_products(base_uri)
            for summary in products:
                try:
                    detail = get_product_detail(base_uri, summary["productId"])
                    if inspect:
                        fixed = [r for r in detail.get("lendingRates", [])
                                 if (r.get("lendingRateType") or "").upper() == "FIXED"]
                        if fixed and inspected < inspect:
                            inspected += 1
                            print(json.dumps({"brand": brand, "product": summary.get("name"),
                                              "lendingRate": fixed[0],
                                              "eligibility": detail.get("eligibility")}, indent=2))
                    reason = exclusion_reason(summary, detail)
                    if reason:
                        excluded.append({"lender": brand, "product_name": summary.get("name"), "reason": reason})
                        continue
                    all_rows.extend(normalize_product(brand, summary, detail))
                except Exception as e:
                    failures.append({"brand": brand, "product_id": summary.get("productId"), "error": str(e)})
            print(f"  {brand}: {len(products)} home loan products")
        except Exception as e:
            failures.append({"brand": brand, "error": str(e)})
            print(f"  {brand}: FAILED ({e})")

        if inspect and inspected >= inspect:
            break
        time.sleep(POLITENESS_DELAY)

    if inspect:
        print(f"\nInspected {inspected} fixed-rate entries. Nothing written.")
        return

    raw_count = len(all_rows)
    rows = collapse_rows(all_rows)
    anomalies = find_anomalies(rows)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lender_count": len({r["lender"] for r in rows}),
        "raw_row_count": raw_count,
        "row_count": len(rows),
        "rows": rows,
        "excluded": excluded,
        "anomalies": anomalies,
        "failures": failures,
    }

    with open(output_path, "w") as f:
        json.dump(output, f, separators=(",", ":"))   # compact, WordPress doesn't need pretty-printing

    fixed_rows = [r for r in rows if r["rate_type"] == "FIXED"]
    with_term = [r for r in fixed_rows if r["period"]]
    print(f"\nDone. {len(rows)} rate rows written to {output_path} ({raw_count - len(rows)} duplicates merged).")
    print(f"Fixed-rate rows with a term: {len(with_term)} of {len(fixed_rows)}")
    if excluded:
        print(f"{len(excluded)} products excluded (staff-only), see 'excluded' in the output file.")
    if anomalies:
        print(f"{len(anomalies)} rows have a comparison rate below the interest rate, see 'anomalies'.")
    if failures:
        print(f"{len(failures)} banks or products failed, see 'failures' in the output file.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch home loan rates from the CDR Register.")
    parser.add_argument("--output", default="rates.json", help="Output JSON file path")
    parser.add_argument("--limit", type=int, default=None, help="Only query the first N banks (for testing)")
    parser.add_argument("--inspect", type=int, default=0,
                        help="Print N raw fixed-rate entries (with eligibility) from live banks and exit, writes nothing")
    args = parser.parse_args()

    try:
        run(args.output, args.limit, args.inspect)
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)
