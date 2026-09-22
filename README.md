# ozlot

Scrapes historical draw results from [The Lott](https://www.thelott.com)'s
public results API.

## Usage

```
pip install -r requirements.txt
python scrape_lotto_results.py
```

By default this fetches OzLotto, Powerball and TattsLotto (Saturday Lotto /
Gold Lotto / X Lotto) draws from 1979 to today, one calendar month at a time,
and writes each product's draws to `lotto_results/<Product>.jsonl` (one JSON
draw object per line, matching the API's own draw schema).

The script is safe to re-run: a manifest under `lotto_results/.manifest/`
records which months have already been fully fetched, and draws already on
disk are never duplicated, so an interrupted run can just be started again.
The current (still in-progress) month is deliberately never marked complete,
so re-running keeps picking up new draws as they're published.

## Keeping results up to date

`.github/workflows/update-lotto-results.yml` runs the scraper daily
(`workflow_dispatch` also works for a manual run), commits any new draws to
`lotto_results/`, and pushes. Because the manifest is committed too, each
run only re-fetches the current month per product rather than the whole
history.

### Options

```
python scrape_lotto_results.py \
  --products OzLotto,Powerball,TattsLotto \
  --company NSWLotteries \
  --start 1979-01-01 \
  --end 2026-09-22 \
  --output-dir lotto_results \
  --delay 0.4
```

- `--products` - comma-separated `ProductFilter` values. Confirmed working:
  `OzLotto`, `Powerball`, `TattsLotto`. (`SetForLife` and `Keno` are accepted
  by the API but this endpoint returns no draws for them; Monday & Wednesday
  Lotto's product id wasn't identified.)
- `--company` - `CompanyFilter` value; only affects which state's per-division
  dividend amounts are attached to each draw, not which draws are returned.
- `--start` / `--end` - date range to cover (`YYYY-MM-DD`).
- `--delay` - seconds between requests, to stay polite to the API.

## Notes

The API sits behind Akamai bot detection: requests must carry a realistic
full browser header set (`accept-language`, `priority`, `sec-ch-ua*`,
`sec-fetch-*`, `user-agent`) or they get a 403 with no useful body. The
script hardcodes such a header set; if it starts getting blocked, capturing
a fresh set of headers from a real browser request (as in the original
`curl` this script was built from) and updating `HEADERS` should fix it.
