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
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone

import requests

CDR_REGISTER_URL = "https://api.cdr.gov.au/cdr-register/v1/banking/data-holders/brands"
CDR_API_VERSION = "3"          # x-v header, per Consumer Data Standards
REQUEST_TIMEOUT = 15           # seconds, per-bank call
POLITENESS_DELAY = 0.5         # seconds between banks, be a good citizen
PRODUCT_CATEGORY = "RESIDENTIAL_MORTGAGES"


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
        rows.append({
            "lender": brand_name,
            "product_name": summary.get("name"),
            "product_id": summary.get("productId"),
            "rate_type": rate.get("lendingRateType"),          # FIXED / VARIABLE
            "loan_purpose": rate.get("loanPurpose"),            # OWNER_OCCUPIED / INVESTMENT
            "repayment_type": rate.get("repaymentType"),        # PRINCIPAL_AND_INTEREST / INTEREST_ONLY
            "period": rate.get("period"),                       # ISO 8601 duration, e.g. "P3Y", fixed-rate term
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


def run(output_path, limit=None):
    print("Fetching banking data holders from the CDR Register...")
    holders = get_banking_data_holders()
    if limit:
        holders = holders[:limit]
    print(f"Found {len(holders)} banking brands to query.")

    all_rows = []
    failures = []

    for holder in holders:
        brand = holder["brand_name"]
        base_uri = holder["base_uri"]
        try:
            products = get_home_loan_products(base_uri)
            for summary in products:
                try:
                    detail = get_product_detail(base_uri, summary["productId"])
                    all_rows.extend(normalize_product(brand, summary, detail))
                except Exception as e:
                    failures.append({"brand": brand, "product_id": summary.get("productId"), "error": str(e)})
            print(f"  {brand}: {len(products)} home loan products")
        except Exception as e:
            failures.append({"brand": brand, "error": str(e)})
            print(f"  {brand}: FAILED ({e})")

        time.sleep(POLITENESS_DELAY)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lender_count": len(holders) - len(set(f["brand"] for f in failures if "product_id" not in f)),
        "row_count": len(all_rows),
        "rows": all_rows,
        "failures": failures,
    }

    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nDone. {len(all_rows)} rate rows written to {output_path}")
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
