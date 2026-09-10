#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
IPO Tracker FAST v4
===================
Purpose:
- Rebuild IPO master metadata from JPX on every run (so bad company names are corrected).
- IPO discovery window: 3 years.
- Price-history retention: up to 4 years.
- First v4 run: full price backfill.
- Later runs: recent-price incremental refresh only.
- 庶民のIPO: rating, public price, initial price.
- yfinance: batched daily prices.
- J-Quants: optional financial refresh; any failure is non-fatal.
- Output stays a JSON ARRAY for compatibility with the existing app.

Important:
The script never reuses an old company name when JPX provides a current/archived name.
This means existing "...代表者インタビュー" contamination is cleaned automatically
without deleting docs/latest.json.
"""

from __future__ import annotations

import io
import json
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

SCHEMA_VERSION = 4

IPO_DISCOVERY_DAYS = 1095       # 3 years
HISTORY_RETENTION_DAYS = 1460   # 4 years
RECENT_REFRESH_DAYS = 120
FINANCIAL_REFRESH_DAYS = 7

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
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 Chrome/126 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
}

DATE_RE = re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})")
CODE_RE = re.compile(r"(?<![0-9A-Z])(\d{4}[A-Z]?)(?![0-9A-Z])")
MARKETS = ("プライム", "スタンダード", "グロース")

INTERVIEW_RE = re.compile(
    r"\s*(?:代表者|創業者|社長|経営者)\s*インタビュー.*$",
    re.IGNORECASE,
)


def clean_text(value: Any) -> str:
    s = str(value if value is not None else "")
    s = s.replace("\u3000", " ")
    return re.sub(r"\s+", " ", s).strip()


def normalize_company_name(value: Any) -> str:
    """Remove JPX link labels accidentally concatenated into company names."""
    s = clean_text(value)
    s = s.replace("代表者インタビュー", "")
    s = s.replace("創業者インタビュー", "")
    s = s.replace("社長インタビュー", "")
    s = s.replace("経営者インタビュー", "")
    s = INTERVIEW_RE.sub("", s)
    s = s.replace("*", " ")
    s = re.sub(r"\s+", " ", s).strip(" |　")
    return s


def parse_iso_date(value: Any) -> str | None:
    m = DATE_RE.search(clean_text(value))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


def parse_yen(value: Any) -> int | None:
    s = clean_text(value)
    if not s or s in ("-", "—", "nan", "None"):
        return None
    # Avoid treating percentage/profit columns as the IPO price.
    m = re.search(r"(-?[\d,]+(?:\.\d+)?)\s*円", s)
    if not m:
        if not re.fullmatch(r"[\d,]+", s):
            return None
        raw = s
    else:
        raw = m.group(1)
    try:
        v = float(raw.replace(",", ""))
        if v <= 0:
            return None
        return int(round(v))
    except Exception:
        return None


def fetch_html(url: str, timeout: int = 35) -> str:
    last = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            # requests can guess JPX encoding incorrectly; apparent_encoding is safer here.
            if r.apparent_encoding:
                r.encoding = r.apparent_encoding
            return r.text
        except Exception as e:
            last = e
            if attempt < 2:
                time.sleep(2 + attempt * 2)
    raise RuntimeError(f"fetch failed: {url}: {last}")


def flatten_columns(df: pd.DataFrame) -> list[str]:
    out = []
    for c in df.columns:
        if isinstance(c, tuple):
            parts = []
            for p in c:
                t = clean_text(p)
                if t and not t.startswith("Unnamed") and t not in parts:
                    parts.append(t)
            out.append(" / ".join(parts))
        else:
            out.append(clean_text(c))
    return out


def find_col(columns: list[str], *needles: str) -> int | None:
    for i, c in enumerate(columns):
        if all(n in c for n in needles):
            return i
    return None


def parse_jpx_with_pandas(html: str, source_url: str) -> list[dict[str, Any]]:
    """Primary JPX parser. pandas.read_html expands rowspan/colspan into logical rows."""
    out: list[dict[str, Any]] = []
    try:
        tables = pd.read_html(io.StringIO(html))
    except Exception:
        return out

    for df in tables:
        if df.empty:
            continue
        cols = flatten_columns(df)

        date_i = find_col(cols, "上場日")
        name_i = find_col(cols, "会社名")
        code_i = find_col(cols, "コード")
        market_i = find_col(cols, "市場区分")

        if None in (date_i, name_i, code_i, market_i):
            continue

        price_i = find_col(cols, "公募・売出価格")
        if price_i is None:
            price_i = find_col(cols, "売出価格")

        for _, row in df.iterrows():
            vals = list(row.values)
            listed = parse_iso_date(vals[date_i])
            if not listed:
                continue

            raw_code = clean_text(vals[code_i]).replace(" ", "")
            cm = CODE_RE.search(raw_code)
            if not cm:
                continue
            code4 = cm.group(1)

            raw_name = clean_text(vals[name_i])
            # JPX marks technical listings with "*". Exclude them.
            if "*" in raw_name:
                continue
            name = normalize_company_name(raw_name)
            if not name:
                continue

            raw_market = clean_text(vals[market_i])
            market = next((m for m in MARKETS if m in raw_market), "")
            if not market:
                continue

            price = parse_yen(vals[price_i]) if price_i is not None else None

            out.append({
                "code4": code4,
                "name": name,
                "market": market,
                "listedDate": listed,
                "publicPriceJPX": price,
                "jpxSourceUrl": source_url,
            })

    return out


def parse_jpx_with_bs4(html: str, source_url: str) -> list[dict[str, Any]]:
    """
    Fallback parser for JPX.
    Uses table-row state instead of assuming market is in the same physical <tr>.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict[str, Any]] = []

    for table in soup.find_all("table"):
        pending: dict[str, Any] | None = None

        for tr in table.find_all("tr"):
            cells = [clean_text(x.get_text(" ", strip=True)) for x in tr.find_all(["th", "td"])]
            row_text = " | ".join(cells)

            # If a prior physical row had date/name/code, market may appear in this row.
            if pending:
                market = next((m for m in MARKETS if m in row_text), "")
                if market:
                    pending["market"] = market
                    out.append(pending)
                    pending = None

            listed = None
            for c in cells:
                d = parse_iso_date(c)
                if d:
                    listed = d
                    break
            if not listed:
                continue

            # Prefer an anchor whose visible text looks like a security code.
            code4 = None
            for a in tr.find_all("a"):
                cm = CODE_RE.fullmatch(clean_text(a.get_text(" ", strip=True)).replace(" ", ""))
                if cm:
                    code4 = cm.group(1)
                    break
            if not code4:
                for c in cells:
                    cm = CODE_RE.fullmatch(c.replace(" ", ""))
                    if cm:
                        code4 = cm.group(1)
                        break
            if not code4:
                continue

            # Company name: choose the company link, explicitly excluding interview/YouTube links.
            raw_name = ""
            for a in tr.find_all("a"):
                txt = clean_text(a.get_text(" ", strip=True))
                href = clean_text(a.get("href", ""))
                if not txt:
                    continue
                if "インタビュー" in txt or "youtu" in href.lower():
                    continue
                if CODE_RE.fullmatch(txt.replace(" ", "")):
                    continue
                # Ignore navigation labels.
                if txt in ("詳細", "会社概要", "確認書"):
                    continue
                raw_name = txt
                break

            # Fallback: locate text before code while excluding interview labels.
            if not raw_name:
                code_pos = None
                for i, c in enumerate(cells):
                    if CODE_RE.fullmatch(c.replace(" ", "")):
                        code_pos = i
                        break
                if code_pos is not None:
                    for c in reversed(cells[:code_pos]):
                        if "インタビュー" in c or not c:
                            continue
                        if parse_iso_date(c):
                            continue
                        raw_name = c
                        break

            if "*" in row_text and "*" in raw_name:
                continue
            name = normalize_company_name(raw_name)
            if not name:
                continue

            market = next((m for m in MARKETS if m in row_text), "")

            # Public/offer price can be in the same logical row; this is only a fallback.
            public = None
            yen_values = [parse_yen(c) for c in cells]
            yen_values = [v for v in yen_values if v]
            if yen_values:
                public = yen_values[-1]

            item = {
                "code4": code4,
                "name": name,
                "market": market,
                "listedDate": listed,
                "publicPriceJPX": public,
                "jpxSourceUrl": source_url,
            }
            if market:
                out.append(item)
            else:
                pending = item

    return out


def parse_jpx() -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    ok_pages = 0

    for url in JPX_URLS:
        try:
            html = fetch_html(url)
            ok_pages += 1
        except Exception as e:
            print(f"[WARN] JPX fetch: {e}", flush=True)
            continue

        rows = parse_jpx_with_pandas(html, url)
        if not rows:
            rows = parse_jpx_with_bs4(html, url)

        print(f"[JPX] {url.split('/')[-1]} parsed={len(rows)}", flush=True)

        for x in rows:
            # Always normalize again before storage.
            x["name"] = normalize_company_name(x.get("name"))
            if x["code4"] not in merged:
                merged[x["code4"]] = x

    if ok_pages == 0:
        raise RuntimeError("All JPX pages failed to download.")

    today = date.today().isoformat()
    cutoff = (date.today() - timedelta(days=IPO_DISCOVERY_DAYS)).isoformat()

    rows = [
        x for x in merged.values()
        if cutoff <= x.get("listedDate", "") <= today
        and x.get("market") in MARKETS
        and x.get("name")
    ]
    rows.sort(key=lambda x: x["listedDate"], reverse=True)

    # Guard against silently writing an empty/obviously broken file.
    if len(rows) < 80:
        raise RuntimeError(
            f"JPX parser produced only {len(rows)} IPOs in the 3-year window. "
            "Refusing to overwrite latest.json."
        )

    bad_names = [x for x in rows if "インタビュー" in x["name"]]
    if bad_names:
        raise RuntimeError(f"JPX company-name validation failed: {bad_names[:3]}")

    print(f"[JPX] 3-year target IPOs={len(rows)}", flush=True)
    return rows


def normalize_rating(value: Any) -> str | None:
    s = clean_text(value).upper()
    # Convert full-width A-E and strip decorations.
    trans = str.maketrans("ＡＢＣＤＥＳ", "ABCDES")
    s = s.translate(trans)
    m = re.search(r"(?<![A-Z])([SABCDE])(?:評価)?(?![A-Z])", s)
    return m.group(1) if m else None


def parse_ipokabu_table(html: str, year: int, source_url: str) -> dict[str, dict[str, Any]]:
    """
    Parse the desktop table. pandas handles the two-row header and rowspans.
    """
    result: dict[str, dict[str, Any]] = {}
    try:
        tables = pd.read_html(io.StringIO(html))
    except Exception:
        tables = []

    for df in tables:
        if df.empty:
            continue
        cols = flatten_columns(df)
        joined = " | ".join(cols)
        if "公開価格" not in joined or ("証券コード" not in joined and "銘柄" not in joined):
            continue

        code_i = find_col(cols, "証券コード")
        name_i = find_col(cols, "銘柄")
        public_i = find_col(cols, "公開価格")
        initial_i = find_col(cols, "初値")
        date_i = find_col(cols, "上場日")
        market_i = find_col(cols, "市場")
        rating_i = find_col(cols, "評価")

        for _, row in df.iterrows():
            vals = list(row.values)
            row_blob = " | ".join(clean_text(v) for v in vals)

            code4 = None
            if code_i is not None:
                cm = CODE_RE.search(clean_text(vals[code_i]))
                if cm:
                    code4 = cm.group(1)
            if not code4:
                cm = CODE_RE.search(row_blob)
                if cm:
                    code4 = cm.group(1)
            if not code4:
                continue

            public = parse_yen(vals[public_i]) if public_i is not None else None
            initial = parse_yen(vals[initial_i]) if initial_i is not None else None

            # If multi-row headers confused pandas, infer initial price from a market+price cell.
            if initial is None:
                market_pos = next((i for i, v in enumerate(vals) if any(m in clean_text(v) for m in MARKETS)), None)
                if market_pos is not None:
                    for v in vals[market_pos + 1:]:
                        p = parse_yen(v)
                        if p:
                            initial = p
                            break

            rating = normalize_rating(vals[rating_i]) if rating_i is not None else normalize_rating(row_blob)
            market = ""
            if market_i is not None:
                market = next((m for m in MARKETS if m in clean_text(vals[market_i])), "")
            if not market:
                market = next((m for m in MARKETS if m in row_blob), "")

            listed = None
            if date_i is not None:
                md = clean_text(vals[date_i])
                mm = re.search(r"(\d{1,2})/(\d{1,2})", md)
                if mm:
                    try:
                        listed = date(year, int(mm.group(1)), int(mm.group(2))).isoformat()
                    except ValueError:
                        pass

            result[code4] = {
                "ipoRating": rating,
                "ipoRatingSource": "庶民のIPO" if rating else None,
                "ipoSourceUrl": source_url,
                "publicPrice": public,
                "initialPrice": initial,
                "ipokabuMarket": market or None,
                "ipokabuListedDate": listed,
            }

    return result


def parse_ipokabu_bs4(html: str, year: int, source_url: str) -> dict[str, dict[str, Any]]:
    """Fallback: inspect each <tr> by code and identify public/initial prices."""
    soup = BeautifulSoup(html, "html.parser")
    result: dict[str, dict[str, Any]] = {}

    for tr in soup.find_all("tr"):
        cells = [clean_text(x.get_text(" ", strip=True)) for x in tr.find_all(["td", "th"])]
        if not cells:
            continue
        blob = " | ".join(cells)
        cm = CODE_RE.search(blob)
        if not cm:
            continue
        code4 = cm.group(1)

        rating = normalize_rating(blob)
        market = next((m for m in MARKETS if m in blob), "")

        prices = []
        for c in cells:
            p = parse_yen(c)
            if p:
                prices.append(p)

        public = prices[0] if prices else None
        initial = None
        # Profit amount may also be in yen; prefer a price after the market label.
        market_index = next((i for i, c in enumerate(cells) if any(m in c for m in MARKETS)), None)
        if market_index is not None:
            for c in cells[market_index + 1:]:
                p = parse_yen(c)
                if p:
                    initial = p
                    break

        md = re.search(r"(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)", blob)
        listed = None
        if md:
            try:
                listed = date(year, int(md.group(1)), int(md.group(2))).isoformat()
            except ValueError:
                pass

        result[code4] = {
            "ipoRating": rating,
            "ipoRatingSource": "庶民のIPO" if rating else None,
            "ipoSourceUrl": source_url,
            "publicPrice": public,
            "initialPrice": initial,
            "ipokabuMarket": market or None,
            "ipokabuListedDate": listed,
        }
    return result


def parse_ipokabu() -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for year, url in IPO_KABU_URLS.items():
        try:
            html = fetch_html(url)
        except Exception as e:
            print(f"[WARN] 庶民のIPO fetch {year}: {e}", flush=True)
            continue

        rows = parse_ipokabu_table(html, year, url)
        if len(rows) < 5:
            fallback = parse_ipokabu_bs4(html, year, url)
            if len(fallback) > len(rows):
                rows = fallback

        print(f"[庶民のIPO] {year}: parsed={len(rows)}", flush=True)
        merged.update(rows)

    # Do not fail the whole run if the third-party site changes,
    # but make the problem conspicuous in the log.
    if len(merged) < 100:
        print(
            f"[WARN] 庶民のIPO total is only {len(merged)}. "
            "JPX/yfinance processing will continue; IPO ratings/prices may be partial.",
            flush=True,
        )
    else:
        print(f"[庶民のIPO] total={len(merged)}", flush=True)
    return merged


def load_existing() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], bool]:
    if not OUT.exists():
        return [], {}, True

    try:
        raw = json.loads(OUT.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[WARN] existing latest.json unreadable: {e}", flush=True)
        return [], {}, True

    if isinstance(raw, dict):
        items = raw.get("ipos", [])
    elif isinstance(raw, list):
        items = raw
    else:
        items = []

    if not isinstance(items, list):
        items = []

    by_code: dict[str, dict[str, Any]] = {}
    for x in items:
        if not isinstance(x, dict):
            continue
        c = clean_text(x.get("code4") or x.get("code")).replace(".T", "")
        m = CODE_RE.search(c)
        if m:
            by_code[m.group(1)] = x

    # First v4 run is a full backfill. Subsequent v4 runs can be incremental.
    is_v4 = bool(items) and all(
        isinstance(x, dict) and x.get("_schemaVersion") == SCHEMA_VERSION
        for x in items[: min(20, len(items))]
    )
    full_backfill = not is_v4

    print(
        f"[CACHE] existing={len(by_code)} mode={'FULL BACKFILL' if full_backfill else 'INCREMENTAL'}",
        flush=True,
    )
    return items, by_code, full_backfill


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
    if not symbols:
        return {}

    result: dict[str, list[dict[str, Any]]] = {}

    # Chunking is more reliable than one huge request and still much faster
    # than one-ticker-at-a-time fetching.
    chunk_size = 60
    for pos in range(0, len(symbols), chunk_size):
        chunk = symbols[pos: pos + chunk_size]
        print(f"[yfinance] batch {pos + 1}-{pos + len(chunk)}/{len(symbols)}", flush=True)

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
            timeout=25,
        )
        if raw is None or raw.empty:
            print("[WARN] yfinance batch returned no rows", flush=True)
            continue

        for symbol in chunk:
            try:
                if isinstance(raw.columns, pd.MultiIndex):
                    lev0 = set(map(str, raw.columns.get_level_values(0)))
                    lev1 = set(map(str, raw.columns.get_level_values(1)))
                    if symbol in lev1:
                        sub = raw.xs(symbol, axis=1, level=1)
                    elif symbol in lev0:
                        sub = raw.xs(symbol, axis=1, level=0)
                    else:
                        continue
                else:
                    # Single ticker case.
                    sub = raw
                hist = normalize_price_frame(sub)
                if hist:
                    result[symbol] = hist
            except Exception as e:
                print(f"[WARN] yfinance parse {symbol}: {e}", flush=True)

    print(f"[yfinance] tickers with price data={len(result)}/{len(symbols)}", flush=True)
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
    if not history or not listed:
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


def pct(current: float | None, base: float | int | None) -> float | None:
    if current is None or not base:
        return None
    try:
        return round((float(current) / float(base) - 1.0) * 100.0, 2)
    except Exception:
        return None


# ---------- Optional J-Quants financials ----------
# Financial refresh is deliberately non-fatal. Price/IPO data still complete
# if J-Quants auth/API changes.

def jq_headers_from_secret(secret: str) -> dict[str, str] | None:
    secret = clean_text(secret)
    if not secret:
        return None
    # Current API-key style.
    if ":" not in secret:
        return {"x-api-key": secret}
    return None


def jq_legacy_token(secret: str) -> str | None:
    if ":" not in secret:
        return None
    email, password = secret.split(":", 1)
    endpoints = [
        "https://api.jquants.com/v1/token/auth_user",
        "https://api.jquants.com/v2/token/auth_user",
    ]
    for url in endpoints:
        try:
            r = requests.post(
                url,
                json={"mailaddress": email, "password": password},
                timeout=20,
            )
            if r.ok:
                js = r.json()
                tok = js.get("idToken") or js.get("refreshToken")
                if tok:
                    return str(tok)
        except Exception:
            pass
    return None


def fetch_financials_nonfatal(code4: str, secret: str) -> list[dict[str, Any]]:
    if not secret:
        return []

    candidates: list[tuple[str, dict[str, str]]] = []
    key_headers = jq_headers_from_secret(secret)
    if key_headers:
        candidates.extend([
            (f"https://api.jquants.com/v2/fins/summary?code={code4}0", key_headers),
            (f"https://api.jquants.com/v2/fins/statements?code={code4}0", key_headers),
        ])

    token = jq_legacy_token(secret)
    if token:
        headers = {"Authorization": f"Bearer {token}"}
        candidates.extend([
            (f"https://api.jquants.com/v1/fins/statements?code={code4}0", headers),
            (f"https://api.jquants.com/v1/fins/summary?code={code4}0", headers),
        ])

    for url, headers in candidates:
        try:
            r = requests.get(url, headers=headers, timeout=25)
            if not r.ok:
                continue
            js = r.json()
            data = (
                js.get("data")
                or js.get("statements")
                or js.get("financial_summary")
                or js.get("financials")
                or []
            )
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


def main() -> None:
    run_started = datetime.now().isoformat(timespec="seconds")
    _, old_by_code, full_backfill = load_existing()

    # 1) Always rebuild metadata from JPX. Old names are never trusted.
    jpx = parse_jpx()

    # 2) Fetch IPO rating/public/initial price data.
    ipokabu = parse_ipokabu()

    # 3) Full price backfill once for schema v4, then incremental updates.
    today = date.today()
    if full_backfill:
        price_start = (today - timedelta(days=HISTORY_RETENTION_DAYS + 10)).isoformat()
    else:
        price_start = (today - timedelta(days=RECENT_REFRESH_DAYS)).isoformat()
    price_end = (today + timedelta(days=1)).isoformat()

    symbols = [f"{x['code4']}.T" for x in jpx]
    price_map = download_prices(symbols, price_start, price_end)

    # 4) Optional J-Quants financial refresh.
    jq_secret = os.environ.get("JQUANTS_API_KEY", "").strip()
    if not jq_secret:
        print("[J-Quants] no secret configured; financial refresh skipped", flush=True)

    items: list[dict[str, Any]] = []
    jq_refresh_budget = 12  # keep the daily job fast; refresh a limited stale set each run
    jq_refreshed = 0

    for i, master in enumerate(jpx, 1):
        code4 = master["code4"]
        prev = old_by_code.get(code4, {})
        ipo = ipokabu.get(code4, {})
        symbol = f"{code4}.T"

        history = merge_history(prev.get("priceHistory", []), price_map.get(symbol, []))
        current_price = history[-1].get("close") if history else None
        price_date = history[-1].get("date") if history else None

        public = (
            ipo.get("publicPrice")
            or master.get("publicPriceJPX")
            or prev.get("publicPrice")
        )
        initial = ipo.get("initialPrice") or prev.get("initialPrice")

        financials = prev.get("financials", [])
        financials_updated = prev.get("financialsUpdatedAt")
        if jq_secret and jq_refreshed < jq_refresh_budget and should_refresh_financials(prev):
            fresh = fetch_financials_nonfatal(code4, jq_secret)
            if fresh:
                financials = fresh
                financials_updated = today.isoformat()
            jq_refreshed += 1

        miles: dict[str, Any] = {}
        for days in (30, 90, 180, 365, 730, 1095):
            p, d = milestone(history, master["listedDate"], days)
            miles[f"price{days}d"] = p
            miles[f"price{days}dDate"] = d

        item = {
            "_schemaVersion": SCHEMA_VERSION,
            "code": code4,
            "code4": code4,
            # Always use freshly parsed/sanitized JPX company name:
            "name": normalize_company_name(master["name"]),
            "market": master["market"],
            "listedDate": master["listedDate"],
            "publicPrice": public,
            "initialPrice": initial,
            "initialReturnPct": pct(float(initial) if initial else None, public),
            "ipoRating": ipo.get("ipoRating") or prev.get("ipoRating"),
            "ipoRatingSource": (
                ipo.get("ipoRatingSource")
                or prev.get("ipoRatingSource")
            ),
            "ipoSourceUrl": (
                ipo.get("ipoSourceUrl")
                or prev.get("ipoSourceUrl")
            ),
            "currentPrice": current_price,
            "priceAsOfDate": price_date,
            "priceVsPublicPct": pct(current_price, public),
            "priceVsInitialPct": pct(current_price, initial),
            **miles,
            "priceHistory": history,
            "financials": financials if isinstance(financials, list) else [],
            "financialsUpdatedAt": financials_updated,
            "dataRetrievedAt": run_started,
            "dataSource": [
                "JPX",
                * (["庶民のIPO"] if ipo else []),
                * (["yfinance"] if history else []),
                * (["J-Quants"] if financials else []),
            ],
        }

        # Final safety net against contaminated names from any source/parser.
        item["name"] = normalize_company_name(item["name"])
        if "インタビュー" in item["name"]:
            raise RuntimeError(f"Bad company name survived sanitization: {item['name']}")

        items.append(item)

        if i % 25 == 0 or i == len(jpx):
            print(f"[BUILD] {i}/{len(jpx)}", flush=True)

    # Keep existing app compatibility: latest.json is a plain ARRAY.
    # Atomic replacement means a failed run cannot erase the existing good JSON.
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(items, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    tmp.replace(OUT)

    mb = OUT.stat().st_size / 1024 / 1024
    print(
        f"[DONE] {len(items)} IPOs, latest.json={mb:.2f} MB, "
        f"mode={'full-backfill' if full_backfill else 'incremental'}",
        flush=True,
    )


if __name__ == "__main__":
    main()
