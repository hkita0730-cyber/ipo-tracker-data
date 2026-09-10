#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
IPO Tracker FAST v5

Design goals
------------
1) IPO search window: most recent 3 years
2) Price-history retention: up to 4 years
3) Rebuild master metadata every run, so bad names such as
   "... 代表者インタビュー" do not survive incremental updates
4) Use BOTH JPX and 庶民のIPO:
   - JPX preferred for official company name / listing date / market
   - 庶民のIPO supplies rating / public price / initial price and also acts
     as a fallback IPO master if JPX HTML parsing is temporarily incomplete
5) yfinance price download is batched
6) existing docs/latest.json is preserved until the very end; output is replaced atomically
7) output remains a JSON ARRAY for compatibility with the current app
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import yfinance as yf
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "latest.json"
OUT.parent.mkdir(parents=True, exist_ok=True)

SCHEMA_VERSION = 5

IPO_DISCOVERY_DAYS = 1095       # 3 years
HISTORY_RETENTION_DAYS = 1460   # 4 years
RECENT_REFRESH_DAYS = 120
FINANCIAL_REFRESH_DAYS = 7

MARKETS = ("プライム", "スタンダード", "グロース")

JPX_URLS = [
    "https://www.jpx.co.jp/listing/stocks/new/index.html",
    "https://www.jpx.co.jp/listing/stocks/new/00-archives-01.html",  # 2025
    "https://www.jpx.co.jp/listing/stocks/new/00-archives-02.html",  # 2024
    "https://www.jpx.co.jp/listing/stocks/new/00-archives-03.html",  # 2023
]

IPO_KABU_URLS = {
    2026: "https://ipokabu.net/ipo/list2026",
    2025: "https://ipokabu.net/ipo/list2025",
    2024: "https://ipokabu.net/ipo/list2024",
    2023: "https://ipokabu.net/ipo/list2023",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
}

FULL_DATE_RE = re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})")
MD_RE = re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)")
CODE_RE = re.compile(r"(?<![0-9A-Z])(\d{4}[A-Z]?)(?![0-9A-Z])")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    s = str(value).replace("\u3000", " ")
    return re.sub(r"\s+", " ", s).strip()


def normalize_company_name(value: Any) -> str:
    s = clean_text(value)
    # These labels are links on JPX and must never become part of the company name.
    for label in (
        "代表者インタビュー",
        "創業者インタビュー",
        "社長インタビュー",
        "経営者インタビュー",
    ):
        s = s.replace(label, "")
    s = re.sub(r"\s+", " ", s).strip(" |　")
    return s


def extract_code(value: Any) -> str | None:
    s = clean_text(value).upper().replace(" ", "")
    # pandas/HTML may turn an old numeric code into "5892.0"
    mfloat = re.fullmatch(r"(\d{4})\.0", s)
    if mfloat:
        return mfloat.group(1)
    m = CODE_RE.search(s)
    return m.group(1) if m else None


def extract_full_date(value: Any) -> str | None:
    m = FULL_DATE_RE.search(clean_text(value))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


def extract_month_day(value: Any, year: int) -> str | None:
    m = MD_RE.search(clean_text(value))
    if not m:
        return None
    try:
        return date(year, int(m.group(1)), int(m.group(2))).isoformat()
    except ValueError:
        return None


def parse_yen(value: Any) -> int | None:
    s = clean_text(value)
    if not s or s in ("-", "—"):
        return None
    m = re.search(r"(-?[\d,]+(?:\.\d+)?)\s*円", s)
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
        return int(round(v)) if v > 0 else None
    except Exception:
        return None


def normalize_rating(value: Any) -> str | None:
    s = clean_text(value).upper()
    s = s.translate(str.maketrans("ＳＡＢＣＤＥ", "SABCDE"))
    # Prefer a stand-alone rating glyph.
    m = re.search(r"(?<![A-Z])([SABCDE])(?![A-Z])", s)
    return m.group(1) if m else None


def fetch_html(url: str, timeout: int = 40) -> str:
    last = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            if r.apparent_encoding:
                r.encoding = r.apparent_encoding
            return r.text
        except Exception as e:
            last = e
            if attempt < 2:
                time.sleep(2 + attempt * 2)
    raise RuntimeError(f"fetch failed: {url}: {last}")


# ---------------------------------------------------------------------------
# Generic HTML table expander
# ---------------------------------------------------------------------------
def expand_table(table) -> list[list[str]]:
    """
    Expand rowspan/colspan into a rectangular logical grid.

    JPX and 庶民のIPO both use multi-row tables.  This avoids relying on
    physical <tr> layout and is the key fix for the "only 50 IPOs" problem.
    """
    rows: list[dict[int, str]] = []
    future: dict[tuple[int, int], str] = {}

    trs = table.find_all("tr")
    for r_idx, tr in enumerate(trs):
        row: dict[int, str] = {}

        # Fill values inherited from rowspans.
        inherited_cols = sorted(c for (rr, c) in future if rr == r_idx)
        for c in inherited_cols:
            row[c] = future[(r_idx, c)]

        col = 0
        for cell in tr.find_all(["th", "td"], recursive=False):
            while col in row:
                col += 1

            txt = clean_text(cell.get_text(" ", strip=True))
            try:
                colspan = max(1, int(cell.get("colspan", 1)))
            except Exception:
                colspan = 1
            try:
                rowspan = max(1, int(cell.get("rowspan", 1)))
            except Exception:
                rowspan = 1

            for dc in range(colspan):
                c = col + dc
                row[c] = txt
                if rowspan > 1:
                    for rr in range(r_idx + 1, r_idx + rowspan):
                        future[(rr, c)] = txt
            col += colspan

        rows.append(row)

    max_col = 0
    for row in rows:
        if row:
            max_col = max(max_col, max(row))
    return [[row.get(c, "") for c in range(max_col + 1)] for row in rows]


def header_labels(grid: list[list[str]], first_data_row: int) -> list[str]:
    if not grid:
        return []
    width = max(len(r) for r in grid)
    labels = []
    # Usually 2 header rows, but allow all rows before the first data row.
    start = max(0, first_data_row - 4)
    for c in range(width):
        parts = []
        for r in range(start, first_data_row):
            if c >= len(grid[r]):
                continue
            t = clean_text(grid[r][c])
            if not t or t.startswith("---"):
                continue
            if t not in parts:
                parts.append(t)
        labels.append(" / ".join(parts))
    return labels


def find_label_col(labels: list[str], must: tuple[str, ...], reject: tuple[str, ...] = ()) -> int | None:
    for i, lab in enumerate(labels):
        if all(x in lab for x in must) and not any(x in lab for x in reject):
            return i
    return None


# ---------------------------------------------------------------------------
# JPX
# ---------------------------------------------------------------------------
def parse_jpx_page(html: str, source_url: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    found: dict[tuple[str, str], dict[str, Any]] = {}

    for table in soup.find_all("table"):
        grid = expand_table(table)
        if not grid:
            continue

        # Ignore unrelated tables.
        joined_top = " ".join(" ".join(r) for r in grid[:4])
        if "上場日" not in joined_top or "会社名" not in joined_top or "コード" not in joined_top:
            continue

        for row in grid:
            listed = next((extract_full_date(v) for v in row if extract_full_date(v)), None)
            if not listed:
                continue

            code_positions = [(i, extract_code(v)) for i, v in enumerate(row)]
            code_positions = [(i, c) for i, c in code_positions if c]
            if not code_positions:
                continue
            code_i, code4 = code_positions[0]

            market = next((m for m in MARKETS if any(m in clean_text(v) for v in row)), "")
            # The expanded second physical row contains the inherited date/name/code
            # together with market; only accept that complete logical row.
            if not market:
                continue

            raw_name = ""
            # Company is normally the nearest meaningful cell before code.
            for v in reversed(row[:code_i]):
                t = clean_text(v)
                if not t:
                    continue
                if extract_full_date(t):
                    continue
                if any(m in t for m in MARKETS):
                    continue
                if extract_code(t):
                    continue
                if t in ("会社名", "上場日", "上場承認日", "詳細", "確認書", "会社概要"):
                    continue
                raw_name = t
                break

            if not raw_name:
                continue

            # JPX official note: "*" after company name denotes a technical listing.
            if "*" in raw_name:
                continue

            name = normalize_company_name(raw_name)
            if not name:
                continue

            found[(code4, listed)] = {
                "code4": code4,
                "name": name,
                "market": market,
                "listedDate": listed,
                "jpxSourceUrl": source_url,
            }

    return list(found.values())


def parse_jpx() -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    downloaded = 0

    for url in JPX_URLS:
        try:
            html = fetch_html(url)
            downloaded += 1
            rows = parse_jpx_page(html, url)
            print(f"[JPX] {url.split('/')[-1]} parsed={len(rows)}", flush=True)
            for x in rows:
                # Prefer the newest occurrence if the same code appears more than once.
                old = merged.get(x["code4"])
                if not old or x["listedDate"] > old["listedDate"]:
                    merged[x["code4"]] = x
        except Exception as e:
            print(f"[WARN] JPX {url}: {e}", flush=True)

    if downloaded == 0:
        raise RuntimeError("All JPX pages failed to download.")

    cutoff = (date.today() - timedelta(days=IPO_DISCOVERY_DAYS)).isoformat()
    today_s = date.today().isoformat()
    merged = {
        c: x for c, x in merged.items()
        if cutoff <= x.get("listedDate", "") <= today_s
        and x.get("market") in MARKETS
    }

    bad = [x for x in merged.values() if "インタビュー" in x.get("name", "")]
    if bad:
        raise RuntimeError(f"JPX name sanitization failed: {bad[:3]}")

    print(f"[JPX] usable 3-year records={len(merged)}", flush=True)
    return merged


# ---------------------------------------------------------------------------
# 庶民のIPO
# ---------------------------------------------------------------------------
def parse_ipokabu_year(html: str, year: int, source_url: str) -> dict[str, dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    result: dict[str, dict[str, Any]] = {}

    for table in soup.find_all("table"):
        grid = expand_table(table)
        if not grid:
            continue

        joined = " ".join(" ".join(r) for r in grid[:4])
        if "公開価格" not in joined or ("証券コード" not in joined and "銘柄" not in joined):
            continue

        first_data = None
        for i, row in enumerate(grid):
            if any(extract_code(v) for v in row) and any(extract_month_day(v, year) for v in row):
                first_data = i
                break
        if first_data is None:
            continue

        labels = header_labels(grid, first_data)

        public_i = find_label_col(labels, ("公開価格",))
        initial_i = find_label_col(labels, ("初値",), ("損益", "騰落"))
        name_i = find_label_col(labels, ("銘柄",))
        market_i = find_label_col(labels, ("市場",))
        rating_i = find_label_col(labels, ("評価",))
        date_i = find_label_col(labels, ("上場日",))
        code_i = find_label_col(labels, ("証券コード",))

        for row in grid[first_data:]:
            code4 = None
            if code_i is not None and code_i < len(row):
                code4 = extract_code(row[code_i])
            if not code4:
                code4 = next((extract_code(v) for v in row if extract_code(v)), None)
            if not code4:
                continue

            listed = None
            if date_i is not None and date_i < len(row):
                listed = extract_month_day(row[date_i], year)
            if not listed:
                listed = next((extract_month_day(v, year) for v in row if extract_month_day(v, year)), None)
            if not listed:
                continue

            market = ""
            if market_i is not None and market_i < len(row):
                market = next((m for m in MARKETS if m in clean_text(row[market_i])), "")
            if not market:
                market = next((m for m in MARKETS if any(m in clean_text(v) for v in row)), "")

            # Only Tokyo Prime/Standard/Growth are wanted.
            if not market:
                continue

            rating = None
            if rating_i is not None and rating_i < len(row):
                rating = normalize_rating(row[rating_i])
            if not rating:
                # Look near the date/code rather than over the whole row to avoid broker initials.
                for v in row[: max(4, (code_i or 2) + 1)]:
                    r = normalize_rating(v)
                    if r:
                        rating = r
                        break

            public = parse_yen(row[public_i]) if public_i is not None and public_i < len(row) else None
            initial = parse_yen(row[initial_i]) if initial_i is not None and initial_i < len(row) else None

            # Fallback price extraction if labels differ on mobile/desktop table variants.
            yen_cells = [(i, parse_yen(v)) for i, v in enumerate(row)]
            yen_cells = [(i, p) for i, p in yen_cells if p is not None]
            if public is None and yen_cells:
                # Public price appears before initial price in the desktop table.
                public = yen_cells[0][1]
            if initial is None:
                if market_i is not None:
                    after_market = [p for i, p in yen_cells if i > market_i]
                    if after_market:
                        initial = after_market[0]
                if initial is None and len(yen_cells) >= 2:
                    initial = yen_cells[-1][1]

            name = ""
            if name_i is not None and name_i < len(row):
                name = clean_text(row[name_i])
                # Broker name may be concatenated in the same cell.  Keep only the part
                # before obvious broker suffixes if present.
                name = re.split(r"\s+(?:野村|大和|みずほ|岡三|SMBC|SBI|松井|楽天|三菱UFJ)", name)[0]
            name = normalize_company_name(name)

            result[code4] = {
                "code4": code4,
                "nameIpokabu": name or None,
                "listedDateIpokabu": listed,
                "marketIpokabu": market,
                "ipoRating": rating,
                "ipoRatingSource": "庶民のIPO" if rating else None,
                "ipoSourceUrl": source_url,
                "publicPrice": public,
                "initialPrice": initial,
            }

        # The first matching main IPO table is sufficient; pages also contain a mobile duplicate.
        if result:
            break

    return result


def parse_ipokabu() -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for year, url in IPO_KABU_URLS.items():
        try:
            html = fetch_html(url)
            rows = parse_ipokabu_year(html, year, url)
            print(f"[庶民のIPO] {year} parsed={len(rows)}", flush=True)
            merged.update(rows)
        except Exception as e:
            print(f"[WARN] 庶民のIPO {year}: {e}", flush=True)

    cutoff = (date.today() - timedelta(days=IPO_DISCOVERY_DAYS)).isoformat()
    today_s = date.today().isoformat()
    merged = {
        c: x for c, x in merged.items()
        if cutoff <= x.get("listedDateIpokabu", "") <= today_s
        and x.get("marketIpokabu") in MARKETS
    }
    print(f"[庶民のIPO] usable 3-year records={len(merged)}", flush=True)
    return merged


# ---------------------------------------------------------------------------
# Master merge
# ---------------------------------------------------------------------------
def build_master(
    jpx: dict[str, dict[str, Any]],
    ipokabu: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Union of both sources.
    JPX wins for official name/date/market.
    庶民のIPO fills missing records if JPX page parsing is temporarily incomplete.
    """
    codes = set(jpx) | set(ipokabu)
    out = []

    for code4 in codes:
        j = jpx.get(code4, {})
        k = ipokabu.get(code4, {})

        listed = j.get("listedDate") or k.get("listedDateIpokabu")
        market = j.get("market") or k.get("marketIpokabu")
        name = normalize_company_name(j.get("name") or k.get("nameIpokabu") or code4)

        if not listed or market not in MARKETS:
            continue
        if "*" in name:
            continue
        if "インタビュー" in name:
            name = normalize_company_name(name)

        out.append({
            "code4": code4,
            "name": name,
            "market": market,
            "listedDate": listed,
            "masterSource": "JPX" if j else "庶民のIPO fallback",
            "jpxSourceUrl": j.get("jpxSourceUrl"),
        })

    cutoff = (date.today() - timedelta(days=IPO_DISCOVERY_DAYS)).isoformat()
    today_s = date.today().isoformat()
    out = [x for x in out if cutoff <= x["listedDate"] <= today_s]
    out.sort(key=lambda x: x["listedDate"], reverse=True)

    # Only protect against a catastrophic empty scrape.  Do NOT fail just because
    # one source has a temporary partial parse; the union/fallback is intentional.
    if len(out) < 20:
        raise RuntimeError(
            f"Only {len(out)} IPO master records could be built. "
            "Refusing to overwrite latest.json."
        )
    if len(out) < 150:
        print(
            f"[WARN] master has {len(out)} records, lower than expected; "
            "continuing because JPX+庶民のIPO fallback produced a non-empty valid universe.",
            flush=True,
        )

    print(
        f"[MASTER] total={len(out)} "
        f"(JPX={sum(1 for x in out if x['masterSource']=='JPX')}, "
        f"fallback={sum(1 for x in out if x['masterSource']!='JPX')})",
        flush=True,
    )
    return out


# ---------------------------------------------------------------------------
# Existing JSON / prices
# ---------------------------------------------------------------------------
def load_existing() -> tuple[dict[str, dict[str, Any]], bool]:
    if not OUT.exists():
        print("[CACHE] no existing latest.json -> FULL BACKFILL", flush=True)
        return {}, True

    try:
        raw = json.loads(OUT.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[WARN] existing latest.json unreadable: {e}", flush=True)
        return {}, True

    items = raw.get("ipos", []) if isinstance(raw, dict) else raw if isinstance(raw, list) else []
    by_code: dict[str, dict[str, Any]] = {}

    for x in items:
        if not isinstance(x, dict):
            continue
        code4 = extract_code(x.get("code4") or x.get("code"))
        if code4:
            by_code[code4] = x

    is_v5 = bool(by_code) and all(
        x.get("_schemaVersion") == SCHEMA_VERSION
        for x in list(by_code.values())[: min(20, len(by_code))]
    )
    full_backfill = not is_v5
    print(
        f"[CACHE] existing={len(by_code)} mode={'FULL BACKFILL' if full_backfill else 'INCREMENTAL'}",
        flush=True,
    )
    return by_code, full_backfill


def normalize_price_frame(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []

    out: list[dict[str, Any]] = []
    for idx, row in df.iterrows():
        try:
            d = pd.Timestamp(idx).date().isoformat()
        except Exception:
            continue

        close = row.get("Close")
        if close is None or pd.isna(close):
            continue

        item: dict[str, Any] = {"date": d, "close": round(float(close), 4)}
        for field in ("Open", "High", "Low"):
            v = row.get(field)
            if v is not None and not pd.isna(v):
                item[field.lower()] = round(float(v), 4)
        vol = row.get("Volume")
        if vol is not None and not pd.isna(vol):
            try:
                item["volume"] = int(vol)
            except Exception:
                pass
        out.append(item)
    return out


def download_prices(symbols: list[str], start: str, end: str) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    chunk_size = 50

    for pos in range(0, len(symbols), chunk_size):
        chunk = symbols[pos: pos + chunk_size]
        print(f"[yfinance] batch {pos+1}-{pos+len(chunk)}/{len(symbols)}", flush=True)

        try:
            raw = yf.download(
                tickers=chunk,
                start=start,
                end=end,
                interval="1d",
                auto_adjust=False,
                actions=False,
                group_by="column",
                threads=True,
                progress=False,
                timeout=30,
            )
        except Exception as e:
            print(f"[WARN] yfinance batch failed: {e}", flush=True)
            continue

        if raw is None or raw.empty:
            continue

        for symbol in chunk:
            try:
                if isinstance(raw.columns, pd.MultiIndex):
                    l0 = set(map(str, raw.columns.get_level_values(0)))
                    l1 = set(map(str, raw.columns.get_level_values(1)))
                    if symbol in l1:
                        sub = raw.xs(symbol, axis=1, level=1)
                    elif symbol in l0:
                        sub = raw.xs(symbol, axis=1, level=0)
                    else:
                        continue
                else:
                    sub = raw
                hist = normalize_price_frame(sub)
                if hist:
                    result[symbol] = hist
            except Exception as e:
                print(f"[WARN] yfinance parse {symbol}: {e}", flush=True)

    print(f"[yfinance] data={len(result)}/{len(symbols)} tickers", flush=True)
    return result


def merge_history(old: Any, new: Any) -> list[dict[str, Any]]:
    by_date: dict[str, dict[str, Any]] = {}
    if isinstance(old, list):
        for x in old:
            if isinstance(x, dict) and x.get("date"):
                by_date[str(x["date"])] = x
    if isinstance(new, list):
        for x in new:
            if isinstance(x, dict) and x.get("date"):
                by_date[str(x["date"])] = x

    cutoff = (date.today() - timedelta(days=HISTORY_RETENTION_DAYS)).isoformat()
    return [by_date[d] for d in sorted(by_date) if d >= cutoff]


def milestone(history: list[dict[str, Any]], listed: str, days: int) -> tuple[float | None, str | None]:
    if not history:
        return None, None
    target = date.fromisoformat(listed) + timedelta(days=days)
    limit = target + timedelta(days=15)
    for x in history:
        try:
            d = date.fromisoformat(str(x.get("date")))
        except Exception:
            continue
        if target <= d <= limit:
            return x.get("close"), x.get("date")
        if d > limit:
            break
    return None, None


def pct(current: Any, base: Any) -> float | None:
    try:
        if current is None or base in (None, 0):
            return None
        return round((float(current) / float(base) - 1.0) * 100.0, 2)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Optional J-Quants financial refresh (non-fatal, limited budget)
# ---------------------------------------------------------------------------
def jq_headers(secret: str) -> dict[str, str] | None:
    secret = clean_text(secret)
    if not secret:
        return None
    # Current API-key style.
    if ":" not in secret:
        return {"x-api-key": secret}
    return None


def fetch_financials_nonfatal(code4: str, secret: str) -> list[dict[str, Any]]:
    headers = jq_headers(secret)
    if not headers:
        return []

    urls = [
        f"https://api.jquants.com/v2/fins/summary?code={code4}0",
        f"https://api.jquants.com/v2/fins/statements?code={code4}0",
    ]
    for url in urls:
        try:
            r = requests.get(url, headers=headers, timeout=20)
            if not r.ok:
                continue
            js = r.json()
            for key in ("data", "statements", "financials", "financial_summary"):
                data = js.get(key)
                if isinstance(data, list):
                    return data
        except Exception:
            continue
    return []


def should_refresh_financials(prev: dict[str, Any]) -> bool:
    if not prev.get("financials"):
        return True
    stamp = clean_text(prev.get("financialsUpdatedAt"))
    if not stamp:
        return True
    try:
        return (date.today() - date.fromisoformat(stamp[:10])).days >= FINANCIAL_REFRESH_DAYS
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    run_started = datetime.now().isoformat(timespec="seconds")
    old_by_code, full_backfill = load_existing()

    # Always rebuild IPO metadata.
    jpx = parse_jpx()
    ipokabu = parse_ipokabu()
    master = build_master(jpx, ipokabu)

    today = date.today()
    start = (
        today - timedelta(days=HISTORY_RETENTION_DAYS + 10)
        if full_backfill
        else today - timedelta(days=RECENT_REFRESH_DAYS)
    ).isoformat()
    end = (today + timedelta(days=1)).isoformat()

    symbols = [f"{x['code4']}.T" for x in master]
    price_map = download_prices(symbols, start, end)

    jq_secret = os.environ.get("JQUANTS_API_KEY", "").strip()
    jq_budget = 8
    jq_attempts = 0

    items: list[dict[str, Any]] = []

    for i, m in enumerate(master, 1):
        code4 = m["code4"]
        prev = old_by_code.get(code4, {})
        ipo = ipokabu.get(code4, {})
        symbol = f"{code4}.T"

        history = merge_history(prev.get("priceHistory", []), price_map.get(symbol, []))
        current_price = history[-1].get("close") if history else prev.get("currentPrice")
        price_date = history[-1].get("date") if history else prev.get("priceAsOfDate")

        public = ipo.get("publicPrice") or prev.get("publicPrice")
        initial = ipo.get("initialPrice") or prev.get("initialPrice")

        financials = prev.get("financials", [])
        financials_updated = prev.get("financialsUpdatedAt")

        if jq_secret and jq_attempts < jq_budget and should_refresh_financials(prev):
            fresh = fetch_financials_nonfatal(code4, jq_secret)
            jq_attempts += 1
            if fresh:
                financials = fresh
                financials_updated = today.isoformat()

        miles: dict[str, Any] = {}
        for days in (30, 90, 180, 365, 730, 1095):
            p, d = milestone(history, m["listedDate"], days)
            miles[f"price{days}d"] = p
            miles[f"price{days}dDate"] = d

        # IMPORTANT: fresh master name always wins. Old contaminated name is never reused.
        fresh_name = normalize_company_name(m["name"])
        if "インタビュー" in fresh_name:
            raise RuntimeError(f"Invalid company name after sanitization: {fresh_name}")

        item = {
            "_schemaVersion": SCHEMA_VERSION,
            "code": code4,
            "code4": code4,
            "name": fresh_name,
            "market": m["market"],
            "listedDate": m["listedDate"],
            "masterSource": m["masterSource"],

            "publicPrice": public,
            "initialPrice": initial,
            "initialReturnPct": pct(initial, public),
            "ipoRating": ipo.get("ipoRating") or prev.get("ipoRating"),
            "ipoRatingSource": ipo.get("ipoRatingSource") or prev.get("ipoRatingSource"),
            "ipoSourceUrl": ipo.get("ipoSourceUrl") or prev.get("ipoSourceUrl"),

            "currentPrice": current_price,
            "priceAsOfDate": price_date,
            "priceVsPublicPct": pct(current_price, public),
            "priceVsInitialPct": pct(current_price, initial),

            **miles,

            "priceHistory": history,
            "financials": financials if isinstance(financials, list) else [],
            "financialsUpdatedAt": financials_updated,

            # Preserve optional fields from older versions if already present.
            "marketCap": prev.get("marketCap"),
            "currentPER": prev.get("currentPER"),

            "dataRetrievedAt": run_started,
            "dataSource": [
                m["masterSource"],
                * (["庶民のIPO"] if ipo else []),
                * (["yfinance"] if history else []),
                * (["J-Quants"] if financials else []),
            ],
        }
        items.append(item)

        if i % 25 == 0 or i == len(master):
            print(f"[BUILD] {i}/{len(master)}", flush=True)

    # Final validations before touching latest.json.
    if not items:
        raise RuntimeError("No IPO data built; latest.json left unchanged.")
    if any("インタビュー" in x.get("name", "") for x in items):
        raise RuntimeError("Interview label found in final company names; latest.json left unchanged.")

    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(items, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    tmp.replace(OUT)

    print(
        f"[DONE] IPOs={len(items)} latest.json={OUT.stat().st_size/1024/1024:.2f}MB "
        f"mode={'full-backfill' if full_backfill else 'incremental'}",
        flush=True,
    )


if __name__ == "__main__":
    main()
