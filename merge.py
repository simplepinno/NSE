"""Build merged analysis files from the per-run NSE snapshots.

Reads run folders data/YYYY-MM-DD/HHMM/ that have not been merged yet and updates data/merged/*.csv.

Hourly files: one row per run, every run kept
    ticker, gainers, losers, movers
Daily files: one block per NSE "as_of" date + table; the newest scrape of a block replaces the old
    one as a whole (bond tabs list every trade separately, so rows are never matched one by one)
    equity_sectors, bonds, derivatives, market_summary, usp_equity, usp_bonds

Left out on purpose: the Acorn / Centum / Chase bond-note tables (NSE publishes them with shifted
columns; the clean bond tabs already hold that data).

Usage:
    python merge.py             merge any run folders not merged yet
    python merge.py --rebuild   delete data/merged/ and rebuild it from every run folder
"""
import csv
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

DATA = Path("data")
MERGED = DATA / "merged"
STATE = MERGED / "_merged_runs.txt"
MAP = DATA / "ticker_isin_map.csv"  # optional: code,isin,... (upload it to data/ to turn the join on)

DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RUN_RE = re.compile(r"^\d{4}$")
SKIP_BONDS = ("acorn_green", "centum_real_estate", "chase_bank")
NUM = re.compile(r"^-?(\d{1,3}(,\d{3})+|\d*)(\.\d+)?$")

HOURLY = {
    "ticker": "api_v1_ticker.csv",
    "gainers": "api_data_gainers.csv",
    "losers": "api_data_losers.csv",
    "movers": "api_data_movers.csv",
}
# name: (file pattern, columns that identify one table block within a date)
DAILY = {
    "equity_sectors": ("equity_statistics__*.csv", ["as_of", "option"]),
    "bonds": ("bonds_statistics__*.csv", ["as_of", "option"]),
    "derivatives": ("derivatives_statistics__*.csv", ["as_of", "option"]),
    "market_summary": ("market_statistics_summary__*.csv", ["as_of"]),
    "usp_equity": ("usp_statistics__all__0.csv", ["as_of"]),
    "usp_bonds": ("usp_statistics__all__1.csv", ["as_of"]),
}


def clean(v):
    """Trim; turn '1,234.50' / '.99' into plain numbers. Dates, codes and text are left alone."""
    v = (v or "").strip()
    if v and NUM.match(v) and any(c.isdigit() for c in v):
        v = v.replace(",", "")
        if v.startswith("."):
            v = "0" + v
        elif v.startswith("-."):
            v = "-0" + v[1:]
    return v


def read_rows(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [{k: clean(v) for k, v in r.items() if k is not None} for r in csv.DictReader(f)]


def load_map():
    """code -> isin, and isin -> code, from the mapping table (empty if the file is not there)."""
    code_isin, isin_code = {}, {}
    if MAP.exists():
        for r in read_rows(MAP):
            if r.get("code") and r.get("isin"):
                code_isin[r["code"]] = r["isin"]
                isin_code[r["isin"]] = r["code"]
    return code_isin, isin_code


def write_rows(path, rows, fields=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        fields = list(dict.fromkeys(k for r in rows for k in r))
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, restval="")
        w.writeheader()
        w.writerows(rows)


def append_rows(path, rows):
    """Append rows; if new columns appear, rewrite the file with the wider header."""
    if not rows:
        return
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            header = next(csv.reader(f), [])
        if all(k in header for r in rows for k in r):
            with open(path, "a", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=header, restval="").writerows(rows)
            return
        rows = read_rows(path) + rows
    write_rows(path, rows)


def as_of_date(s):
    try:
        return datetime.strptime(s, "%d-%b-%Y")
    except Exception:
        return datetime.min


def merge_hourly(run, code_isin):
    for name, fname in HOURLY.items():
        src = run / fname
        if not src.exists():
            continue
        rows = read_rows(src)
        if name == "ticker" and code_isin:
            seen = set()
            for r in rows:
                code = r.get("issuer", "")
                # first listing of a code is the ordinary share; repeats (e.g. KPLC preference
                # shares) have no ISIN in the sector tables, so they stay blank
                r["isin"] = code_isin.get(code, "") if code not in seen else ""
                seen.add(code)
        append_rows(MERGED / f"{name}.csv", rows)


def merge_daily(run, isin_code):
    def same(a, b):  # compare rows ignoring when they were scraped
        strip = lambda rows: [{k: v for k, v in r.items() if k != "scraped_at"} for r in rows]
        return strip(a) == strip(b)

    for name, (pattern, block) in DAILY.items():
        files = sorted(run.glob(pattern))
        if name == "bonds":
            files = [f for f in files if not any(s in f.name for s in SKIP_BONDS)]
        new = []
        for f in files:
            new += read_rows(f)
        if not new:
            continue
        if name == "equity_sectors" and isin_code:
            for r in new:
                r["code"] = isin_code.get(r.get("ISIN Code", ""), "")
        bk = lambda r: tuple(r.get(k, "") for k in block)
        newblocks = {}
        for r in new:
            newblocks.setdefault(bk(r), []).append(r)
        dest = MERGED / f"{name}.csv"
        blocks, old_fields = {}, []
        if dest.exists():
            old = read_rows(dest)
            old_fields = list(old[0].keys()) if old else []
            for r in old:
                blocks.setdefault(bk(r), []).append(r)
        fields = list(dict.fromkeys(old_fields + [k for r in new for k in r]))
        if fields == old_fields and all(k in blocks and same(blocks[k], v) for k, v in newblocks.items()):
            continue  # nothing new for these dates
        blocks.update(newblocks)
        rows = [r for k in sorted(blocks, key=lambda k: (as_of_date(k[0]), k)) for r in blocks[k]]
        write_rows(dest, rows, fields)


def run_folders():
    out = []
    for day in sorted(p for p in DATA.iterdir() if p.is_dir() and DAY_RE.match(p.name)):
        out += [r for r in sorted(day.iterdir()) if r.is_dir() and RUN_RE.match(r.name)]
    return out


def main():
    if "--rebuild" in sys.argv and MERGED.exists():
        shutil.rmtree(MERGED)
    MERGED.mkdir(parents=True, exist_ok=True)
    done = set(STATE.read_text().split()) if STATE.exists() else set()
    code_isin, isin_code = load_map()
    todo = [r for r in run_folders() if f"{r.parent.name}/{r.name}" not in done]
    ok = 0
    for run in todo:
        tag = f"{run.parent.name}/{run.name}"
        try:
            merge_hourly(run, code_isin)
            merge_daily(run, isin_code)
        except Exception as e:  # never stop the bot over the merge
            print(f"merge {tag}: {e}")
            continue
        with open(STATE, "a", encoding="utf-8") as f:
            f.write(tag + "\n")
        ok += 1
    print(f"merge: {ok} run(s) merged, {len(todo) - ok} failed, map={'on' if code_isin else 'off (data/ticker_isin_map.csv not found)'}")


if __name__ == "__main__":
    main()
