#!/usr/bin/env python3
"""Scrape historical draw results from The Lott's public results API.

Queries https://data.api.thelott.com/sales/vmax/web/data/lotto/results/search/daterange
one calendar month at a time (the API rejects wider windows) and writes the
raw draws to newline-delimited JSON, one file per product. Already-fetched
months are recorded in a manifest so the script can be re-run to pick up
where it left off, and draws already on disk are never duplicated.

Usage:
    python scrape_lotto_results.py
    python scrape_lotto_results.py --products OzLotto,Powerball,TattsLotto \
        --start 1980-01-01 --output-dir lotto_results

Known valid --products values (confirmed against the live API):
    OzLotto      - Oz Lotto (Tuesday)
    Powerball    - Powerball (Thursday)
    TattsLotto   - Saturday Lotto / Gold Lotto / X Lotto (Saturday)
Other product ids (e.g. SetForLife, Keno) are accepted by the API but this
particular endpoint returns no draws for them, so they're left out of the
default list.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

API_URL = "https://data.api.thelott.com/sales/vmax/web/data/lotto/results/search/daterange"
SYDNEY_TZ = ZoneInfo("Australia/Sydney")

# This exact header set (and, per Akamai's bot fingerprinting, roughly this
# order) is required - requests missing accept-language/priority/sec-ch-ua*
# get a 403 from Akamai even though the URL and body are unchanged.
HEADERS = {
    "accept": "*/*",
    "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
    "content-type": "application/json",
    "origin": "https://www.thelott.com",
    "priority": "u=1, i",
    "referer": "https://www.thelott.com/",
    "sec-ch-ua": '"Google Chrome";v="153", "Not_A Brand";v="8", "Chromium";v="153"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
    ),
}

DEFAULT_PRODUCTS = ["OzLotto", "Powerball", "TattsLotto"]
DEFAULT_COMPANY = "NSWLotteries"
DEFAULT_START = date(1979, 1, 1)


def month_chunks(start: date, end: date):
    """Yield (chunk_key, DateStart, DateEnd) for each calendar month, both
    bounds inclusive, expressed as UTC ISO8601 timestamps covering that
    month in Australia/Sydney local time (matching the API's own convention)."""
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        local_start = datetime(year, month, 1, 0, 0, 0, tzinfo=SYDNEY_TZ)
        if month == 12:
            next_year, next_month = year + 1, 1
        else:
            next_year, next_month = year, month + 1
        next_month_start_utc = datetime(next_year, next_month, 1, 0, 0, 0, tzinfo=SYDNEY_TZ).astimezone(timezone.utc)
        end_utc = next_month_start_utc - timedelta(seconds=1)

        date_start = local_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        date_end = end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        yield f"{year:04d}-{month:02d}", date_start, date_end

        year, month = next_year, next_month


@dataclass
class ProductState:
    product: str
    output_file: Path
    manifest_file: Path
    seen_draw_numbers: set = field(default_factory=set)
    completed_chunks: set = field(default_factory=set)

    @classmethod
    def load(cls, product: str, output_dir: Path) -> "ProductState":
        output_file = output_dir / f"{product}.jsonl"
        manifest_file = output_dir / ".manifest" / f"{product}.json"
        state = cls(product=product, output_file=output_file, manifest_file=manifest_file)

        if output_file.exists():
            with output_file.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    draw = json.loads(line)
                    state.seen_draw_numbers.add(draw.get("DrawNumber"))

        if manifest_file.exists():
            state.completed_chunks = set(json.loads(manifest_file.read_text(encoding="utf-8")))

        return state

    def mark_chunk_done(self, chunk_key: str) -> None:
        self.completed_chunks.add(chunk_key)
        self.manifest_file.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_file.write_text(json.dumps(sorted(self.completed_chunks)), encoding="utf-8")

    def append_draws(self, draws: list[dict]) -> int:
        new_draws = [d for d in draws if d.get("DrawNumber") not in self.seen_draw_numbers]
        if not new_draws:
            return 0
        self.output_file.parent.mkdir(parents=True, exist_ok=True)
        with self.output_file.open("a", encoding="utf-8") as f:
            for draw in new_draws:
                f.write(json.dumps(draw, sort_keys=True) + "\n")
                self.seen_draw_numbers.add(draw.get("DrawNumber"))
        return len(new_draws)


def fetch_chunk(
    session: requests.Session,
    product: str,
    company: str,
    date_start: str,
    date_end: str,
    max_retries: int,
    timeout: int,
) -> list[dict]:
    payload = {
        "DateStart": date_start,
        "DateEnd": date_end,
        "ProductFilter": [product],
        "CompanyFilter": [company],
    }

    backoff = 2.0
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.post(API_URL, headers=HEADERS, json=payload, timeout=timeout)
        except requests.RequestException as exc:
            if attempt == max_retries:
                raise
            print(f"    request error ({exc}); retrying in {backoff:.0f}s", file=sys.stderr)
            time.sleep(backoff)
            backoff *= 2
            continue

        data = None
        if "json" in resp.headers.get("Content-Type", ""):
            try:
                data = resp.json()
            except ValueError:
                data = None

        if data is not None and (resp.status_code == 200 or data.get("ErrorInfo")):
            if data.get("Success"):
                return data.get("Draws") or []
            error = data.get("ErrorInfo") or {}
            if error.get("ErrorNo") == 50010:
                raise ValueError(
                    f"Product {product!r} rejected by API: {error.get('DisplayMessage')}"
                )
            if attempt == max_retries:
                raise RuntimeError(f"API error for {product} {date_start}..{date_end}: {error}")
            print(f"    API error ({error}); retrying in {backoff:.0f}s", file=sys.stderr)
        elif resp.status_code in (403, 429) or resp.status_code >= 500:
            if attempt == max_retries:
                raise RuntimeError(
                    f"HTTP {resp.status_code} for {product} {date_start}..{date_end} "
                    f"after {max_retries} attempts"
                )
            retry_after = resp.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else backoff
            print(
                f"    HTTP {resp.status_code}; retrying in {wait:.0f}s "
                f"(attempt {attempt}/{max_retries})",
                file=sys.stderr,
            )
            time.sleep(wait)
            backoff *= 2
            continue
        else:
            resp.raise_for_status()

        time.sleep(backoff)
        backoff *= 2

    return []


def scrape_product(
    session: requests.Session,
    product: str,
    company: str,
    start: date,
    end: date,
    output_dir: Path,
    delay: float,
    max_retries: int,
    timeout: int,
) -> None:
    state = ProductState.load(product, output_dir)
    print(f"== {product} == ({len(state.seen_draw_numbers)} draws already saved)")

    for chunk_key, date_start, date_end in month_chunks(start, end):
        if chunk_key in state.completed_chunks:
            continue

        draws = fetch_chunk(session, product, company, date_start, date_end, max_retries, timeout)
        added = state.append_draws(draws)
        state.mark_chunk_done(chunk_key)

        if draws:
            print(f"  {chunk_key}: {len(draws)} draw(s), {added} new")

        time.sleep(delay)

    print(f"== {product} done: {len(state.seen_draw_numbers)} total draws in {state.output_file}")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--products",
        default=",".join(DEFAULT_PRODUCTS),
        help=f"Comma-separated ProductFilter values (default: {','.join(DEFAULT_PRODUCTS)})",
    )
    parser.add_argument("--company", default=DEFAULT_COMPANY, help=f"CompanyFilter value (default: {DEFAULT_COMPANY})")
    parser.add_argument("--start", default=DEFAULT_START.isoformat(), help="Earliest draw date, YYYY-MM-DD")
    parser.add_argument("--end", default=date.today().isoformat(), help="Latest draw date, YYYY-MM-DD (default: today)")
    parser.add_argument("--output-dir", default="lotto_results", help="Directory to write *.jsonl results into")
    parser.add_argument("--delay", type=float, default=0.4, help="Seconds to sleep between requests")
    parser.add_argument("--max-retries", type=int, default=5, help="Retries per request before giving up on a month")
    parser.add_argument("--timeout", type=int, default=20, help="Per-request timeout in seconds")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    products = [p.strip() for p in args.products.split(",") if p.strip()]
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    output_dir = Path(args.output_dir)

    session = requests.Session()
    for product in products:
        try:
            scrape_product(
                session,
                product,
                args.company,
                start,
                end,
                output_dir,
                args.delay,
                args.max_retries,
                args.timeout,
            )
        except ValueError as exc:
            print(f"skipping {product}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
