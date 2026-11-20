# ThinkRider Scraper

Find the best AliExpress price for a product (default: **ThinkRider XXPRO**).

Runs entirely through [uv](https://docs.astral.sh/uv/) — no system Python setup needed.

## Usage

```sh
uv run python src/thinkriderscraper/__init__.py                 # default query: ThinkRider XXPRO
uv run python src/thinkriderscraper/__init__.py "ThinkRider X7" # any other product
uv run python src/thinkriderscraper/__init__.py --sort orders   # sort by units sold instead of price
uv run python src/thinkriderscraper/__init__.py --max-results 15 # show more results
uv run python src/thinkriderscraper/__init__.py --no-filter     # show everything AliExpress returned
```

The output lists matching products sorted by the chosen key and prints the
best (lowest) price at the end.

Every run also writes a self-contained `index.html` (results embedded, styled
table, best price highlighted) — just open it in a browser, no server needed.
Each run also appends the best price to `results_history.json`.

## Automated runs (GitHub Actions)

`.github/workflows/scrape.yml` runs the scraper every 30 minutes on GitHub
Actions, commits the updated `index.html` + `results_history.json`, and
publishes `index.html` to GitHub Pages. To enable it:

1. Push this repo to GitHub (a private repo works fine).
2. Only if the workflow fails with a permissions error: Settings -> Actions ->
   General -> Workflow permissions -> "Read and write permissions".
3. To serve the HTML report via Pages: Settings -> Pages -> Source ->
   "GitHub Actions". The report then lives at
   `https://<user>.github.io/<repo>/`.

Notes: scheduled runs are on UTC and can be delayed a few minutes under
GitHub's load; they don't fire while the repo is archived, and GitHub may
skip runs during incidents. AliExpress is sometimes stricter with
datacenter IPs than with residential ones — if runs start failing with
anti-bot pages, slow the schedule down first.

## Notes

- AliExpress shows prices in the currency tied to your location (GeoIP), here
  forced to EUR for Germany via the `aep_usuc_f` cookie. Edit `SHIP_TO_COOKIE`
  in `src/thinkriderscraper/__init__.py` to change ship-to country / currency
  (e.g. `c_tp=USD&region=US`).
- The scraper impersonates Chrome via `curl_cffi` because AliExpress rejects
  plain HTTP clients.
- "Best price" excludes obvious accessories (freehubs, cassettes, spare parts,
  ...) unless your query explicitly names one. Use `--no-filter` to see all.
