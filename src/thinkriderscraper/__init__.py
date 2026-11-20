"""AliExpress price scraper for the ThinkRider XXPRO.

Fetches the AliExpress search result page for a product query, parses the
server-rendered product cards plus the embedded search metrics JSON, and
reports the best (lowest) price found.

Usage:
    uv run thinkriderscraper                 # default query: ThinkRider XXPRO
    uv run thinkriderscraper "ThinkRider X7"
    uv run thinkriderscraper --sort orders
    uv run thinkriderscraper --max-results 15
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from curl_cffi import requests

SEARCH_URL = "https://www.aliexpress.com/w/wholesale-{query}.html"

# AliExpress serves prices in the currency chosen for your location (GeoIP).
# Change this cookie snippet if you want a different ship-to country/currency
# (e.g. "aep_usuc_f=site=glo&c_tp=USD&region=US&b_locale=en_US").
SHIP_TO_COOKIE = "aep_usuc_f=site=glo&c_tp=EUR&region=DE&b_locale=en_US"

IMPERSONATE = "chrome"  # curl_cffi sends a real Chrome TLS fingerprint


@dataclass
class Product:
    item_id: str
    title: str
    price: float
    currency: str
    rating: float | None
    orders: int
    url: str

    def price_str(self) -> str:
        return f"{self.currency} {self.price:,.2f}"


def fetch_search_page(query: str) -> str:
    url = SEARCH_URL.format(query=query.replace(" ", "+"))
    headers = {
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://www.aliexpress.com/",
    }
    resp = requests.get(
        url,
        impersonate=IMPERSONATE,
        headers=headers,
        cookies=_parse_cookie_string(SHIP_TO_COOKIE),
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Search page returned HTTP {resp.status_code}")
    return resp.text


def _parse_cookie_string(s: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in s.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


def _parse_embedded_json(html: str) -> dict | None:
    """Extract window._dida_config_._init_data_ = { data: {...} }."""
    anchor = html.find("_init_data_=")
    if anchor == -1:
        return None
    brace = html.find("{", html.find("data:", anchor))
    if brace == -1:
        return None
    data, _ = json.JSONDecoder().raw_decode(html, brace)
    return data


def _iter_product_metrics(obj):
    """Yield every dict in the embedded JSON that carries product card metrics."""
    if isinstance(obj, dict):
        if "salePriceAmount" in obj and obj.get("x_object_type") == "productV3":
            yield obj
        for value in obj.values():
            yield from _iter_product_metrics(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _iter_product_metrics(value)


_CARD_ANCHOR_RE = re.compile(
    r'<a class="[^"]*search-card-item[^"]*" href="([^"]+)"',
    re.DOTALL,
)
_TITLE_RE = re.compile(r'<h3 class="[^"]*titleText[^"]*">(.*?)</h3>', re.DOTALL)
_PDP_NPI_RE = re.compile(r"dis!([A-Z]{3})!([\d.]+)!([\d.]*)")


def _parse_cards(html: str) -> dict[str, dict]:
    """Map item id -> {title, url, currency, display price} from the HTML cards."""
    cards: dict[str, dict] = {}
    for anchor_match in _CARD_ANCHOR_RE.finditer(html):
        href = anchor_match.group(1)
        rest = html[anchor_match.end(): anchor_match.end() + 10000]

        title_match = _TITLE_RE.search(rest)
        title = ""
        if title_match:
            title = re.sub(r"<!--.*?-->", "", title_match.group(1))
            title = html_mod.unescape(title).strip()

        parsed = urlparse(href if href.startswith("http") else f"https:{href}")
        item_id = re.search(r"/item/(\d+)\.html", parsed.path)
        if not item_id:
            continue

        currency, org_price, sale_price = "", None, None
        for value in parse_qs(unquote(parsed.query)).get("pdp_npi", []):
            m = _PDP_NPI_RE.search(value)
            if m:
                currency = m.group(1)
                org_price = float(m.group(2))
                sale_price = float(m.group(3)) if m.group(3) else org_price

        cards[item_id.group(1)] = {
            "title": title,
            "url": f"https://www.aliexpress.com/item/{item_id.group(1)}.html",
            "currency": currency,
            "sale_price_npi": sale_price,
            "org_price_npi": org_price,
        }
    return cards


def _cents_from_metrics(metrics: dict) -> tuple[float | None, str]:
    """Pull (price, currency) from embedded card metrics. Prices are minor units."""
    price = metrics.get("salePriceAmount")
    currency = metrics.get("currency_code") or ""
    amount = None
    if price is not None:
        try:
            amount = float(price) / 100.0
        except (TypeError, ValueError):
            amount = None
    return amount, currency


def collect_products(query: str) -> list[Product]:
    page = fetch_search_page(query)
    data = _parse_embedded_json(page)
    if data is None:
        raise RuntimeError(
            "Could not find search result data. AliExpress may be serving an "
            "anti-bot page - try again."
        )

    html_cards = _parse_cards(page)
    seen: Counter[str] = Counter()
    products: list[Product] = []

    for metrics in _iter_product_metrics(data):
        item_id = str(metrics.get("_p_origin_prod") or metrics.get("x_object_id") or "")
        price, currency = _cents_from_metrics(metrics)
        card = html_cards.get(item_id, {})

        if price is None:
            price = card.get("sale_price_npi")
        if not currency:
            currency = card.get("currency") or _currency_from_formatted(
                metrics.get("formatted_price", "")
            )
        if price is None:
            continue

        title = card.get("title") or ""
        seen[item_id] += 1
        orders = _parse_orders(metrics.get("real_trade_count"))
        rating = _parse_rating(metrics.get("star_rating"))

        products.append(
            Product(
                item_id=item_id,
                title=title or "(unknown item)",
                price=price,
                currency=currency or "?",
                rating=rating,
                orders=orders,
                url=card.get("url") or f"https://www.aliexpress.com/item/{item_id}.html",
            )
        )

    # Drop duplicate cards (same product shown via SPU super link).
    deduped: dict[str, Product] = {}
    for product in products:
        if product.item_id not in deduped or product.price < deduped[product.item_id].price:
            deduped[product.item_id] = product
    return list(deduped.values())


_SYMBOLS = {"€": "EUR", "$": "USD", "US $": "USD", "£": "GBP"}


def _currency_from_formatted(formatted: str) -> str:
    for symbol, code in _SYMBOLS.items():
        if symbol in formatted:
            return code
    return ""


def _parse_rating(raw) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _parse_orders(raw) -> int:
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return 0


def is_relevant(title: str, query: str) -> bool:
    """True when every word of the query appears in the normalized title and the
    title does not look like an accessory (unless the query names that accessory)."""
    norm_title = re.sub(r"[^a-z0-9 ]+", " ", title.lower())
    norm_query = re.sub(r"[^a-z0-9 ]+", " ", query.lower()).split()
    if any(word not in norm_title for word in norm_query):
        return False
    accessories = {
        "freehub", "cassette", "wheel", "wheels", "brake", "brakes",
        "pedal", "pedals", "bearing", "bearings", "adapter", "replacement",
        "holder", "shelf", "monitor", "computer", "skin", "sleeve",
        "cover", "mount", "riser", "spare", "part", "parts",
    }
    return not any(word in norm_title for word in accessories - set(norm_query))


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ThinkRider XXpro Best Price Search</title>
<style>
  :root {{
    --bg: #0f1420; --panel: #171e2e; --border: #26314a;
    --text: #e8ecf4; --muted: #8b96ad; --accent: #ff7a1a; --good: #3dcc8e;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 2rem 1rem; background: var(--bg); color: var(--text);
    font: 15px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif;
  }}
  .wrap {{ max-width: 960px; margin: 0 auto; }}
  h1 {{ font-size: 1.5rem; margin: 0 0 .25rem; }}
  .meta {{ color: var(--muted); margin-bottom: 1.5rem; }}
  .best {{
    display: flex; flex-wrap: wrap; gap: .5rem 1.5rem; align-items: baseline;
    background: var(--panel); border: 1px solid var(--good); border-radius: 10px;
    padding: 1rem 1.25rem; margin-bottom: 1.5rem;
  }}
  .best .price {{ font-size: 1.9rem; font-weight: 700; color: var(--good); }}
  .best a {{ color: var(--accent); text-decoration: none; }}
  table {{ width: 100%; border-collapse: collapse; background: var(--panel);
           border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }}
  th, td {{ padding: .6rem .9rem; text-align: left; border-top: 1px solid var(--border); }}
  th {{ background: #1d2638; font-size: .8rem; text-transform: uppercase;
        letter-spacing: .05em; color: var(--muted); border-top: none; }}
  tr.bestrow td {{ background: rgba(61, 220, 142, .08); }}
  td.price, th.price {{ white-space: nowrap; font-weight: 600; }}
  td.title a {{ color: var(--text); text-decoration: none; }}
  td.title a:hover {{ color: var(--accent); }}
  td.num {{ text-align: right; white-space: nowrap; color: var(--muted); }}
  td.rank {{ color: var(--muted); width: 2.5rem; }}
  .tag {{ display: inline-block; background: var(--good); color: #06281a;
          font-size: .7rem; font-weight: 700; padding: .1rem .4rem;
          border-radius: .5rem; margin-left: .5rem; vertical-align: middle; }}
  .history {{ background: var(--panel); border: 1px solid var(--border);
              border-radius: 10px; padding: 1rem 1.25rem; margin-bottom: 1.5rem; }}
  .history h2 {{ font-size: .85rem; margin: 0 0 .75rem; color: var(--muted);
                 text-transform: uppercase; letter-spacing: .05em; }}
  .history svg {{ display: block; width: 100%; height: auto; }}
  .history .tick {{ fill: var(--muted); font-size: 11px;
                    font-family: ui-monospace, "SF Mono", Menlo, monospace; }}
  .tablewrap {{ overflow-x: auto; -webkit-overflow-scrolling: touch; }}
  @media (max-width: 640px) {{
    body {{ padding: 1.25rem .75rem; }}
    h1 {{ font-size: 1.2rem; }}
    .best {{ padding: .85rem 1rem; }}
    .best .price {{ font-size: 1.5rem; }}
    td, th {{ padding: .5rem .6rem; font-size: .85rem; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <h1>ThinkRider XXpro Best Price Search</h1>
  <p class="meta">Query <strong>{query}</strong> &middot; {count} results
     ({sort}-sorted) &middot; generated {generated}</p>
  <div class="best">
    <span>Best price</span>
    <span class="price">{best_price}</span>
    <span>on <a href="{best_url}" target="_blank" rel="noopener">{best_title}</a></span>
  </div>
  {history_section}
  <div class="tablewrap">
  <table>
    <thead><tr>
      <th>#</th><th>Product</th><th class="price">Price</th>
      <th class="num">Rating</th><th class="num">Orders</th>
    </tr></thead>
    <tbody id="rows"></tbody>
  </table>
  </div>
</div>
<script>
const RESULTS = {results_json};
const rows = document.getElementById("rows");
RESULTS.forEach((p, i) => {{
  const tr = document.createElement("tr");
  if (p.is_best) tr.className = "bestrow";
  const rating = p.rating ? p.rating.toFixed(1) : "n/a";
  tr.innerHTML = `
    <td class="rank">${{i + 1}}</td>
    <td class="title"><a href="${{p.url}}" target="_blank" rel="noopener">${{p.title}}</a>${{p.is_best ? '<span class="tag">BEST</span>' : ""}}</td>
    <td class="price">${{p.currency}} ${{p.price.toLocaleString(undefined, {{minimumFractionDigits: 2, maximumFractionDigits: 2}})}}</td>
    <td class="num">${{rating}}</td>
    <td class="num">${{p.orders.toLocaleString()}}</td>`;
  rows.appendChild(tr);
}});
</script>
</body>
</html>
"""


def _load_history(history_path: Path) -> list[dict]:
    if not history_path.exists():
        return []
    try:
        history = json.loads(history_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return history if isinstance(history, list) else []


def _render_history_svg(history: list[dict], currency: str) -> str:
    """Render the best-price history as a self-contained inline SVG line chart."""
    points = [
        (entry["timestamp"], float(entry["best_price"]))
        for entry in history
        if isinstance(entry, dict)
        and isinstance(entry.get("best_price"), (int, float))
        and isinstance(entry.get("timestamp"), str)
    ][-_CHART_MAX_POINTS:]
    if len(points) < 2:
        return ""

    prices = [p for _, p in points]
    lo, hi = min(prices), max(prices)
    span = (hi - lo) or 1.0  # flat history: line sits at the bottom of the chart
    n = len(prices)
    pad, w, hgt = _SVG_PAD, _SVG_W, _SVG_H
    coords = [
        (
            pad + (w - 2 * pad) * (i / (n - 1)),
            hgt - pad - (hgt - 2 * pad) * ((price - lo) / span),
        )
        for i, price in enumerate(prices)
    ]
    polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    min_i, max_i = prices.index(lo), prices.index(hi)
    min_x, min_y = coords[min_i]

    return (
        f'<svg viewBox="0 0 {w} {hgt}" preserveAspectRatio="xMidYMid meet" '
        f'role="img" aria-label="Best price history">\n'
        f'<text x="{pad}" y="14" class="tick">{hi:.2f} {currency}</text>\n'
        f'<text x="{pad}" y="{hgt - 2}" class="tick">{lo:.2f} {currency}</text>\n'
        f'<text x="{w - pad}" y="14" text-anchor="end" '
        f'class="tick">{points[0][0][:10]}</text>\n'
        f'<text x="{w - pad}" y="{hgt - 2}" text-anchor="end" '
        f'class="tick">{points[-1][0][:10]}</text>\n'
        f'<polyline points="{polyline}" fill="none" '
        f'stroke="var(--accent)" stroke-width="2" stroke-linejoin="round"/>\n'
        f'<circle cx="{min_x:.1f}" cy="{min_y:.1f}" r="5" fill="var(--good)">\n'
        f'<title>{lo:.2f} {currency} (lowest)</title>\n'
        f"</circle>\n"
        f"</svg>"
    )


def _render_history_section(history: list[dict], currency: str) -> str:
    """Full history card, or an empty string while fewer than 2 data points exist."""
    if len(history) < 2:
        return ""
    chart = _render_history_svg(history, currency)
    if not chart:
        return ""
    shown = min(len(history), _CHART_MAX_POINTS)
    return (
        '<div class="history">\n'
        f'<h2>Best price — last {shown} runs</h2>\n'
        f"{chart}\n"
        "</div>\n"
    )


def write_results_html(
    products: list[Product],
    query: str,
    sort: str,
    out_path: Path | None = None,
    history_path: Path | None = None,
) -> Path:
    """Write a self-contained index.html displaying the results.

    Defaults to <project root>/index.html regardless of the current
    working directory. Renders the best-price history chart from
    <project root>/results_history.json when it has at least two entries.
    """
    out = out_path or Path(__file__).resolve().parents[2] / "index.html"
    best = min(products, key=lambda p: p.price)
    payload = [
        {
            "title": p.title,
            "price": p.price,
            "currency": p.currency,
            "rating": p.rating,
            "orders": p.orders,
            "url": p.url,
            "is_best": p.item_id == best.item_id,
        }
        for p in products
    ]
    history = _load_history(
        history_path or Path(__file__).resolve().parents[2] / "results_history.json"
    )
    history_section = _render_history_section(history, best.currency)
    html = _HTML_TEMPLATE.format(
        query=html_mod.escape(query),
        count=len(products),
        sort=sort,
        generated=datetime.now().strftime("%Y-%m-%d %H:%M"),
        best_price=html_mod.escape(best.price_str()),
        best_url=html_mod.escape(best.url),
        best_title=html_mod.escape(best.title),
        history_section=history_section,
        results_json=json.dumps(payload, ensure_ascii=False).replace("</", "<\\/"),
    )
    out.write_text(html, encoding="utf-8")
    return out


# Keep the history file bounded even after months of 30-minute runs.
HISTORY_MAX_ENTRIES = 20_000

# Chart geometry (viewBox units; the SVG itself scales to container width).
_SVG_W, _SVG_H, _SVG_PAD = 720, 220, 12
_CHART_MAX_POINTS = 200


def update_history(
    products: list[Product], query: str, history_path: Path | None = None
) -> Path:
    """Append the best price found this run to the JSON history file.

    Defaults to <project root>/results_history.json regardless of the
    current working directory.
    """
    path = history_path or Path(__file__).resolve().parents[2] / "results_history.json"
    history = _load_history(path)

    best = min(products, key=lambda p: p.price)
    history.append(
        {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "query": query,
            "best_price": best.price,
            "currency": best.currency,
            "title": best.title,
            "url": best.url,
            "item_id": best.item_id,
            "results": len(products),
        }
    )
    del history[:-HISTORY_MAX_ENTRIES]

    path.write_text(
        json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Find the best AliExpress price.")
    parser.add_argument("query", nargs="?", default="ThinkRider XXPRO")
    parser.add_argument("--sort", choices=("price", "orders", "rating"), default="price")
    parser.add_argument("--max-results", type=int, default=10)
    parser.add_argument(
        "--no-filter", action="store_true",
        help="show all results, not just ones matching every query word",
    )
    args = parser.parse_args(argv)

    try:
        products = collect_products(args.query)
    except Exception as exc:  # noqa: BLE001 - one clear error for the CLI
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)

    if not products:
        print("No results found.")
        return

    if not args.no_filter:
        relevant = [p for p in products if is_relevant(p.title, args.query)]
        if relevant:
            products = relevant
        else:
            print("warning: no title matched every query word; showing all results\n")

    key = {
        "price": lambda p: p.price,
        "orders": lambda p: -p.orders,
        "rating": lambda p: -float(p.rating or 0),
    }[args.sort]
    products.sort(key=key)
    shown = products[: args.max_results]

    print(f"Query: {args.query!r} - {len(products)} results ({args.sort}-sorted)\n")
    for rank, product in enumerate(shown, 1):
        rating = f"{product.rating:.1f}" if product.rating else "n/a"
        print(f"#{rank} {product.price_str():>14}  rating {rating:>4}  {product.orders:>6} orders")
        print(f"    {product.title}")
        print(f"    {product.url}\n")

    best = min(products, key=lambda p: p.price)
    print("=" * 60)
    print(f"Best price: {best.price_str()} - {best.title}")
    print(best.url)

    out = write_results_html(shown, args.query, args.sort)
    print(f"\nWrote {out.resolve()}")

    hist = update_history(products, args.query)
    print(f"Updated {hist.resolve()} ({len(products)} products, best {best.price_str()})")


if __name__ == "__main__":
    main()
