#!/usr/bin/env python3
"""
cdr_rate_fetcher.py

Pulls home loan product data from every bank's public, unauthenticated
Consumer Data Right (CDR) Product Reference Data API, flattens it, and
writes rates.json for the WordPress rate table.

No API keys or accreditation needed.

Usage:
    pip install requests
    python cdr_rate_fetcher.py --output rates.json
    python cdr_rate_fetcher.py --output rates.json --limit 5   # test run, first 5 banks
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone

import requests

# Public register endpoint (the non-"summary" /brands endpoint needs mTLS
# accreditation and fails for anyone else).
CDR_REGISTER_URL = "https://api.cdr.gov.au/cdr-register/v1/banking/data-holders/brands/summary"

# Version negotiation: each server answers with the highest version it
# supports between x-min-v and x-v, so one stale version can't break a bank.
REGISTER_HEADERS = {"x-v": "2", "x-min-v": "1", "Accept": "application/json"}
PRODUCT_LIST_HEADERS = {"x-v": "4", "x-min-v": "1", "Accept": "application/json"}
PRODUCT_DETAIL_HEADERS = {"x-v": "7", "x-min-v": "1", "Accept": "application/json"}

REQUEST_TIMEOUT = 20      # seconds per request
POLITENESS_DELAY = 0.3    # seconds between banks
PRODUCT_CATEGORY = "RESIDENTIAL_MORTGAGES"
KEEP_RATE_TYPES = {"FIXED", "VARIABLE"}

# CDR featureType values -> labels shown in the Features column.
FEATURE_LABELS = {
    "OFFSET": "Offset",
    "REDRAW": "Redraw",
    "EXTRA_REPAYMENTS": "Extra repayments",
}

session = requests.Session()
session.headers.update({"User-Agent": "hlir-rate-fetcher/1.1"})


def get_banking_data_holders():
    """Every registered banking brand and its public base URI."""
    resp = session.get(CDR_REGISTER_URL, headers=REGISTER_HEADERS, timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        raise RuntimeError(f"CDR Register returned HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()

    holders, seen = [], set()
    for entry in data.get("data", []):
        brand_name = entry.get("brandName") or entry.get("legalEntityName")
        base_uri = (entry.get("publicBaseUri") or "").rstrip("/")
        if brand_name and base_uri and base_uri not in seen:
            seen.add(base_uri)
            holders.append({"brand_name": brand_name, "base_uri": base_uri})
    return holders


def get_home_loan_products(base_uri):
    """Current residential mortgage products for one bank, following pagination."""
    products = []
    url = f"{base_uri}/cds-au/v1/banking/products"
    params = {"product-category": PRODUCT_CATEGORY, "page-size": 100, "effective": "CURRENT"}
    pages = 0

    while url and pages < 50:
        resp = session.get(url, params=params, headers=PRODUCT_LIST_HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        body = resp.json()
        products.extend(body.get("data", {}).get("products", []))
        url = body.get("links", {}).get("next")
        params = None  # the next link already carries its query string
        pages += 1

    # Some banks ignore the category filter, so check it again here.
    return [p for p in products if p.get("productCategory", PRODUCT_CATEGORY) == PRODUCT_CATEGORY]


def get_product_detail(base_uri, product_id):
    url = f"{base_uri}/cds-au/v1/banking/products/{product_id}"
    resp = session.get(url, headers=PRODUCT_DETAIL_HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("data", {})


def to_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def max_lvr_from_tiers(tiers):
    """Maximum LVR as a whole percentage (80, not 0.8), if the rate has an LVR tier."""
    for tier in tiers or []:
        name = (tier.get("name") or "").lower()
        if "lvr" in name or "loan to value" in name or "loan-to-value" in name:
            value = to_number(tier.get("maximumValue"))
            if value is None:
                return None
            if 0 < value <= 1:
                value *= 100
            return round(value)
    return None


def normalize_product(brand_name, summary, detail):
    """One row per usable lending rate (CDR lists several per product)."""
    rows = []
    name = detail.get("name") or summary.get("name") or ""

    features = []
    for f in detail.get("features", []) or []:
        label = FEATURE_LABELS.get(f.get("featureType"))
        if label and label not in features:
            features.append(label)

    is_refinance_named = "refinanc" in name.lower()

    for rate in detail.get("lendingRates", []) or []:
        rate_type = rate.get("lendingRateType")
        interest = to_number(rate.get("rate"))
        comparison = to_number(rate.get("comparisonRate"))

        # Only plain fixed/variable rates with a comparison rate are shown on the
        # site; discount, penalty and introductory entries would distort it.
        if rate_type not in KEEP_RATE_TYPES:
            continue
        if not interest or interest <= 0 or not comparison or comparison <= 0:
            continue

        rows.append({
            "lender": brand_name,
            "product_name": name,
            "rate_type": rate_type,                       # FIXED / VARIABLE
            "loan_purpose": rate.get("loanPurpose"),      # OWNER_OCCUPIED / INVESTMENT
            "repayment_type": rate.get("repaymentType"),  # PRINCIPAL_AND_INTEREST / INTEREST_ONLY
            "period": rate.get("period"),                 # e.g. "P3Y" for a 3-year fixed
            "interest_rate": rate.get("rate"),
            "comparison_rate": rate.get("comparisonRate"),
            "max_lvr": max_lvr_from_tiers(rate.get("tiers")),
            "features": features,
            # Heuristic: CDR has no "refinance" purpose, so this flags products
            # whose own name mentions refinancing.
            "is_refinance_named": is_refinance_named,
        })

    # Drop exact duplicates (some banks repeat identical tiers).
    unique, seen = [], set()
    for row in rows:
        key = json.dumps(row, sort_keys=True)
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


def run(output_path, limit=None):
    print("Fetching banking data holders from the CDR Register...")
    holders = get_banking_data_holders()
    if limit:
        holders = holders[:limit]
    print(f"Found {len(holders)} banking brands to query.")

    all_rows, failures, lenders_with_rows = [], [], set()

    for holder in holders:
        brand, base_uri = holder["brand_name"], holder["base_uri"]
        try:
            products = get_home_loan_products(base_uri)
            count_before = len(all_rows)
            for summary in products:
                try:
                    detail = get_product_detail(base_uri, summary["productId"])
                    all_rows.extend(normalize_product(brand, summary, detail))
                except Exception as e:
                    failures.append({"brand": brand, "product_id": summary.get("productId"), "error": str(e)[:200]})
            added = len(all_rows) - count_before
            if added:
                lenders_with_rows.add(brand)
            print(f"  {brand}: {len(products)} products, {added} rates")
        except Exception as e:
            failures.append({"brand": brand, "error": str(e)[:200]})
            print(f"  {brand}: FAILED ({str(e)[:120]})")

        time.sleep(POLITENESS_DELAY)

    if not all_rows:
        raise RuntimeError("No rate rows collected; not overwriting rates.json.")

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lender_count": len(lenders_with_rows),
        "row_count": len(all_rows),
        "rows": all_rows,
        "failures": failures,
    }

    with open(output_path, "w") as f:
        json.dump(output, f, separators=(",", ":"))

    print(f"\nDone. {len(all_rows)} rate rows from {len(lenders_with_rows)} lenders written to {output_path}")
    if failures:
        print(f"{len(failures)} banks or products failed, see 'failures' in the output file.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch home loan rates from the CDR Register.")
    parser.add_argument("--output", default="rates.json", help="Output JSON file path")
    parser.add_argument("--limit", type=int, default=None, help="Only query the first N banks (for testing)")
    args = parser.parse_args()

    try:
        run(args.output, args.limit)
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)
