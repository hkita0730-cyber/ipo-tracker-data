#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
IPO Tracker FAST v6

Key change from v5:
- Do not depend on JPX / 庶民のIPO HTML table structure.
- Parse the visible text stream instead. This is robust against rowspan/colspan,
  mobile/desktop duplicate tables, and extra interview links.
- JPX + 庶民のIPO are both used; JPX is preferred for official master data.
- Existing latest.json is not deleted. On the first v6 run we do a full backfill,
  then later runs are incremental.
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

SCHEMA_VERSION = 6

IPO_DISCOVERY_DAYS = 1095       # 3 years
HISTORY_RETENTION_DAYS = 1460   # 4 years
RECENT_REFRESH_DAYS = 120
FINANCIAL_REFRESH_DAYS = 7

MARKETS = ("プライム", "スタンダード", "グロース")

JPX_URLS = [
    "https://www.jpx.co.jp/listing/stocks/new/index.html",
    "https://www.jpx.co.jp/listing/stocks/new/00-archives-01.html",
    "https://www.jpx.co.jp/listing/stocks/new/00-archives-02.html",
    "https://www.jpx.co.jp/listing/stocks/new/00-archives-03.html",
]

IPO_KABU_URLS = {
    2026: "https://ipokabu.net/ipo/list2026",
    2025: "https://ipokabu.net/ipo/list2025",
    2024: "https://ipokabu.net/ipo/list2024",
    2023: "https://ipokabu.net/ipo/list2023",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.7,en;q=0.5",
}

FULL_DATE_RE = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$")
MD_RE = re.compile(r"^(\d{1,2})/(\d{1,2})$")
CODE_RE = re.compile(r"^[\[\]〖〗\s]*(\d{4}[A-Z]?)[\[\]〖〗\s]*$")
RATING_RE = re.compile(r"^[ＳＡＢＣＤＥSABCDE]$")

BROKER_WORDS = (
    "証券", "證券", "マネックス", "SBI", "楽天", "松井", "野村", "大和",
    "みずほ", "岡三", "東海東京", "岩井コスモ", "むさし", "丸三",
)

IGNORE_NAME_WORDS = (
    "代表者インタビュー", "創業者インタビュー", "社長インタビュー",
    "経営者インタビュー", "会社概要", "確認書", "詳細", "Iの部",
    "CG報告書", "決算短信",
)


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    s = str(value).replace("\u3000", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def normalize_company_name(value: Any) -> str:
    s = clean_text(value)
    for label in ("代表者インタビュー", "創業者インタビュー", "社長インタビュー", "経営者インタビュー"):
        s = s.replace(label, "")
    s = re.sub(r"\s+", " ", s).strip(" |　")
    return s


def parse_full_date_line(s: str) -> str | None:
    s = clean_text(s).strip("（）() ")
    m = FULL_DATE_RE.fullmatch(s)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


def parse_md_line(s: str, year: int) -> str | None:
    s = clean_text(s).strip()
    m = MD_RE.fullmatch(s)
    if not m:
        return None
    try:
        return date(year, int(m.group(1)), int(m.group(2))).isoformat()
    except ValueError:
        return None


def parse_code_line(s: str) -> str | None:
    s = clean_text(s).upper()
    m = CODE_RE.fullmatch(s)
    return m.group(1) if m else None


def parse_yen(s: str) -> int | None:
    s = clean_text(s)
    m = re.fullmatch(r"(-?[\d,]+(?:\.\d+)?)円", s)
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
        return int(round(v)) if v > 0 else None
    except Exception:
        return None


def normalize_rating(s: str) -> str | None:
    s = clean_text(s).upper().translate(str.maketrans("ＳＡＢＣＤＥ", "SABCDE"))
    return s if re.fullmatch(r"[SABCDE]", s) else None


def fetch_html(url: str, timeout: int = 40) -> str:
    last = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            # JPX sometimes comes back with a legacy Japanese charset.
            if r.apparent_encoding:
                r.encoding = r.apparent_encoding
            return r.text
        except Exception as e:
            last = e
            if attempt < 2:
                time.sleep(2 + attempt * 2)
    raise RuntimeError(f"fetch failed: {url}: {last}")


def visible_lines(html: str) -> list[str]:
    """
    Turn HTML into the same visible text order a browser/search engine sees.
    We intentionally ignore table geometry.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    raw = soup.get_text("\n", strip=True)
    lines = []
    for part in raw.splitlines():
        t = clean_text(part)
        if t:
            lines.append(t)
    return lines


# ---------------------------------------------------------------------------
# JPX: text-stream parser
# ---------------------------------------------------------------------------
def plausible_jpx_name(line: str) -> bool:
    t = clean_text(line)
    if not t:
        return False
    if parse_full_date_line(t):
        return False
    if parse_code_line(t):
        return False
    if any(m in t for m in MARKETS):
        return False
    if any(x in t for x in IGNORE_NAME_WORDS):
        return False
    if re.fullmatch(r"[-–—\d,.～〜()（）OA]+", t):
        return False
    return True


def parse_jpx_text(html: str, source_url: str) -> list[dict[str, Any]]:
    lines = visible_lines(html)
    out: dict[tuple[str, str], dict[str, Any]] = {}

    # A JPX logical item is:
    # listing date / approval date / company / [interview] / code / ...
    # market appears shortly afterwards on the second visual row.
    i = 0
    while i < len(lines):
        listed = parse_full_date_line(lines[i])
        if not listed:
            i += 1
            continue

        # Ignore approval-date lines: a listing-date line is followed nearby by another
        # date plus a security code; an approval-date line usually has no second date.
        end = min(len(lines), i + 28)
        block = lines[i:end]

        code_pos = None
        code4 = None
        for k, line in enumerate(block[1:], 1):
            c = parse_code_line(line)
            if c:
                code_pos = k
                code4 = c
                break

        if code4 is None:
            i += 1
            continue

        # Search market in the same block.
        market = ""
        for line in block:
            for m in MARKETS:
                if line == m or m in line:
                    market = m
                    break
            if market:
                break
        if not market:
            i += 1
            continue

        # Company name is the first plausible text between listing date and code,
        # after skipping the approval date and interview label.
        raw_name = ""
        for line in block[1:code_pos]:
            if parse_full_date_line(line):
                continue
            if plausible_jpx_name(line):
                raw_name = line
                break

        if not raw_name:
            i += 1
            continue

        # JPX uses * after company name for technical listings.
        if "*" in raw_name:
            i += 1
            continue

        name = normalize_company_name(raw_name)
        if not name or "インタビュー" in name:
            i += 1
            continue

        out[(code4, listed)] = {
            "code4": code4,
            "name": name,
            "market": market,
            "listedDate": listed,
            "jpxSourceUrl": source_url,
        }
        i += 1

    return list(out.values())


def parse_jpx() -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    downloaded = 0

    for url in JPX_URLS:
        try:
            html = fetch_html(url)
            downloaded += 1
            rows = parse_jpx_text(html, url)
            print(f"[JPX] {url.split('/')[-1]} text-parsed={len(rows)}", flush=True)
            for x in rows:
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
        and x.get("name")
    }

    for x in merged.values():
        x["name"] = normalize_company_name(x["name"])

    print(f"[JPX] usable 3-year records={len(merged)}", flush=True)
    return merged


# ---------------------------------------------------------------------------
# 庶民のIPO: text-stream parser
# ---------------------------------------------------------------------------
def plausible_ipokabu_name(line: str) -> bool:
    t = clean_text(line)
    if not t:
        return False
    if parse_code_line(t):
        return False
    if RATING_RE.fullmatch(t):
        return False
    if any(m in t for m in MARKETS):
        return False
    if any(b in t for b in BROKER_WORDS):
        return False
    if parse_yen(t) is not None:
        return False
    if "％" in t or "%" in t or "倍" in t:
        return False
    return True


def parse_ipokabu_text(html: str, year: int, source_url: str) -> dict[str, dict[str, Any]]:
    lines = visible_lines(html)
    result: dict[str, dict[str, Any]] = {}

    # The desktop table appears first. Later the page repeats a mobile table.
    # Parse all and deduplicate by security code.
    i = 0
    while i < len(lines):
        listed = parse_md_line(lines[i], year)
        if not listed:
            i += 1
            continue

        # Look only until the next month/day row or max 45 lines.
        end = min(len(lines), i + 45)
        for j in range(i + 1, end):
            if parse_md_line(lines[j], year):
                end = j
                break
        block = lines[i:end]

        rating = None
        rating_pos = None
        for k, line in enumerate(block[1:8], 1):
            r = normalize_rating(line)
            if r:
                rating = r
                rating_pos = k
                break

        code4 = None
        code_pos = None
        for k, line in enumerate(block[1:14], 1):
            c = parse_code_line(line)
            if c:
                code4 = c
                code_pos = k
                break
        if not code4:
            i += 1
            continue

        market = ""
        market_pos = None
        for k, line in enumerate(block):
            if line in MARKETS:
                market = line
                market_pos = k
                break
        if not market:
            i += 1
            continue

        # Company is normally immediately after code in desktop table.
        name = ""
        if code_pos is not None:
            for line in block[code_pos + 1: min(len(block), code_pos + 8)]:
                if plausible_ipokabu_name(line):
                    name = normalize_company_name(line)
                    break

        # Public price: first positive yen cell after code and before market.
        public = None
        if code_pos is not None:
            stop = market_pos if market_pos is not None else len(block)
            for line in block[code_pos + 1:stop]:
                p = parse_yen(line)
                if p is not None:
                    public = p
                    break

        # Initial price: first positive yen cell after market.
        initial = None
        if market_pos is not None:
            for line in block[market_pos + 1:]:
                p = parse_yen(line)
                if p is not None:
                    initial = p
                    break

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

        i = max(i + 1, end)

    return result


def parse_ipokabu() -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}

    for year, url in IPO_KABU_URLS.items():
        try:
            html = fetch_html(url)
            rows = parse_ipokabu_text(html, year, url)
            print(f"[庶民のIPO] {year} text-parsed={len(rows)}", flush=True)
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
    old: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Priority:
      JPX -> 庶民のIPO -> existing JSON only as last-resort fallback.

    Old company names are sanitized before fallback, so interview labels are removed.
    """
    codes = set(jpx) | set(ipokabu)

    # Existing JSON can rescue a temporarily missing web row, but only for records
    # whose listing date is still inside the desired 3-year IPO window.
    cutoff = (date.today() - timedelta(days=IPO_DISCOVERY_DAYS)).isoformat()
    today_s = date.today().isoformat()

    for c, x in old.items():
        d = clean_text(x.get("listedDate"))
        m = clean_text(x.get("market"))
        if cutoff <= d <= today_s and m in MARKETS:
            codes.add(c)

    out = []

    for code4 in codes:
        j = jpx.get(code4, {})
        k = ipokabu.get(code4, {})
        p = old.get(code4, {})

        listed = j.get("listedDate") or k.get("listedDateIpokabu") or p.get("listedDate")
        market = j.get("market") or k.get("marketIpokabu") or p.get("market")
        name = normalize_company_name(
            j.get("name") or k.get("nameIpokabu") or p.get("name") or code4
        )

        if not listed or not (cutoff <= listed <= today_s):
            continue
        if market not in MARKETS:
            continue
        if "*" in name:
            continue
        if "インタビュー" in name:
            name = normalize_company_name(name)

        source = (
            "JPX" if j
            else "庶民のIPO fallback" if k
            else "existing JSON fallback"
        )

        out.append({
            "code4": code4,
            "name": name,
            "market": market,
            "listedDate": listed,
            "masterSource": source,
        })

    out.sort(key=lambda x: x["listedDate"], reverse=True)

    # Only fail on a catastrophic scrape. In normal partial-site failures,
    # the union/fallback allows the workflow to complete.
    if len(out) < 5:
        raise RuntimeError(
            f"Only {len(out)} IPO master records could be built; latest.json left unchanged."
        )

    print(
        "[MASTER] total={} JPX={} ipokabu-fallback={} old-fallback={}".format(
            len(out),
            sum(x["masterSource"] == "JPX" for x in out),
            sum(x["masterSource"] == "庶民のIPO fallback" for x in out),
            sum(x["masterSource"] == "existing JSON fallback" for x in out),
        ),
        flush=True,
    )
    return out


# ---------------------------------------------------------------------------
# Existing JSON
# ---------------------------------------------------------------------------
def load_existing() -> tuple[dict[str, dict[str, Any]], bool]:
    if not OUT.exists():
        print("[CACHE] no latest.json -> FULL BACKFILL", flush=True)
        return {}, True

    try:
        raw = json.loads(OUT.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[WARN] latest.json unreadable: {e}", flush=True)
        return {}, True

    items = raw.get("ipos", []) if isinstance(raw, dict) else raw if isinstance(raw, list) else []
    by_code: dict[str, dict[str, Any]] = {}

    for x in items:
        if not isinstance(x, dict):
            continue
        c = clean_text(x.get("code4") or x.get("code")).replace(".T", "")
        m = re.search(r"(\d{4}[A-Z]?)", c.upper())
        if m:
            by_code[m.group(1)] = x

    is_v6 = bool(by_code) and all(
        x.get("_schemaVersion") == SCHEMA_VERSION
        for x in list(by_code.values())[: min(20, len(by_code))]
    )

    full = not is_v6
    print(f"[CACHE] existing={len(by_code)} mode={'FULL BACKFILL' if full else 'INCREMENTAL'}", flush=True)
    return by_code, full


# ---------------------------------------------------------------------------
# yfinance prices
# ---------------------------------------------------------------------------
def normalize_price_frame(df: pd.DataFrame) -> list[dict[str, Any]]:
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


def download_prices(symbols: list[str], start: str, end: str) -> dict[str, list[dict[str, Any]]]:
    result = {}
    chunk_size = 40

    for pos in range(0, len(symbols), chunk_size):
        chunk = symbols[pos:pos + chunk_size]
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

    print(f"[yfinance] tickers with data={len(result)}/{len(symbols)}", flush=True)
    return result


def merge_history(old: Any, new: Any) -> list[dict[str, Any]]:
    by_date = {}
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


def milestone(history: list[dict[str, Any]], listed: str, days: int):
    if not history:
        return None, None
    target = date.fromisoformat(listed) + timedelta(days=days)
    limit = target + timedelta(days=15)
    for x in history:
        try:
            d = date.fromisoformat(str(x["date"]))
        except Exception:
            continue
        if target <= d <= limit:
            return x.get("close"), x.get("date")
        if d > limit:
            break
    return None, None


def pct(current: Any, base: Any):
    try:
        if current is None or base in (None, 0):
            return None
        return round((float(current) / float(base) - 1.0) * 100.0, 2)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Optional J-Quants financials
# ---------------------------------------------------------------------------
def fetch_financials_nonfatal(code4: str, api_key: str) -> list[dict[str, Any]]:
    if not api_key or ":" in api_key:
        # Do not risk failing/hanging on an old credential format.
        return []

    headers = {"x-api-key": api_key}
    for endpoint in ("summary", "statements"):
        url = f"https://api.jquants.com/v2/fins/{endpoint}?code={code4}0"
        try:
            r = requests.get(url, headers=headers, timeout=15)
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
    try:
        return (date.today() - date.fromisoformat(stamp[:10])).days >= FINANCIAL_REFRESH_DAYS
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    started = datetime.now().isoformat(timespec="seconds")
    old, full_backfill = load_existing()

    jpx = parse_jpx()
    ipokabu = parse_ipokabu()
    master = build_master(jpx, ipokabu, old)

    today = date.today()
    start = (
        today - timedelta(days=HISTORY_RETENTION_DAYS + 10)
        if full_backfill
        else today - timedelta(days=RECENT_REFRESH_DAYS)
    ).isoformat()
    end = (today + timedelta(days=1)).isoformat()

    symbols = [f"{x['code4']}.T" for x in master]
    price_map = download_prices(symbols, start, end)

    jq_key = os.environ.get("JQUANTS_API_KEY", "").strip()
    jq_budget = 6
    jq_attempts = 0

    items = []

    for idx, m in enumerate(master, 1):
        code4 = m["code4"]
        prev = old.get(code4, {})
        ipo = ipokabu.get(code4, {})
        symbol = f"{code4}.T"

        history = merge_history(prev.get("priceHistory", []), price_map.get(symbol, []))

        current = history[-1].get("close") if history else prev.get("currentPrice")
        current_date = history[-1].get("date") if history else prev.get("priceAsOfDate")

        public = ipo.get("publicPrice") or prev.get("publicPrice")
        initial = ipo.get("initialPrice") or prev.get("initialPrice")

        financials = prev.get("financials", [])
        fin_updated = prev.get("financialsUpdatedAt")

        if jq_key and jq_attempts < jq_budget and should_refresh_financials(prev):
            fresh = fetch_financials_nonfatal(code4, jq_key)
            jq_attempts += 1
            if fresh:
                financials = fresh
                fin_updated = today.isoformat()

        miles = {}
        for days in (30, 90, 180, 365, 730, 1095):
            p, d = milestone(history, m["listedDate"], days)
            miles[f"price{days}d"] = p
            miles[f"price{days}dDate"] = d

        name = normalize_company_name(m["name"])
        if "インタビュー" in name:
            name = normalize_company_name(name)

        items.append({
            "_schemaVersion": SCHEMA_VERSION,
            "code": code4,
            "code4": code4,
            "name": name,
            "market": m["market"],
            "listedDate": m["listedDate"],
            "masterSource": m["masterSource"],

            "publicPrice": public,
            "initialPrice": initial,
            "initialReturnPct": pct(initial, public),
            "ipoRating": ipo.get("ipoRating") or prev.get("ipoRating"),
            "ipoRatingSource": ipo.get("ipoRatingSource") or prev.get("ipoRatingSource"),
            "ipoSourceUrl": ipo.get("ipoSourceUrl") or prev.get("ipoSourceUrl"),

            "currentPrice": current,
            "priceAsOfDate": current_date,
            "priceVsPublicPct": pct(current, public),
            "priceVsInitialPct": pct(current, initial),

            **miles,

            "priceHistory": history,
            "financials": financials if isinstance(financials, list) else [],
            "financialsUpdatedAt": fin_updated,
            "marketCap": prev.get("marketCap"),
            "currentPER": prev.get("currentPER"),
            "dataRetrievedAt": started,
        })

        if idx % 25 == 0 or idx == len(master):
            print(f"[BUILD] {idx}/{len(master)}", flush=True)

    # If yfinance is temporarily unavailable, preserving old history is allowed.
    # But we still require a valid master and clean names.
    if len(items) < 5:
        raise RuntimeError("Too few final records; latest.json left unchanged.")
    if any("インタビュー" in clean_text(x.get("name")) for x in items):
        raise RuntimeError("Interview label remains in final data; latest.json left unchanged.")

    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(OUT)

    print(
        f"[DONE] records={len(items)} file={OUT.stat().st_size/1024/1024:.2f}MB "
        f"mode={'full-backfill' if full_backfill else 'incremental'}",
        flush=True,
    )


if __name__ == "__main__":
    main()
