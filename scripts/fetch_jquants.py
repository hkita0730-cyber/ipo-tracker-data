#!/usr/bin/env python3
"""
Fast IPO tracker data fetcher
- JPX: IPO master (Prime / Standard / Growth only)
- 庶民のIPO: rating / public price / initial price
- yfinance: batched 2-year daily price history
- J-Quants: financials only when needed (new IPOs or stale financial data)
- Incremental: preserves existing history and only refreshes recent prices on later runs

This script is designed for GitHub Actions + docs/latest.json.
"""

from __future__ import annotations

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

JPX_URLS = [
    "https://www.jpx.co.jp/listing/stocks/new/index.html",
    "https://www.jpx.co.jp/listing/stocks/new/00-archives-01.html",
    "https://www.jpx.co.jp/listing/stocks/new/00-archives-02.html",
    "https://www.jpx.co.jp/listing/stocks/new/00-archives-03.html",
]
IPO_KABU_URLS = [
    "https://ipokabu.net/ipo/list2026",
    "https://ipokabu.net/ipo/list2025",
    "https://ipokabu.net/ipo/list2024",
]
LOOKBACK_DAYS = 730
RECENT_DAYS = 100
FINANCIAL_REFRESH_DAYS = 7
JQUANTS_INTERVAL = 13.0

HEADERS = {
    "User-Agent": "Mozilla/5.0 (IPO tracker personal research; contact unavailable)"
}

DATE_RE = re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})")
CODE_RE = re.compile(r"^\*?(\d{4}[A-Z]?)$")


def clean_text(x: Any) -> str:
    return re.sub(r"\s+", " ", str(x or "")).strip()


def parse_date(s: str) -> str | None:
    m = DATE_RE.search(clean_text(s))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


def get(url: str, timeout: int = 30) -> str:
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or r.encoding
    return r.text


def parse_jpx() -> list[dict[str, Any]]:
    rows_out = []
    seen = set()

    for url in JPX_URLS:
        try:
            html = get(url)
        except Exception as e:
            print(f"[WARN] JPX fetch failed: {url}: {e}")
            continue

        soup = BeautifulSoup(html, "html.parser")
        for tr in soup.select("tr"):
            cells = [clean_text(c.get_text(" ", strip=True)) for c in tr.find_all(["td", "th"])]
            cells = [c for c in cells if c]
            if not cells:
                continue

            listing_date = None
            date_idx = None
            for i, c in enumerate(cells):
                d = parse_date(c)
                if d:
                    listing_date = d
                    date_idx = i
                    break
            if not listing_date:
                continue

            code_idx = None
            code4 = None
            for i, c in enumerate(cells):
                m = CODE_RE.fullmatch(c.replace(" ", ""))
                if m:
                    code4 = m.group(1)
                    code_idx = i
                    break
            if not code4:
                continue

            # Find company name immediately before the code, skipping JPX link labels.
            name = ""
            for prev in reversed(cells[:code_idx]):
                cand = clean_text(prev)
                if not cand or cand in ("代表者インタビュー", "詳細"):
                    continue
                name = cand
                break

            technical = "*" in name or "*" in cells[code_idx]
            name = name.replace("*", "").strip()

            market = ""
            for c in cells:
                if c in ("プライム", "スタンダード", "グロース"):
                    market = c
                    break

            if market not in ("プライム", "スタンダード", "グロース"):
                continue
            if technical:
                continue

            public_price = None
            # JPX table commonly has public price after code; search nearby.
            for c in cells[code_idx + 1:]:
                m = re.search(r"([\d,]+)\s*円", c)
                if m:
                    public_price = int(m.group(1).replace(",", ""))
                    break
            # Some tables put price in plain numeric cells.
            if public_price is None:
                for c in cells[code_idx + 1:]:
                    if re.fullmatch(r"[\d,]+", c):
                        v = int(c.replace(",", ""))
                        if 10 <= v <= 1000000:
                            public_price = v
                            break

            if code4 in seen:
                continue
            seen.add(code4)
            rows_out.append({
                "code4": code4,
                "name": name,
                "market": market,
                "listedDate": listing_date,
                "publicPriceJPX": public_price,
                "jpxSourceUrl": url,
            })

    rows_out.sort(key=lambda x: x["listedDate"], reverse=True)
    if not rows_out:
        raise RuntimeError("JPX IPO parsing returned zero target IPOs; refusing to overwrite output.")
    print(f"[JPX] {len(rows_out)} target IPOs")
    return rows_out


def parse_price(s: str) -> int | None:
    nums = re.findall(r"[\d,]+", clean_text(s))
    if not nums:
        return None
    try:
        return int(nums[0].replace(",", ""))
    except ValueError:
        return None


def parse_ipokabu() -> dict[str, dict[str, Any]]:
    result = {}
    for url in IPO_KABU_URLS:
        try:
            html = get(url)
        except Exception as e:
            print(f"[WARN] 庶民のIPO fetch failed: {url}: {e}")
            continue
        soup = BeautifulSoup(html, "html.parser")
        for table in soup.find_all("table"):
            headers = [clean_text(x.get_text(" ", strip=True)) for x in table.find_all("th")]
            blob = " ".join(headers)
            if "証券コード" not in blob or "公開価格" not in blob:
                continue
            for tr in table.find_all("tr"):
                cells = [clean_text(x.get_text(" ", strip=True)) for x in tr.find_all(["td", "th"])]
                if not cells:
                    continue
                code = None
                for c in cells:
                    m = re.search(r"\b(\d{4}[A-Z]?)\b", c)
                    if m:
                        code = m.group(1)
                        break
                if not code:
                    continue

                text = " | ".join(cells)
                rating = None
                for r in ("S", "A", "B", "C", "D"):
                    if re.search(rf"評価\s*{r}\b", text) or re.search(rf"\b{r}\b", text):
                        rating = r
                        break

                prices = [parse_price(c) for c in cells if "円" in c or re.fullmatch(r"[\d,]+", c)]
                prices = [p for p in prices if p is not None and p > 0]
                public = prices[0] if prices else None
                initial = prices[-1] if len(prices) >= 2 else None

                result[code] = {
                    "ipoRating": rating,
                    "ipoRatingSource": "庶民のIPO",
                    "ipoSourceUrl": url,
                    "publicPrice": public,
                    "initialPrice": initial,
                }
            break
    print(f"[庶民のIPO] {len(result)} rows")
    return result


def jquants_token(api_key: str) -> str:
    # Supports the common J-Quants API v2 API-key flow.
    r = requests.post(
        "https://api.jquants.com/v2/token/auth_user",
        json={"mailaddress": api_key.split(":", 1)[0], "password": api_key.split(":", 1)[1]},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["idToken"]


def fetch_jquants_financials(code4: str, token: str) -> list[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}"}
    # The financial endpoint can vary by J-Quants generation.
    # Try V2 first, then V1-compatible path.
    endpoints = [
        f"https://api.jquants.com/v2/fins/summary?code={code4}0",
        f"https://api.jquants.com/v1/fins/summary?code={code4}0",
    ]
    for url in endpoints:
        try:
            r = requests.get(url, headers=headers, timeout=30)
            if r.status_code == 404:
                continue
            r.raise_for_status()
            js = r.json()
            data = js.get("data", [])
            return data if isinstance(data, list) else []
        except Exception:
            continue
    return []


def normalize_history(df: pd.DataFrame, symbol: str) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    if isinstance(df.columns, pd.MultiIndex):
        # When downloading one ticker in a multi-ticker call, select its ticker level.
        if symbol in df.columns.get_level_values(-1):
            df = df.xs(symbol, axis=1, level=-1)
        elif symbol in df.columns.get_level_values(0):
            df = df.xs(symbol, axis=1, level=0)

    out = []
    for idx, row in df.iterrows():
        try:
            d = pd.Timestamp(idx).date().isoformat()
        except Exception:
            continue
        close = row.get("Close")
        if pd.isna(close):
            continue
        item = {"date": d, "close": float(close)}
        for key in ("Open", "High", "Low", "Volume"):
            val = row.get(key)
            if val is not None and not pd.isna(val):
                item[key.lower()] = float(val) if key != "Volume" else int(val)
        out.append(item)
    return out


def download_prices(symbols: list[str], start: str, end: str) -> dict[str, list[dict[str, Any]]]:
    if not symbols:
        return {}
    print(f"[yfinance] batch download {len(symbols)} tickers...")
    raw = yf.download(
        symbols,
        start=start,
        end=end,
        interval="1d",
        auto_adjust=False,
        actions=False,
        threads=True,
        group_by="column",
        progress=False,
        timeout=20,
    )
    if raw is None or raw.empty:
        return {}

    result = {}
    # yfinance returns columns either (field, ticker) or (ticker, field).
    for symbol in symbols:
        try:
            if isinstance(raw.columns, pd.MultiIndex):
                level0 = set(raw.columns.get_level_values(0))
                level1 = set(raw.columns.get_level_values(1))
                if symbol in level1:
                    sub = raw.xs(symbol, axis=1, level=1)
                elif symbol in level0:
                    sub = raw.xs(symbol, axis=1, level=0)
                else:
                    continue
            else:
                sub = raw
            hist = normalize_history(sub, symbol)
            if hist:
                result[symbol] = hist
        except Exception as e:
            print(f"[WARN] yfinance {symbol}: {e}")
    print(f"[yfinance] received {len(result)} tickers")
    return result


def merge_history(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_date = {x.get("date"): x for x in old if x.get("date")}
    for x in new:
        if x.get("date"):
            by_date[x["date"]] = x
    return [by_date[d] for d in sorted(by_date)]


def milestone(history: list[dict[str, Any]], listed: str, days: int) -> tuple[float | None, str | None]:
    if not history:
        return None, None
    target = date.fromisoformat(listed) + timedelta(days=days)
    best = None
    for x in history:
        try:
            d = date.fromisoformat(x["date"])
            if d >= target:
                if d <= target + timedelta(days=15):
                    best = x
                break
        except Exception:
            pass
    return (best.get("close"), best.get("date")) if best else (None, None)


def main():
    started = datetime.now().isoformat(timespec="seconds")
    old = {}
    if OUT.exists():
        try:
            old = json.loads(OUT.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[WARN] existing latest.json unreadable: {e}")

    old_items = {str(x.get("code4")): x for x in old.get("ipos", []) if x.get("code4")}
    jpx = parse_jpx()
    ipokabu = parse_ipokabu()

    today = date.today()
    full_start = (today - timedelta(days=LOOKBACK_DAYS + 10)).isoformat()
    recent_start = (today - timedelta(days=RECENT_DAYS)).isoformat()
    end = (today + timedelta(days=1)).isoformat()

    symbols = [f"{x['code4']}.T" for x in jpx]
    # First run: fetch 2 years in one/batched yfinance request.
    # Later runs: only refresh recent prices and merge into preserved history.
    has_history = any(x.get("priceHistory") for x in old_items.values())
    start = recent_start if has_history else full_start
    prices = download_prices(symbols, start, end)

    # J-Quants is deliberately limited to financials that are new/stale.
    token = None
    api_key = os.environ.get("JQUANTS_API_KEY", "")
    if api_key and ":" in api_key:
        try:
            token = jquants_token(api_key)
        except Exception as e:
            print(f"[WARN] J-Quants auth failed; continuing without financial refresh: {e}")

    items = []
    for n, x in enumerate(jpx, 1):
        code4 = x["code4"]
        previous = old_items.get(code4, {})
        symbol = f"{code4}.T"

        hist = merge_history(previous.get("priceHistory", []), prices.get(symbol, []))
        # Trim to the last 2 years from today.
        cutoff = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
        hist = [h for h in hist if h.get("date", "") >= cutoff]

        ipo = ipokabu.get(code4, {})
        listed = x["listedDate"]

        # Refresh financials only for new/stale items, not every IPO every day.
        financials = previous.get("financials", [])
        fin_updated = previous.get("financialsUpdatedAt")
        stale = True
        if fin_updated:
            try:
                stale = (today - date.fromisoformat(fin_updated[:10])).days >= FINANCIAL_REFRESH_DAYS
            except Exception:
                stale = True

        if token and (not financials or stale):
            try:
                financials = fetch_jquants_financials(code4, token)
                if financials:
                    fin_updated = today.isoformat()
                time.sleep(JQUANTS_INTERVAL)
            except Exception as e:
                print(f"[WARN] financials {code4}: {e}")

        current_price = hist[-1]["close"] if hist else None
        current_date = hist[-1]["date"] if hist else None

        prices_m = {}
        for days in (30, 90, 180, 365, 730):
            p, d = milestone(hist, listed, days)
            prices_m[f"price{days}d"] = p
            prices_m[f"price{days}dDate"] = d

        public = ipo.get("publicPrice") or x.get("publicPriceJPX") or previous.get("publicPrice")
        initial = ipo.get("initialPrice") or previous.get("initialPrice")
        initial_return = None
        if public and initial:
            initial_return = round((initial / public - 1) * 100, 2)

        items.append({
            "code": code4,
            "code4": code4,
            "name": x["name"],
            "market": x["market"],
            "listedDate": listed,
            "publicPrice": public,
            "initialPrice": initial,
            "initialReturnPct": initial_return,
            "ipoRating": ipo.get("ipoRating") or previous.get("ipoRating"),
            "ipoRatingSource": ipo.get("ipoRatingSource") or previous.get("ipoRatingSource"),
            "ipoSourceUrl": ipo.get("ipoSourceUrl") or previous.get("ipoSourceUrl"),
            "currentPrice": current_price,
            "priceAsOfDate": current_date,
            "priceVsPublicPct": round((current_price / public - 1) * 100, 2) if current_price and public else None,
            "priceVsInitialPct": round((current_price / initial - 1) * 100, 2) if current_price and initial else None,
            **prices_m,
            "priceHistory": hist,
            "financials": financials,
            "financialsUpdatedAt": fin_updated,
            "dataSource": ["JPX", "庶民のIPO", "yfinance"] + (["J-Quants"] if financials else []),
        })

        if n % 25 == 0:
            print(f"[BUILD] {n}/{len(jpx)}")

    payload = {
        "updatedAt": datetime.now().isoformat(timespec="seconds"),
        "updateMode": "incremental-fast",
        "priceHistorySource": "yfinance",
        "financialSource": "J-Quants (when API key configured)",
        "historyWindowDays": LOOKBACK_DAYS,
        "ipoCount": len(items),
        "ipos": items,
        "notes": [
            "初回はyfinanceの一括取得で過去2年を取得。",
            "2回目以降は既存の2年分を保持し、直近約100日だけ更新。",
            "J-Quantsは財務データの新規・7日以上古い銘柄だけ更新。",
        ],
        "runStartedAt": started,
    }

    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(OUT)
    print(f"[DONE] wrote {OUT} ({OUT.stat().st_size / 1024 / 1024:.2f} MB)")


if __name__ == "__main__":
    main()
