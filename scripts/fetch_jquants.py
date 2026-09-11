#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
IPO Tracker FAST v8

Main fix:
- Every current-listed security code is verified against JPX's official
  "Listed company search" before writing latest.json.
- The verified JPX code -> company-name -> market mapping overrides all scraped names.
- This eliminates shifted pairings such as MEEQ/Hmcomm/etc across the whole universe,
  not only a few hard-coded examples.

Policy:
- IPO discovery: last 3 years
- Retention: up to 4 years
- JPX archive provides listing date / offer price
- JPX current-company search provides authoritative current code/name/market
- 庶民のIPO provides rating/public price/initial price when available
- yfinance provides price history and listing-day Open fallback for 初値
- existing JSON is the persistent master; new IPOs are appended incrementally
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pandas as pd
import requests
import yfinance as yf
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "latest.json"
OUT.parent.mkdir(parents=True, exist_ok=True)

SCHEMA_VERSION = 21
IPO_DISCOVERY_DAYS = 1095       # new IPO search: 3 years
RETENTION_DAYS = 1460           # keep tracked IPO records/history: 4 years
RECENT_REFRESH_DAYS = 120
FINANCIAL_REFRESH_DAYS = 7

MARKETS = ("プライム", "スタンダード", "グロース")

JPX_ARCHIVES = [
    "https://www.jpx.co.jp/listing/stocks/new/index.html",
    "https://www.jpx.co.jp/listing/stocks/new/00-archives-01.html",
    "https://www.jpx.co.jp/listing/stocks/new/00-archives-02.html",
    "https://www.jpx.co.jp/listing/stocks/new/00-archives-03.html",
    "https://www.jpx.co.jp/listing/stocks/new/00-archives-04.html",
]

JPX_EN_ARCHIVES = [
    "https://www.jpx.co.jp/english/listing/stocks/new/index.html",
    "https://www.jpx.co.jp/english/listing/stocks/new/00-archives-01.html",
    "https://www.jpx.co.jp/english/listing/stocks/new/00-archives-02.html",
    "https://www.jpx.co.jp/english/listing/stocks/new/00-archives-03.html",
    "https://www.jpx.co.jp/english/listing/stocks/new/00-archives-04.html",
]

IPO_KABU_URLS = {
    2026: "https://ipokabu.net/ipo/",
    2025: "https://ipokabu.net/ipo/list2025",
    2024: "https://ipokabu.net/ipo/list2024",
    2023: "https://ipokabu.net/ipo/list2023",
    2022: "https://ipokabu.net/ipo/list2022",
}

IPO_KABU_ATTENTION_GRADES = ("S", "A", "B", "C", "D")
IPO_KABU_DETAIL_URL = "https://ipokabu.net/ipo/{code}"

JPX_SEARCH = (
    "https://www2.jpx.co.jp/tseHpFront/StockSearch.do?"
    "method=topsearch&topSearchStr={code}"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 "
        "Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.7,en;q=0.5",
}

DATE_RE = re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})")
MD_RE = re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)")
CODE_RE = re.compile(r"^\s*((?:\d{4}|\d{3}[A-Z]))\s*$", re.I)


def clean_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and math.isnan(v):
        return ""
    return re.sub(r"\s+", " ", str(v).replace("\u3000", " ")).strip()


def normalize_company_name(v: Any) -> str:
    s = clean_text(v)
    for label in (
        "代表者インタビュー", "創業者インタビュー",
        "社長インタビュー", "経営者インタビュー",
    ):
        s = s.replace(label, "")
    s = re.sub(r"(?:代表者|創業者|社長|経営者)\s*インタビュー", "", s)
    # Normalize common corporate-mark variants only at the edges.
    s = re.sub(r"\s+", " ", s).strip(" |　")
    return s


def parse_date(v: Any) -> str | None:
    m = DATE_RE.search(clean_text(v))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


def parse_md(v: Any, year: int) -> str | None:
    m = MD_RE.search(clean_text(v))
    if not m:
        return None
    try:
        return date(year, int(m.group(1)), int(m.group(2))).isoformat()
    except ValueError:
        return None


def parse_code(v: Any) -> str | None:
    """Canonical TSE code: 4 digits OR 3 digits + letter.

    Accepts J-Quants 5-character representation with a trailing zero:
      83030 -> 8303
      607A0 -> 607A
    """
    s = clean_text(v).upper().replace(".T", "")
    m = re.fullmatch(r"((?:\d{4}|\d{3}[A-Z]))0", s)
    if m:
        s = m.group(1)
    m = re.fullmatch(r"(\d{4})\.0", s)
    if m:
        s = m.group(1)
    m = CODE_RE.fullmatch(s)
    return m.group(1).upper() if m else None


def parse_price(v: Any, plain=True) -> int | None:
    s = clean_text(v)
    m = re.search(r"([\d,]+(?:\.\d+)?)\s*円", s)
    if not m and plain:
        m = re.fullmatch(r"([\d,]+(?:\.\d+)?)", s)
    if not m:
        return None
    try:
        n = float(m.group(1).replace(",", ""))
        if 1 <= n <= 10_000_000:
            return int(round(n))
    except Exception:
        pass
    return None


def fetch_html(url: str, timeout=35) -> str:
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
                time.sleep(1.5 + attempt)
    raise RuntimeError(f"fetch failed {url}: {last}")


def cells(tr) -> list[str]:
    return [clean_text(td.get_text(" ", strip=True))
            for td in tr.find_all(["th", "td"], recursive=False)]


def next_nonempty_row(tr):
    n = tr.find_next_sibling("tr")
    while n is not None:
        c = cells(n)
        if c:
            return c
        n = n.find_next_sibling("tr")
    return []


# ------------------------------------------------------------------
# JPX IPO archives: robust same-row date/code/name extraction
# ------------------------------------------------------------------
def parse_jpx_archive_page(html: str, url: str) -> dict[str, dict[str, Any]]:
    """
    Parse JPX new-listing pages using the FIRST physical row only.

    Important change:
    Previous versions required the market cell to be found in the following row.
    On 2024/2025/current JPX pages that assumption fails in GitHub Actions and
    discarded almost every IPO (e.g. 2025=4, 2024=6).

    The first row already contains the three things needed to build a safe
    candidate master:
      listing date / company name / security code

    Market is verified later with JPX's official current-listed-company search,
    so this parser no longer rejects a candidate just because the second row
    could not be associated.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, dict[str, Any]] = {}

    for tr in soup.find_all("tr"):
        # Use all direct th/td cells; links remain inside their cell text.
        c = cells(tr)
        if len(c) < 2:
            continue

        listed = parse_date(c[0])
        if not listed:
            continue

        # Find the security code in THIS SAME row only.
        code_idx = None
        code4 = None
        for i, value in enumerate(c):
            cc = parse_code(value)
            if cc:
                code_idx, code4 = i, cc
                break
        if code4 is None or code_idx is None:
            continue

        # Company name is the nearest meaningful cell before the code.
        raw_name = ""
        for value in reversed(c[:code_idx]):
            t = clean_text(value)
            if not t:
                continue
            # Skip listing/approval date cells and link labels.
            if parse_date(t):
                continue
            if t in ("代表者インタビュー", "創業者インタビュー", "社長インタビュー",
                     "経営者インタビュー", "詳細", "会社概要", "確認書"):
                continue
            raw_name = t
            break

        if not raw_name:
            continue

        # JPX marks technical listings with *; exclude them.
        if "*" in raw_name:
            continue

        name = normalize_company_name(raw_name)
        if not name:
            continue

        # Try to recover market / public price, but do NOT require them.
        row2 = next_nonempty_row(tr)
        market = next((m for m in MARKETS if any(m in x for x in row2)), "")
        if not market:
            market = next((m for m in MARKETS if any(m in x for x in c)), "")

        offer = None
        # Search likely second-row values first.
        for value in row2:
            p = parse_price(value)
            if p and p != 100:
                offer = p
                break

        prev = out.get(code4)
        item = {
            "code4": code4,
            "archiveName": name,
            "listedDate": listed,
            "archiveMarket": market or None,
            "publicPriceJPX": offer,
            "archiveSourceUrl": url,
        }
        if not prev or listed > prev["listedDate"]:
            out[code4] = item

    return out


def flatten_jpx_columns(df: pd.DataFrame) -> list[str]:
    labels = []
    for col in df.columns:
        if isinstance(col, tuple):
            parts = []
            for p in col:
                s = clean_text(p)
                if s and not s.startswith("Unnamed") and s not in parts:
                    parts.append(s)
            labels.append(" / ".join(parts))
        else:
            labels.append(clean_text(col))
    return labels


def parse_english_listing_date(value: Any) -> str | None:
    s = clean_text(value)
    # First date is the listing date; approval date may follow in parentheses.
    if not s:
        return None
    # pandas handles "Dec. 25, 2025" and similar strings.
    m = re.match(r"^([A-Za-z]{3,9}\.?\s+\d{1,2},\s+\d{4})", s)
    if not m:
        return None
    try:
        return pd.to_datetime(m.group(1), errors="raise").date().isoformat()
    except Exception:
        return None


def parse_jpx_english_page(html: str, url: str) -> dict[str, dict[str, Any]]:
    """
    Candidate discovery from JPX English pages.

    Why English pages:
    The Japanese HTML returned to GitHub Actions has been parsed as only a
    handful of physical rows for 2024/2025. The English archive exposes the
    same official listings in a conventional logical table and does not contain
    the interview-link text that complicated the Japanese parser.

    We only use it for code/date/optional offer price discovery. Japanese company
    name and market are still overwritten by JPX official current-company lookup.
    """
    result: dict[str, dict[str, Any]] = {}

    try:
        tables = pd.read_html(io.StringIO(html))
    except Exception as e:
        print(f"[WARN] JPX EN read_html {url}: {e}", flush=True)
        return result

    for df in tables:
        if df.empty:
            continue

        labels = flatten_jpx_columns(df)
        joined = " | ".join(labels).lower()
        if "date of listing" not in joined or "code" not in joined:
            continue

        date_i = next((i for i,l in enumerate(labels) if "Date of Listing" in l), None)
        code_i = next((i for i,l in enumerate(labels) if re.search(r"(^| / )Code($| / )", l)), None)
        market_i = next((i for i,l in enumerate(labels) if "Market" in l), None)
        offer_i = next((i for i,l in enumerate(labels) if "Offering Price" in l), None)

        if date_i is None or code_i is None:
            continue

        for _, row in df.iterrows():
            vals = list(row.values)
            if date_i >= len(vals) or code_i >= len(vals):
                continue

            listed = parse_english_listing_date(vals[date_i])
            code = parse_code(vals[code_i])
            if not listed or not code:
                continue

            market = None
            if market_i is not None and market_i < len(vals):
                raw_market = clean_text(vals[market_i]).lower()
                if "prime" in raw_market:
                    market = "プライム"
                elif "standard" in raw_market:
                    market = "スタンダード"
                elif "growth" in raw_market:
                    market = "グロース"

            offer = None
            if offer_i is not None and offer_i < len(vals):
                offer = parse_price(vals[offer_i], plain=True)

            result[code] = {
                "code4": code,
                "listedDate": listed,
                "archiveMarket": market,
                "publicPriceJPX": offer,
                "archiveSourceUrl": url,
            }

        if result:
            break

    return result


def fetch_jpx_archive_master() -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}

    # 1) Japanese pages: keep anything the existing parser can recover.
    for url in JPX_ARCHIVES:
        try:
            rows = parse_jpx_archive_page(fetch_html(url), url)
            print(f"[JPX archive JP] {url.split('/')[-1]} rows={len(rows)}", flush=True)
            for code, item in rows.items():
                old = merged.get(code, {})
                merged[code] = {
                    **old,
                    **item,
                    "archiveName": item.get("archiveName") or old.get("archiveName"),
                }
        except Exception as e:
            print(f"[WARN] JPX archive JP {url}: {e}", flush=True)

    # 2) English pages: authoritative candidate discovery for years where the
    # Japanese physical-row parser under-counts.
    for url in JPX_EN_ARCHIVES:
        try:
            rows = parse_jpx_english_page(fetch_html(url), url)
            print(f"[JPX archive EN] {url.split('/')[-1]} rows={len(rows)}", flush=True)
            for code, item in rows.items():
                old = merged.get(code, {})
                # Preserve Japanese name when available; English is used only to
                # guarantee code/date coverage.
                merged[code] = {
                    **old,
                    "code4": code,
                    "listedDate": item.get("listedDate") or old.get("listedDate"),
                    "archiveMarket": item.get("archiveMarket") or old.get("archiveMarket"),
                    "publicPriceJPX": item.get("publicPriceJPX") or old.get("publicPriceJPX"),
                    "archiveSourceUrl": item.get("archiveSourceUrl") or old.get("archiveSourceUrl"),
                    "archiveName": old.get("archiveName"),
                }
        except Exception as e:
            print(f"[WARN] JPX archive EN {url}: {e}", flush=True)

    cutoff = (date.today() - timedelta(days=RETENTION_DAYS)).isoformat()
    today_s = date.today().isoformat()

    merged = {
        c: x for c, x in merged.items()
        if x.get("listedDate")
        and cutoff <= x["listedDate"] <= today_s
    }

    coverage: dict[str, int] = {}
    for item in merged.values():
        y = item["listedDate"][:4]
        coverage[y] = coverage.get(y, 0) + 1

    print(f"[JPX archive] retained={len(merged)} coverage={coverage}", flush=True)
    return merged


# ------------------------------------------------------------------
# JPX official current listed-company lookup
# ------------------------------------------------------------------
def parse_jpx_search_result(html: str, wanted_code: str) -> dict[str, Any] | None:
    soup = BeautifulSoup(html, "html.parser")
    wanted5 = wanted_code + "0"

    for tr in soup.find_all("tr"):
        c = cells(tr)
        if len(c) < 3:
            continue

        code_text = clean_text(c[0]).upper().replace(" ", "")
        # JPX search shows security code as 5 chars: 332A0 / 87290.
        if code_text != wanted5.upper():
            continue

        name = normalize_company_name(c[1])
        market = next((m for m in MARKETS if m in c[2]), "")
        if not name or not market:
            return None
        return {
            "code4": wanted_code,
            "officialName": name,
            "officialMarket": market,
            "officialVerified": True,
        }

    return None


def verify_one_code(code4: str) -> tuple[str, dict[str, Any] | None]:
    url = JPX_SEARCH.format(code=quote(code4))
    try:
        return code4, parse_jpx_search_result(fetch_html(url, timeout=25), code4)
    except Exception:
        return code4, None


def verify_codes_with_jpx(codes: list[str]) -> dict[str, dict[str, Any]]:
    """
    Verify the entire current-listed universe against JPX official code/name mapping.
    Parallel but modest concurrency to avoid making the job slow.
    """
    verified: dict[str, dict[str, Any]] = {}
    print(f"[JPX verify] checking {len(codes)} codes...", flush=True)

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(verify_one_code, c) for c in codes]
        for n, fut in enumerate(as_completed(futs), 1):
            code, data = fut.result()
            if data:
                verified[code] = data
            if n % 50 == 0 or n == len(futs):
                print(f"[JPX verify] {n}/{len(futs)} verified={len(verified)}", flush=True)

    return verified


# ------------------------------------------------------------------
# 庶民のIPO: company-link anchored parser
# ------------------------------------------------------------------
def normalize_grade_text(value: Any) -> str | None:
    s = clean_text(value).upper().translate(str.maketrans("ＳＡＢＣＤＥ", "SABCDE"))
    m = re.search(r"(?<![A-Z])([SABCDE])(?![A-Z])", s)
    return m.group(1) if m else None


def extract_md_any(value: Any, year: int) -> str | None:
    m = re.search(r"(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)", clean_text(value))
    if not m:
        return None
    try:
        return date(year, int(m.group(1)), int(m.group(2))).isoformat()
    except ValueError:
        return None


def ipo_code_from_href(href: str) -> str | None:
    href = clean_text(href)
    m = re.search(r"/ipo/((?:\d{4}|\d{3}[A-Z]))(?:[/?#].*)?$", href, re.I)
    return m.group(1).upper() if m else None


def yen_values(value: Any) -> list[int]:
    vals = []
    for m in re.finditer(r"(-?[\d,]+(?:\.\d+)?)\s*円", clean_text(value)):
        try:
            v = float(m.group(1).replace(",", ""))
            if v > 0:
                vals.append(int(round(v)))
        except Exception:
            pass
    return vals


def row_contains_other_ipo(tr, current_code: str) -> bool:
    if tr is None:
        return False
    for a in tr.find_all("a", href=True):
        c = ipo_code_from_href(a.get("href", ""))
        if c and c != current_code:
            return True
    return False



def parse_ipokabu_annual_table_rows(
    html: str, year: int, url: str
) -> dict[str, dict[str, Any]]:
    """Parse 庶民のIPO annual result table directly by security code.

    Reads only stable annual-table fields:
      listing date / security code / S-A-B-C-D evaluation /
      company name / public price / market (best effort).

    Initial price is intentionally NOT read from the annual table because
    "初値売り損益" is nearby and caused the 31,600円 -> 初値 misread.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, dict[str, Any]] = {}

    for tr in soup.find_all("tr"):
        cs = [
            clean_text(td.get_text(" ", strip=True))
            for td in tr.find_all(["th", "td"], recursive=False)
        ]
        if not cs:
            continue

        code_idx = None
        code = None
        for i, cell in enumerate(cs):
            c = parse_code(cell)
            if c:
                code_idx, code = i, c
                break
        if code is None or code_idx is None:
            continue

        listed = None
        for cell in cs:
            listed = extract_md_any(cell, year)
            if listed:
                break
        if not listed:
            prev = tr.find_previous_sibling("tr")
            if prev is not None:
                listed = extract_md_any(prev.get_text(" ", strip=True), year)
        if not listed:
            continue

        evaluation = None
        for cell in cs:
            g = normalize_grade_text(cell)
            if g in IPO_KABU_ATTENTION_GRADES:
                evaluation = g
                break

        name = ""
        for cell in cs[code_idx + 1:]:
            t = normalize_company_name(cell)
            if not t:
                continue
            if parse_price(t) is not None:
                continue
            if any(m in t for m in MARKETS):
                continue
            if "%" in t or "％" in t or "円" in t:
                continue
            name = t
            break

        public_price = None
        for cell in cs[code_idx + 1:]:
            p = parse_price(cell)
            if p is not None:
                public_price = p
                break

        market = next((m for m in MARKETS if any(m in x for x in cs)), "")
        if not market:
            nxt = tr.find_next_sibling("tr")
            if nxt is not None:
                nt = clean_text(nxt.get_text(" ", strip=True))
                market = next((m for m in MARKETS if m in nt), "")

        out[code] = {
            "code4": code,
            "ipokabuName": name or None,
            "ipokabuListedDate": listed,
            "ipokabuMarket": market or None,
            "publicPriceIpokabu": public_price,
            "initialPriceIpokabu": None,
            "ipoEvaluation": evaluation,
            "ipoSourceUrl": f"https://ipokabu.net/ipo/{code}",
        }

    return out


def parse_ipokabu_anchor_rows(html: str, year: int, url: str) -> dict[str, dict[str, Any]]:
    """
    Parse annual result pages by anchoring each record on its company-detail link:
      /ipo/<security-code>

    This avoids all rowspan/colspan assumptions. The annual pages expose one
    company link per IPO, which lets us recover 2024/2025/2026 even when their
    physical <tr> structure changes.

    For each company anchor:
      - code: from href
      - name: anchor text
      - date/evaluation/public price: enclosing row (or immediately previous row)
      - market/initial price: same row or following rows until next IPO company row
    """
    soup = BeautifulSoup(html, "html.parser")
    result: dict[str, dict[str, Any]] = {}

    for a in soup.find_all("a", href=True):
        code = ipo_code_from_href(a.get("href", ""))
        if not code:
            continue

        name = normalize_company_name(a.get_text(" ", strip=True))
        if not name:
            continue

        tr = a.find_parent("tr")
        if tr is None:
            continue

        # Avoid navigation/index links: annual result row should have listing-date
        # context in the row itself or immediately previous physical row.
        row_text = clean_text(tr.get_text(" ", strip=True))
        prev = tr.find_previous_sibling("tr")
        prev_text = clean_text(prev.get_text(" ", strip=True)) if prev is not None else ""

        listed = extract_md_any(row_text, year) or extract_md_any(prev_text, year)
        if not listed:
            continue

        evaluation = normalize_grade_text(row_text) or normalize_grade_text(prev_text)

        # Public price is the first positive yen value in the company row.
        # On the annual-result page the next yen value is usually initial-profit.
        row_yen = yen_values(row_text)
        public_price = row_yen[0] if row_yen else None

        market = ""
        # IMPORTANT:
        # Annual pages contain both 初値 and 初値売り損益. The old parser could
        # accidentally read the profit amount (e.g. 31,600円) as 初値.
        # Therefore the annual page is NOT used as a source of initial price.
        initial_price = None

        # Search current row and following physical rows only for market.
        scan_rows = [tr]
        nxt = tr.find_next_sibling("tr")
        for _ in range(3):
            if nxt is None or row_contains_other_ipo(nxt, code):
                break
            scan_rows.append(nxt)
            nxt = nxt.find_next_sibling("tr")

        for scan in scan_rows:
            txt = clean_text(scan.get_text(" ", strip=True))
            if not market:
                market = next((m for m in MARKETS if m in txt), "")

        # Annual pages also contain regional-market IPOs. We keep them in the
        # supplemental dictionary only when TSE market is explicit; otherwise
        # JPX official verification can still supply the market later.
        result[code] = {
            "code4": code,
            "ipokabuName": name,
            "ipokabuListedDate": listed,
            "ipokabuMarket": market or None,
            "publicPriceIpokabu": public_price,
            "initialPriceIpokabu": initial_price,
            "ipoEvaluation": evaluation,
            "ipoSourceUrl": f"https://ipokabu.net/ipo/{code}",
        }

    return result



def fetch_ipokabu_detail_initial(code: str) -> int | None:
    """Fetch actual 初値 from the individual 庶民のIPO page.

    This is only used when yfinance cannot provide listing-day Open.
    The parser looks for a table cell whose label is exactly 初値, avoiding
    初値売り損益 / 予想利益 fields.
    """
    try:
        html = fetch_html(IPO_KABU_DETAIL_URL.format(code=code), timeout=25)
        soup = BeautifulSoup(html, "html.parser")

        for tr in soup.find_all("tr"):
            cs = [clean_text(x.get_text(" ", strip=True)) for x in tr.find_all(["th", "td"])]
            for i, cell in enumerate(cs[:-1]):
                if cell == "初値":
                    vals = yen_values(cs[i + 1])
                    if vals:
                        return vals[0]

        # Fallback for non-tabular detail markup. Explicitly exclude 売り損益/予想.
        text = clean_text(soup.get_text(" ", strip=True))
        m = re.search(r"(?:^|\\s)初値\\s*([\\d,]+)\\s*円(?![^。]{0,20}(?:売り損益|予想))", text)
        if m:
            return int(m.group(1).replace(",", ""))
    except Exception as e:
        print(f"[WARN] 庶民のIPO detail initial {code}: {e}", flush=True)
    return None


def fetch_ipokabu() -> dict[str, dict[str, Any]]:
    """Fetch annual S/A/B/C/D evaluations and merge by security code.

    Primary: annual table parser.
    Fallback: old anchor parser fills only missing fields.
    Existing latest.json evaluations are preserved by main() when fetching fails.
    """
    merged: dict[str, dict[str, Any]] = {}

    for year, url in IPO_KABU_URLS.items():
        try:
            html = fetch_html(url)

            table_rows = parse_ipokabu_annual_table_rows(html, year, url)
            anchor_rows = parse_ipokabu_anchor_rows(html, year, url)

            rows = dict(table_rows)
            for code, a in anchor_rows.items():
                if code not in rows:
                    rows[code] = a
                    continue
                r = rows[code]
                for key in (
                    "ipokabuName", "ipokabuListedDate", "ipokabuMarket",
                    "publicPriceIpokabu", "ipoEvaluation", "ipoSourceUrl",
                ):
                    if not r.get(key) and a.get(key):
                        r[key] = a[key]

            evals = sum(
                1 for x in rows.values()
                if x.get("ipoEvaluation") in IPO_KABU_ATTENTION_GRADES
            )
            print(
                f"[庶民のIPO] {year} annual rows={len(table_rows)} "
                f"anchor fallback={len(anchor_rows)} evaluations={evals}",
                flush=True,
            )
            merged.update(rows)

        except Exception as e:
            print(f"[WARN] 庶民のIPO {year}: {e}", flush=True)

    cutoff = (date.today() - timedelta(days=RETENTION_DAYS)).isoformat()
    today_s = date.today().isoformat()

    merged = {
        code: item for code, item in merged.items()
        if item.get("ipokabuListedDate")
        and cutoff <= item["ipokabuListedDate"] <= today_s
    }

    coverage: dict[str, int] = {}
    eval_coverage: dict[str, int] = {}
    for item in merged.values():
        y = item["ipokabuListedDate"][:4]
        coverage[y] = coverage.get(y, 0) + 1
        if item.get("ipoEvaluation") in IPO_KABU_ATTENTION_GRADES:
            eval_coverage[y] = eval_coverage.get(y, 0) + 1

    print(
        f"[庶民のIPO] retained={len(merged)} coverage={coverage} "
        f"evaluationCoverage={eval_coverage}",
        flush=True,
    )
    return merged


# ------------------------------------------------------------------
# yfinance
# ------------------------------------------------------------------
def normalize_price_frame(df):
    if df is None or df.empty:
        return []
    out = []
    for idx, row in df.iterrows():
        try:
            d = pd.Timestamp(idx).date().isoformat()
        except Exception:
            continue
        close = row.get("Close")
        if close is None or pd.isna(close):
            continue
        item = {"date": d, "close": round(float(close), 4)}
        for f in ("Open", "High", "Low"):
            v = row.get(f)
            if v is not None and not pd.isna(v):
                item[f.lower()] = round(float(v), 4)
        v = row.get("Volume")
        if v is not None and not pd.isna(v):
            try:
                item["volume"] = int(v)
            except Exception:
                pass
        out.append(item)
    return out


def download_prices(symbols, start, end):
    result = {}
    chunk_size = 40
    for pos in range(0, len(symbols), chunk_size):
        chunk = symbols[pos:pos+chunk_size]
        print(f"[yfinance] {pos+1}-{pos+len(chunk)}/{len(symbols)}", flush=True)
        try:
            raw = yf.download(
                chunk, start=start, end=end, interval="1d",
                auto_adjust=False, actions=False, group_by="column",
                threads=True, progress=False, timeout=30,
            )
        except Exception as e:
            print(f"[WARN] yfinance batch: {e}", flush=True)
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
            except Exception:
                pass
    return result


def merge_history(old, new):
    by_date = {}
    def normalize_point(x):
        if not isinstance(x, dict) or not x.get("date"):
            return None
        y = dict(x)
        if y.get("close") is None and y.get("price") is not None:
            y["close"] = y.get("price")
        if y.get("price") is None and y.get("close") is not None:
            y["price"] = y.get("close")
        return y

    if isinstance(old, list):
        for x in old:
            y = normalize_point(x)
            if y:
                by_date[str(y["date"])] = y
    if isinstance(new, list):
        for x in new:
            y = normalize_point(x)
            if y:
                by_date[str(y["date"])] = y

    cutoff = (date.today() - timedelta(days=RETENTION_DAYS)).isoformat()
    return [by_date[d] for d in sorted(by_date) if d >= cutoff]


def listing_initial(history, listed):
    start = date.fromisoformat(listed)
    limit = start + timedelta(days=7)
    for x in history:
        try:
            d = date.fromisoformat(str(x.get("date")))
        except Exception:
            continue
        if start <= d <= limit and x.get("open") is not None:
            return x["open"]
        if d > limit:
            break
    return None


def milestone(history, listed, days):
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


def pct(cur, base):
    try:
        if cur is None or base in (None, 0):
            return None
        return round((float(cur) / float(base) - 1) * 100, 2)
    except Exception:
        return None


# ------------------------------------------------------------------
# J-Quants financials: non-fatal
# ------------------------------------------------------------------
def fetch_financials_nonfatal(code, key):
    if not key or ":" in key:
        return []
    headers = {"x-api-key": key}
    for ep in ("summary", "statements"):
        try:
            r = requests.get(
                f"https://api.jquants.com/v2/fins/{ep}?code={code}0",
                headers=headers, timeout=15
            )
            if not r.ok:
                continue
            js = r.json()
            for k in ("data", "statements", "financials", "financial_summary"):
                if isinstance(js.get(k), list):
                    return js[k]
        except Exception:
            pass
    return []


def should_refresh_financials(prev):
    if not prev.get("financials"):
        return True
    try:
        d = date.fromisoformat(str(prev.get("financialsUpdatedAt"))[:10])
        return (date.today() - d).days >= FINANCIAL_REFRESH_DAYS
    except Exception:
        return True


def derive_app_price_fields(hist):
    """Legacy app fields derived from unified history."""
    if not hist:
        return {
            "prevClose": None, "volume": None,
            "week52High": None, "week52Low": None,
            "allTimeHigh": None, "allTimeLow": None,
        }

    highs = [x.get("high") for x in hist if x.get("high") is not None]
    lows = [x.get("low") for x in hist if x.get("low") is not None]
    closes = [x.get("close") for x in hist if x.get("close") is not None]

    cutoff52 = (date.today() - timedelta(days=365)).isoformat()
    recent = [x for x in hist if x.get("date", "") >= cutoff52]
    rh = [x.get("high") for x in recent if x.get("high") is not None]
    rl = [x.get("low") for x in recent if x.get("low") is not None]

    return {
        "prevClose": closes[-2] if len(closes) >= 2 else None,
        "volume": hist[-1].get("volume"),
        "week52High": max(rh) if rh else (max(closes[-250:]) if closes else None),
        "week52Low": min(rl) if rl else (min(closes[-250:]) if closes else None),
        "allTimeHigh": max(highs) if highs else (max(closes) if closes else None),
        "allTimeLow": min(lows) if lows else (min(closes) if closes else None),
    }


def main():
    started = datetime.now().isoformat(timespec="seconds")
    old, full_backfill = load_existing()
    recovery = needs_recovery_backfill(old)

    print(
        f"[CACHE] existing={len(old)} mode={'RECOVERY BACKFILL' if recovery else 'INCREMENTAL'}",
        flush=True,
    )

    # First run after the 2-year -> 3-year requirement change:
    # scan all relevant JPX archive pages. Later runs only scan current + previous archive.
    global JPX_ARCHIVES, JPX_EN_ARCHIVES
    if not recovery:
        JPX_ARCHIVES = JPX_ARCHIVES[:2]
        JPX_EN_ARCHIVES = JPX_EN_ARCHIVES[:2]

    archive = fetch_jpx_archive_master()
    ipokabu = fetch_ipokabu()

    new_codes = sorted((set(archive) | set(ipokabu)) - set(old))
    official_new = verify_codes_with_jpx(new_codes) if new_codes else {}
    print(f"[JPX verify new] candidates={len(new_codes)} verified={len(official_new)}", flush=True)

    master = build_master(archive, ipokabu, official_new, old)
    added_codes = set(x["code4"] for x in master) - set(old)
    print(
        f"[MASTER] total={len(master)} added={len(added_codes)} "
        f"3y={sum(x['searchEligible3y'] for x in master)}",
        flush=True,
    )

    today = date.today()
    start = (
        (today - timedelta(days=RETENTION_DAYS + 10)).isoformat()
        if recovery or full_backfill
        else (today - timedelta(days=RECENT_REFRESH_DAYS)).isoformat()
    )
    end = (today + timedelta(days=1)).isoformat()

    symbols = [f"{x['code4']}.T" for x in master]
    price_map = download_prices(symbols, start, end)
    print(f"[yfinance] data={len(price_map)}/{len(symbols)}", flush=True)

    jq_key = os.environ.get("JQUANTS_API_KEY", "").strip()
    jq_budget = 6
    jq_attempts = 0

    items = []
    for i, m in enumerate(master, 1):
        code = m["code4"]
        prev = old.get(code, {})
        k = ipokabu.get(code, {})
        hist = merge_history(prev.get("priceHistory", []), price_map.get(f"{code}.T", []))

        current = hist[-1].get("close") if hist else prev.get("currentPrice")
        current_date = hist[-1].get("date") if hist else prev.get("priceAsOfDate")

        public = (
            m.get("publicPriceJPX")
            or k.get("publicPriceIpokabu")
            or prev.get("publicPrice")
            or prev.get("ipoPrice")
        )

        yf_initial = listing_initial(hist, m["listedDate"])

        # yfinance listing-day Open is the preferred actual 初値 source.
        # If yfinance is unavailable for this symbol, query the individual
        # 庶民のIPO page, whose "初値" cell is unambiguous.
        detail_initial = None
        if yf_initial is None:
            detail_initial = fetch_ipokabu_detail_initial(code)

        initial = (
            yf_initial
            or detail_initial
            or k.get("initialPriceIpokabu")
            or prev.get("initialPrice")
            or prev.get("firstDayPrice")
        )

        financials = prev.get("financials", [])
        fin_updated = prev.get("financialsUpdatedAt")
        if jq_key and jq_attempts < jq_budget and should_refresh_financials(prev):
            fresh = fetch_financials_nonfatal(code, jq_key)
            jq_attempts += 1
            if fresh:
                financials = fresh
                fin_updated = today.isoformat()

        miles = {}
        for days in (30, 90, 180, 365, 730, 1095):
            p, d = milestone(hist, m["listedDate"], days)
            miles[f"price{days}d"] = p
            miles[f"price{days}dDate"] = d

        ev = clean_text(
            k.get("ipoEvaluation")
            or prev.get("ipoEvaluation")
            or prev.get("ipoAttention")
            or prev.get("ipoRating")
        ).upper()
        if ev not in IPO_KABU_ATTENTION_GRADES:
            ev = None

        item = {
            "_schemaVersion": SCHEMA_VERSION,
            "code": code,
            "code4": code,
            "name": normalize_company_name(m["name"]),
            "market": m["market"],
            "listedDate": m["listedDate"],
            "searchEligible3y": m["searchEligible3y"],
            "masterSource": m["masterSource"],
            "officialCodeNameVerified": m["officialCodeNameVerified"],

            "publicPrice": public,
            "initialPrice": initial,
            "ipoPrice": public,
            "firstDayPrice": initial,

            "publicPriceSource": (
                "JPX" if m.get("publicPriceJPX") else
                "庶民のIPO" if k.get("publicPriceIpokabu") else
                prev.get("publicPriceSource") or "existing"
            ),
            "initialPriceSource": (
                "yfinance listing-day Open" if yf_initial is not None else
                "庶民のIPO individual page" if detail_initial is not None else
                prev.get("initialPriceSource") or "existing"
            ),
            "initialReturnPct": pct(initial, public),
            "ipoEvaluation": ev,
            "ipoSourceUrl": f"https://ipokabu.net/ipo/{code}",

            "currentPrice": current,
            "priceAsOfDate": current_date,
            "currentPriceUpdatedAt": current_date,
            **derive_app_price_fields(hist),
            "priceVsPublicPct": pct(current, public),
            "priceVsInitialPct": pct(current, initial),
            **miles,

            "priceHistory": hist,
            "financials": financials if isinstance(financials, list) else [],
            "financialsUpdatedAt": fin_updated,
            "marketCap": prev.get("marketCap"),
            "currentPER": prev.get("currentPER"),
            "dataSource": "persistent latest.json + JPX + 庶民のIPO + yfinance"
                          + (" + J-Quants" if financials else ""),
            "dataRetrievedAt": started,
        }
        items.append(item)

        if i % 25 == 0 or i == len(master):
            print(f"[BUILD] {i}/{len(master)}", flush=True)

    codes = [x["code4"] for x in items]
    assert len(codes) == len(set(codes)), "duplicate codes"
    assert all(re.fullmatch(r"(?:\d{4}|\d{3}[A-Z])", c) for c in codes), "non-canonical code"
    assert all("インタビュー" not in x["name"] for x in items), "interview label remains"

    retention_cutoff = (date.today() - timedelta(days=RETENTION_DAYS)).isoformat()
    retained_old_codes = {
        c for c, x in old.items()
        if retention_cutoff <= str(x.get("listedDate", "")) <= date.today().isoformat()
    }
    missing_old = retained_old_codes - set(codes)
    assert not missing_old, f"existing records would disappear: {sorted(missing_old)[:10]}"

    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(OUT)

    coverage = {}
    for x in items:
        y = str(x.get("listedDate", ""))[:4]
        if y:
            coverage[y] = coverage.get(y, 0) + 1

    print(
        f"[DONE] records={len(items)} added={len(added_codes)} coverage={coverage} "
        f"file={OUT.stat().st_size/1024/1024:.2f}MB "
        f"mode={'recovery' if recovery else 'incremental'}",
        flush=True,
    )


if __name__ == "__main__":
    main()
