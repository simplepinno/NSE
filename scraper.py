"""NSE snapshot scraper.

Opens nse.co.ke, clicks through every tab and dropdown option, and saves every
visible table as CSV under data/YYYY-MM-DD/HHMM/. Also logs the background
(JSON/AJAX) requests the page makes, so a direct data feed can be found later.
"""
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

HOME = "https://www.nse.co.ke/"
STATS = "https://www.nse.co.ke/dataservices/market-statistics/"
STATS_TABS = [
    "Market Statistics Summary",
    "Equity Statistics",
    "Bonds Statistics",
    "Derivatives Statistics",
    "USP Statistics",
]
HOME_TABS = ["Gainers", "Losers", "Movers"]

NOW = datetime.now(ZoneInfo("Africa/Nairobi"))
OUT = Path("data") / NOW.strftime("%Y-%m-%d") / NOW.strftime("%H%M")
OUT.mkdir(parents=True, exist_ok=True)

manifest = {"scraped_at": NOW.isoformat(), "market_status": None, "entries": [], "errors": []}
net_log = []

JS_TABLES = r"""
() => [...document.querySelectorAll('table')]
  .filter(t => t.offsetParent !== null)
  .map(t => [...t.querySelectorAll('tr')].map(tr =>
      [...tr.querySelectorAll('th,td')].map(c => c.innerText.trim().replace(/\s+/g, ' '))))
"""


def slug(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:80] or "x"


def as_of(page):
    m = re.search(r"Statistics\s+as\s+of\s+([0-9A-Za-z\-]+)", page.inner_text("body"), re.I)
    return m.group(1) if m else ""


def visible_tables(page):
    return [t for t in page.evaluate(JS_TABLES) if t]


def save(section, option, tables, stamp):
    """Write each table with data rows to CSV; record empties in the manifest."""
    saved = 0
    for i, rows in enumerate(tables):
        entry = {"section": section, "option": option, "table": i, "rows": len(rows) - 1, "as_of": stamp}
        if len(rows) <= 1:
            entry["note"] = "no data"
            manifest["entries"].append(entry)
            continue
        name = f"{slug(section)}__{slug(option)}__{i}.csv"
        with open(OUT / name, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["scraped_at", "as_of", "option"] + rows[0])
            for r in rows[1:]:
                w.writerow([NOW.isoformat(), stamp, option] + r)
        entry["file"] = name
        manifest["entries"].append(entry)
        saved += 1
    return saved


def accept_cookies(page):
    try:
        page.get_by_text("Accept All", exact=True).first.click(timeout=3000)
    except Exception:
        pass


def settle(page, ms=1200):
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    page.wait_for_timeout(ms)


def scrape_home(page):
    page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
    accept_cookies(page)
    settle(page, 2000)
    body = page.inner_text("body")
    m = re.search(r"Market Status:\s*(\w+).*?Current Trading Date\s*-\s*([\d/]+)", body, re.S)
    if m:
        manifest["market_status"] = {"status": m.group(1), "trading_date": m.group(2)}
    for tab in HOME_TABS:
        try:
            el = page.get_by_text(tab, exact=True).first
            el.scroll_into_view_if_needed(timeout=8000)
            el.click(timeout=8000, force=True)
            settle(page, 800)
            tables = [t for t in visible_tables(page) if "Change" in json.dumps(t)]
            save("home", tab, tables, as_of(page))
        except Exception as e:
            manifest["errors"].append(f"home/{tab}: {e}")
            page.screenshot(path=str(OUT / f"error_home_{slug(tab)}.png"))


def scrape_stats(page):
    page.goto(STATS, wait_until="domcontentloaded", timeout=60000)
    accept_cookies(page)
    settle(page, 2000)
    for tab in STATS_TABS:
        try:
            page.get_by_text(tab, exact=True).first.click(timeout=8000)
            settle(page, 800)
            if page.locator("select:visible").count() == 0:
                save(tab, "all", visible_tables(page), as_of(page))
                continue
            options = [
                o.strip()
                for o in page.locator("select:visible").first.locator("option").all_inner_texts()
                if o.strip() and not o.strip().lower().startswith("select")
            ]
            for opt in options:
                try:
                    page.locator("select:visible").first.select_option(label=opt)
                    settle(page)
                    save(tab, opt, visible_tables(page), as_of(page))
                except Exception as e:
                    manifest["errors"].append(f"{tab}/{opt}: {e}")
        except Exception as e:
            manifest["errors"].append(f"{tab}: {e}")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(viewport={"width": 1280, "height": 1800}).new_page()

        def on_response(resp):
            try:
                ctype = resp.headers.get("content-type", "")
                if "json" in ctype or "admin-ajax" in resp.url or "wp-json" in resp.url:
                    net_log.append({
                        "url": resp.url,
                        "method": resp.request.method,
                        "post_data": resp.request.post_data,
                        "status": resp.status,
                        "body": resp.text()[:20000],
                    })
            except Exception:
                pass

        page.on("response", on_response)
        for fn in (scrape_home, scrape_stats):
            try:
                fn(page)
            except Exception as e:
                manifest["errors"].append(f"{fn.__name__}: {e}")
                page.screenshot(path=str(OUT / f"error_{fn.__name__}.png"))
        browser.close()

    (OUT / "network_log.json").write_text(json.dumps(net_log, indent=1), encoding="utf-8")
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    summary = [
        f"{n['method']} {n['status']} {n['url']} post={str(n['post_data'])[:200]} len={len(n['body'])}"
        for n in net_log
        if "google" not in n["url"] and "cookie" not in n["url"].lower()
    ]
    (OUT / "network_summary.txt").write_text("\n".join(summary), encoding="utf-8")
    print("=== ERRORS ===")
    for err in manifest["errors"]:
        print(err[:300])
    print("=== NETWORK (first 25) ===")
    for line in summary[:25]:
        print(line)
    files = sum(1 for e in manifest["entries"] if "file" in e)
    print(f"Saved {files} tables, {len(manifest['errors'])} errors, {len(net_log)} network calls logged")
    sys.exit(0 if files else 1)


if __name__ == "__main__":
    main()
