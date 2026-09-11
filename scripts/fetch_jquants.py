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
- existing JSON is used only for history/financial data, NEVER as name/code master
"""

from __future__ import annotations

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

SCHEMA_VERSION = 11
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

IPO_KABU_URLS = {
    2026: "https://ipokabu.net/ipo/list2026",
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
CODE_RE = re.compile(r"^\s*(\d{4}[A-Z]?)\s*$", re.I)


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
    s = clean_text(v).upper()
    m = re.fullmatch(r"(\d{4})\.0", s)
    if m:
        return m.group(1)
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
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, dict[str, Any]] = {}

    for tr in soup.find_all("tr"):
        c = cells(tr)
        if len(c) < 2:
            continue

        listed = parse_date(c[0])
        if not listed:
            continue

        code_idx = None
        code4 = None
        for i, value in enumerate(c):
            cc = parse_code(value)
            if cc:
                code_idx, code4 = i, cc
                break
        if code4 is None or code_idx is None:
            continue

        # Company name must be in the SAME physical row, before code.
        raw_name = ""
        for value in reversed(c[:code_idx]):
            t = clean_text(value)
            if not t or parse_date(t):
                continue
            if t in ("代表者インタビュー", "創業者インタビュー", "詳細"):
                continue
            raw_name = t
            break

        if not raw_name:
            continue
        if "*" in raw_name:
            # JPX technical listing marker
            continue

        name = normalize_company_name(raw_name)
        if not name:
            continue

        row2 = next_nonempty_row(tr)
        market = next((m for m in MARKETS if any(m in x for x in row2)), "")
        if not market:
            market = next((m for m in MARKETS if any(m in x for x in c)), "")
        if not market:
            continue

        offer = None
        # On JPX archive pages the public/offer price is normally the
        # 4th logical cell on the second physical row.
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
            "archiveMarket": market,
            "publicPriceJPX": offer,
            "archiveSourceUrl": url,
        }
        if not prev or listed > prev["listedDate"]:
            out[code4] = item

    return out


def fetch_jpx_archive_master() -> dict[str, dict[str, Any]]:
    merged = {}
    for url in JPX_ARCHIVES:
        try:
            rows = parse_jpx_archive_page(fetch_html(url), url)
            print(f"[JPX archive] {url.split('/')[-1]} rows={len(rows)}", flush=True)
            for code, item in rows.items():
                old = merged.get(code)
                if not old or item["listedDate"] > old["listedDate"]:
                    merged[code] = item
        except Exception as e:
            print(f"[WARN] JPX archive {url}: {e}", flush=True)

    cutoff = (date.today() - timedelta(days=RETENTION_DAYS)).isoformat()
    today_s = date.today().isoformat()
    merged = {
        c: x for c, x in merged.items()
        if cutoff <= x.get("listedDate", "") <= today_s
    }
    print(f"[JPX archive] retained={len(merged)}", flush=True)
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
# 庶民のIPO: robust discovery + S/A/B/C/D attention
# ------------------------------------------------------------------
def discover_ipokabu_year(html: str, year: int, url: str) -> dict[str, dict[str, Any]]:
    """Discover IPO codes from exact /ipo/<code> links, independent of table layout."""
    soup = BeautifulSoup(html, "html.parser")
    result: dict[str, dict[str, Any]] = {}
    for a in soup.find_all("a", href=True):
        href = clean_text(a.get("href"))
        m = re.search(r"/ipo/(\d{4}[A-Z]?)$", href, re.I)
        if not m:
            continue
        code = m.group(1).upper()
        tr = a.find_parent("tr")
        row_text = clean_text(tr.get_text(" ", strip=True)) if tr else clean_text(a.parent.get_text(" ", strip=True))
        listed = parse_md(row_text, year)
        public = None
        initial = None
        pm = re.search(r"公開価格[：:\s]*([\d,]+)\s*円", row_text)
        im = re.search(r"初値[：:\s]*([\d,]+)\s*円", row_text)
        if pm: public = int(pm.group(1).replace(",", ""))
        if im: initial = int(im.group(1).replace(",", ""))
        old = result.get(code, {})
        result[code] = {
            **old,
            "code4": code,
            "ipokabuName": normalize_company_name(a.get_text(" ", strip=True)) or old.get("ipokabuName"),
            "ipokabuListedDate": listed or old.get("ipokabuListedDate"),
            "publicPriceIpokabu": public if public is not None else old.get("publicPriceIpokabu"),
            "initialPriceIpokabu": initial if initial is not None else old.get("initialPriceIpokabu"),
            "ipoSourceUrl": IPO_KABU_DETAIL_URL.format(code=code),
        }
    return result


def fetch_ipokabu_attention(year: int) -> dict[str, str]:
    """Read 庶民のIPO raw S/A/B/C/D evaluation from dedicated attention pages."""
    result: dict[str, str] = {}
    for grade in IPO_KABU_ATTENTION_GRADES:
        url = f"https://ipokabu.net/data/{year}/attention-{grade.lower()}"
        try:
            soup = BeautifulSoup(fetch_html(url), "html.parser")
        except Exception as e:
            print(f"[WARN] 庶民のIPO attention {year}/{grade}: {e}", flush=True)
            continue
        for a in soup.find_all("a", href=True):
            href = clean_text(a.get("href"))
            m = re.search(r"/ipo/(\d{4}[A-Z]?)$", href, re.I)
            if m:
                result[m.group(1).upper()] = grade
    return result


# ------------------------------------------------------------------
# 庶民のIPO: same-row code association; prices/rating are supplemental
# ------------------------------------------------------------------
def rating_from(v: Any) -> str | None:
    s = clean_text(v).upper().translate(str.maketrans("ＳＡＢＣＤＥ", "SABCDE"))
    m = re.search(r"(?:^|\s)([SABCDE])(?:$|\s)", s)
    return m.group(1) if m else None


def parse_ipokabu_year(html: str, year: int, url: str) -> dict[str, dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    result = {}

    for tr in soup.find_all("tr"):
        c = cells(tr)
        if len(c) < 3:
            continue

        listed = parse_md(c[0], year)
        if not listed:
            continue

        code_idx = None
        code4 = None
        for i, v in enumerate(c[:6]):
            cc = parse_code(v)
            if cc:
                code_idx, code4 = i, cc
                break
        if code4 is None:
            continue

        name = ""
        if code_idx + 1 < len(c):
            name = normalize_company_name(c[code_idx + 1])

        public = None
        # Public price appears on the same row after company.
        for v in c[code_idx + 2:]:
            p = parse_price(v, plain=False)
            if p:
                public = p
                break

        row2 = next_nonempty_row(tr)
        market = next((m for m in MARKETS if any(m in x for x in row2)), "")
        initial = None
        if market:
            seen_market = False
            for v in row2:
                if any(m in v for m in MARKETS):
                    seen_market = True
                    continue
                if seen_market:
                    p = parse_price(v, plain=False)
                    if p:
                        initial = p
                        break

        if not market:
            continue

        result[code4] = {
            "code4": code4,
            "ipokabuName": name or None,
            "ipokabuListedDate": listed,
            "ipokabuMarket": market,
            "publicPriceIpokabu": public,
            "initialPriceIpokabu": initial,
            "ipoEvaluation": rating_from(c[0]),
            "ipoSourceUrl": url,
        }

    return result


def fetch_ipokabu() -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for year, url in IPO_KABU_URLS.items():
        try:
            html = fetch_html(url)
        except Exception as e:
            print(f"[WARN] 庶民のIPO {year}: {e}", flush=True)
            continue
        discovered = discover_ipokabu_year(html, year, url)
        try:
            parsed = parse_ipokabu_year(html, year, url)
        except Exception:
            parsed = {}
        attention = fetch_ipokabu_attention(year)
        codes = set(discovered) | set(parsed) | set(attention)
        for code in codes:
            d = discovered.get(code, {})
            p = parsed.get(code, {})
            grade = attention.get(code)
            old = merged.get(code, {})
            merged[code] = {
                **old,
                "code4": code,
                "ipokabuName": d.get("ipokabuName") or p.get("ipokabuName") or old.get("ipokabuName"),
                "ipokabuListedDate": d.get("ipokabuListedDate") or p.get("ipokabuListedDate") or old.get("ipokabuListedDate"),
                "ipokabuMarket": p.get("ipokabuMarket") or old.get("ipokabuMarket"),
                "publicPriceIpokabu": d.get("publicPriceIpokabu") if d.get("publicPriceIpokabu") is not None else p.get("publicPriceIpokabu", old.get("publicPriceIpokabu")),
                "initialPriceIpokabu": d.get("initialPriceIpokabu") if d.get("initialPriceIpokabu") is not None else p.get("initialPriceIpokabu", old.get("initialPriceIpokabu")),
                "ipoEvaluation": grade or old.get("ipoEvaluation") or p.get("ipoEvaluation"),
                "ipoSourceUrl": IPO_KABU_DETAIL_URL.format(code=code),
            }
        print(f"[庶民のIPO] {year} discovered={len(discovered)} table={len(parsed)} attention={len(attention)}", flush=True)
    retention_cutoff=(date.today()-timedelta(days=RETENTION_DAYS)).isoformat()
    today_s=date.today().isoformat()
    merged={c:x for c,x in merged.items() if (not x.get("ipokabuListedDate") or retention_cutoff <= x.get("ipokabuListedDate","") <= today_s)}
    print(f"[庶民のIPO] retained={len(merged)}", flush=True)
    return merged


# ------------------------------------------------------------------
# Existing JSON: history only, never master-name source
# ------------------------------------------------------------------
def load_existing():
    if not OUT.exists():
        return {}, True
    try:
        raw = json.loads(OUT.read_text(encoding="utf-8"))
    except Exception:
        return {}, True

    items = raw.get("ipos", []) if isinstance(raw, dict) else raw if isinstance(raw, list) else []
    by_code = {}
    for x in items:
        if not isinstance(x, dict):
            continue
        code = clean_text(x.get("code4") or x.get("code")).replace(".T", "").upper()
        if re.fullmatch(r"\d{4}[A-Z]?", code):
            by_code[code] = x

    is_v11 = bool(by_code) and all(
        x.get("_schemaVersion") == SCHEMA_VERSION
        for x in list(by_code.values())[:min(20, len(by_code))]
    )
    return by_code, (not is_v11)


# ------------------------------------------------------------------
# Master construction
# ------------------------------------------------------------------
def build_master(archive, ipokabu, official) -> list[dict[str, Any]]:
    """
    Name/code policy:
      1. If JPX current lookup verifies a code, official name+market ALWAYS win.
      2. If not currently listed, use same-row JPX archive name.
      3. Never use existing JSON company name as master data.
    """
    codes = set(archive) | set(ipokabu)
    out = []

    retention_cutoff = (date.today() - timedelta(days=RETENTION_DAYS)).isoformat()
    discovery_cutoff = (date.today() - timedelta(days=IPO_DISCOVERY_DAYS)).isoformat()
    today_s = date.today().isoformat()

    for code in codes:
        a = archive.get(code, {})
        k = ipokabu.get(code, {})
        o = official.get(code, {})

        listed = a.get("listedDate") or k.get("ipokabuListedDate")
        if not listed or not (retention_cutoff <= listed <= today_s):
            continue

        if o:
            name = o["officialName"]
            market = o["officialMarket"]
            source = "JPX official listed-company search"
            verified = True
        elif a:
            name = normalize_company_name(a.get("archiveName"))
            market = a.get("archiveMarket")
            source = "JPX new-listing archive"
            verified = False
        else:
            # Do NOT trust an unverified name/code pair from a third-party source as master.
            continue

        if market not in MARKETS or not name or "*" in name:
            continue

        out.append({
            "code4": code,
            "name": normalize_company_name(name),
            "market": market,
            "listedDate": listed,
            "searchEligible3y": listed >= discovery_cutoff,
            "masterSource": source,
            "officialCodeNameVerified": verified,
            "publicPriceJPX": a.get("publicPriceJPX"),
        })

    out.sort(key=lambda x: x["listedDate"], reverse=True)

    if len(out) < 100:
        raise RuntimeError(f"Master only has {len(out)} records; latest.json left unchanged.")

    return out


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
    print(f"[CACHE] existing={len(old)} mode={'FULL BACKFILL' if full_backfill else 'INCREMENTAL'}", flush=True)

    archive = fetch_jpx_archive_master()
    ipokabu = fetch_ipokabu()

    candidate_codes = sorted(set(archive) | set(ipokabu))
    official = verify_codes_with_jpx(candidate_codes)
    print(f"[JPX verify] exact current mappings={len(official)}/{len(candidate_codes)}", flush=True)

    master = build_master(archive, ipokabu, official)
    print(
        f"[MASTER] total={len(master)} verified={sum(x['officialCodeNameVerified'] for x in master)} "
        f"3y={sum(x['searchEligible3y'] for x in master)}",
        flush=True,
    )

    # Whole-universe sanity checks.
    by_code = {x["code4"]: x["name"] for x in master}
    known = {
        "8729": "ソニーフィナンシャルグループ",
        "332A": "ミーク",
        "265A": "Ｈｍｃｏｍｍ",
        "485A": "パワーエックス",
        "471A": "ＮＳグループ",
    }
    # Accept Japanese/ASCII width variants for Hmcomm/NS.
    aliases = {
        "265A": ("Hmcomm", "Ｈｍｃｏｍｍ", "Ｈmcomm"),
        "471A": ("NSグループ", "ＮＳグループ"),
    }
    for code, expected in known.items():
        if code not in by_code:
            continue
        if code in aliases:
            assert any(a.lower() in by_code[code].lower() for a in aliases[code]), \
                f"{code} mismatch: {by_code[code]}"
        else:
            assert expected.lower() in by_code[code].lower(), \
                f"{code} mismatch: {by_code[code]}"

    today = date.today()
    start = (
        today - timedelta(days=RETENTION_DAYS + 10)
        if full_backfill else
        today - timedelta(days=RECENT_REFRESH_DAYS)
    ).isoformat()
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
        )
        yf_initial = listing_initial(hist, m["listedDate"])
        initial = (
            k.get("initialPriceIpokabu")
            or yf_initial
            or prev.get("initialPrice")
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

            # New canonical names:
            "publicPrice": public,
            "initialPrice": initial,

            # Legacy app-compatible aliases. The current HTML expects these names:
            "ipoPrice": public,
            "firstDayPrice": initial,

            "publicPriceSource": (
                "JPX" if m.get("publicPriceJPX") else
                "庶民のIPO" if k.get("publicPriceIpokabu") else
                "existing"
            ),
            "initialPriceSource": (
                "庶民のIPO" if k.get("initialPriceIpokabu") else
                "yfinance listing-day Open" if yf_initial is not None else
                "existing"
            ),
            "initialReturnPct": pct(initial, public),
            "ipoEvaluation": (
                k.get("ipoEvaluation")
                or prev.get("ipoEvaluation")
                or prev.get("ipoAttention")
                or prev.get("ipoRating")
            ),
            "ipoSourceUrl": k.get("ipoSourceUrl") or prev.get("ipoSourceUrl"),

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
            "dataSource": "JPX + 庶民のIPO + yfinance" + (" + J-Quants" if financials else ""),
            "dataRetrievedAt": started,
        }
        items.append(item)

        if i % 25 == 0 or i == len(master):
            print(f"[BUILD] {i}/{len(master)}", flush=True)

    public_count = sum(x.get("ipoPrice") is not None for x in items)
    initial_count = sum(x.get("firstDayPrice") is not None for x in items)
    print(f"[IPO prices] public={public_count}/{len(items)} initial={initial_count}/{len(items)}", flush=True)

    # Final integrity gates.
    codes = [x["code4"] for x in items]
    assert len(codes) == len(set(codes)), "duplicate codes"
    assert len(items) >= 100, f"too few records: {len(items)}"
    assert all("インタビュー" not in x["name"] for x in items), "interview label remains"

    # Any currently-listed record that was successfully looked up must match
    # the official mapping because the official result overwrote scraped names.
    for x in items:
        if x["code4"] in official:
            assert x["name"] == official[x["code4"]]["officialName"]
            assert x["market"] == official[x["code4"]]["officialMarket"]

    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(OUT)

    print(
        f"[DONE] records={len(items)} verified={sum(x['officialCodeNameVerified'] for x in items)} "
        f"file={OUT.stat().st_size/1024/1024:.2f}MB "
        f"mode={'full-backfill' if full_backfill else 'incremental'}",
        flush=True,
    )


if __name__ == "__main__":
    main()
