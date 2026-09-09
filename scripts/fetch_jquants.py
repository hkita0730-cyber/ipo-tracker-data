#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
IPO tracker data builder.

Sources
- JPX official new listings pages: listing date, company, code, market, public price
- 庶民のIPO (ipokabu.net): rating, public price, initial price
- J-Quants Free: delayed historical prices and financial summaries
- yfinance: latest ~12 weeks that J-Quants Free cannot provide

Target: TSE Prime / Standard / Growth only.
Excludes Tokyo PRO Market, technical listings, and preferred/class shares.
Retention: 2 years from listing date.
"""

import os
import sys
import time
import json
import datetime as dt
import re
import urllib.request
import urllib.parse
import urllib.error
from html import unescape

from bs4 import BeautifulSoup

try:
    import yfinance as yf
except ImportError:
    yf = None

API_BASE = "https://api.jquants.com/v2"
API_KEY = os.environ.get("JQUANTS_API_KEY", "")
OUTPUT_PATH = os.environ.get("OUTPUT_PATH", "docs/latest.json")

LOOKBACK_DAYS = int(os.environ.get("IPO_LOOKBACK_DAYS", "730"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "730"))
YFINANCE_LOOKBACK_DAYS = int(os.environ.get("YFINANCE_LOOKBACK_DAYS", "84"))

# J-Quants Free: 5 requests/minute. Keep a conservative interval.
REQUEST_INTERVAL_SEC = 13

JPX_URLS = [
    "https://www.jpx.co.jp/listing/stocks/new/index.html",
    "https://www.jpx.co.jp/listing/stocks/new/00-archives-01.html",
    "https://www.jpx.co.jp/listing/stocks/new/00-archives-02.html",
    "https://www.jpx.co.jp/listing/stocks/new/00-archives-03.html",
]

IPO_KABU_URLS = {
    2026: "https://ipokabu.net/ipo/",
    2025: "https://ipokabu.net/ipo/list2025",
    2024: "https://ipokabu.net/ipo/list2024",
}

ALLOWED_MARKETS = ("プライム", "スタンダード", "グロース")
CODE_RE = re.compile(r"^\d{3,4}[A-Z]?$")
DATE_RE = re.compile(r"^\d{4}/\d{1,2}/\d{1,2}$")
PRICE_RE = re.compile(r"^\s*[\d,]+(?:\.\d+)?(?:\s*\(.*\))?\s*$")


def http_get(url, timeout=40):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 IPO-tracker/1.0",
            "Accept-Language": "ja,en;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read()


def clean_text(s):
    s = unescape(str(s))
    s = s.replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def number_from_price(text):
    if not text:
        return None
    s = clean_text(text).replace("円", "").replace(",", "")
    m = re.match(r"^(\d+(?:\.\d+)?)", s)
    return float(m.group(1)) if m else None


def code4(code):
    return str(code).strip()


def jquants_code(code):
    code = code4(code)
    if len(code) == 4:
        return code + "0"
    return code


def is_common_stock_code(code):
    c = str(code).strip()
    # J-Quants 5-digit form: final digit 0 = ordinary shares.
    if len(c) == 5 and c.isdigit():
        return c[-1] == "0"
    return True


def allowed_market(market):
    return market and any(x in market for x in ALLOWED_MARKETS)


def parse_jpx_page(url, today):
    print(f"JPX公式を取得中: {url}")
    html = http_get(url).decode("utf-8", "ignore")
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")

    result = []

    for table in tables:
        rows = table.find_all("tr")
        for i, tr in enumerate(rows):
            cells = [clean_text(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
            if not cells:
                continue

            code_idx = None
            code = None
            for j, c in enumerate(cells):
                if CODE_RE.fullmatch(c):
                    code_idx = j
                    code = c
                    break
            if code is None:
                continue

            # JPX uses a two-row layout: the first row contains date/name/code,
            # and the following row contains market/public-offer price.
            combined = cells[:]
            next_cells = []
            if i + 1 < len(rows):
                next_cells = [
                    clean_text(c.get_text(" ", strip=True))
                    for c in rows[i + 1].find_all(["th", "td"])
                ]
                combined += next_cells

            listing_date = None
            for c in cells:
                if DATE_RE.fullmatch(c):
                    listing_date = c.replace("/", "-")
                    break
            if not listing_date:
                continue

            try:
                ld = dt.date.fromisoformat(listing_date)
            except ValueError:
                continue

            # Future listings are not IPO results yet.
            if ld > today:
                continue

            # Company name is normally immediately before the code.
            name = cells[code_idx - 1] if code_idx > 0 else ""
            technical = "*" in name
            name = name.replace("*", "").strip()

            # Market is normally the first cell of the following row.
            market = ""
            for c in next_cells + cells:
                if c in ALLOWED_MARKETS:
                    market = c
                    break
                if any(m in c for m in ALLOWED_MARKETS):
                    market = next((m for m in ALLOWED_MARKETS if m in c), "")
                    if market:
                        break

            if not allowed_market(market):
                continue
            if technical:
                continue
            if not is_common_stock_code(jquants_code(code)):
                continue

            # In JPX's second row, public offering/sale price is the 4th cell
            # (index 3). Fall back to the first standalone numeric price.
            public_price = None
            if len(next_cells) >= 4:
                public_price = number_from_price(next_cells[3])

            if public_price is None:
                for c in next_cells:
                    p = number_from_price(c)
                    if p is not None:
                        public_price = p
                        break

            result.append({
                "listedDate": listing_date,
                "code4": code,
                "code": jquants_code(code),
                "name": name,
                "market": market,
                "publicPrice": public_price,
                "technicalListing": False,
                "source": "JPX",
            })

    return result


def fetch_jpx_ipos(today):
    all_rows = []
    seen = set()
    for url in JPX_URLS:
        try:
            rows = parse_jpx_page(url, today)
            for r in rows:
                key = (r["listedDate"], r["code4"])
                if key not in seen:
                    seen.add(key)
                    all_rows.append(r)
        except Exception as e:
            print(f"JPX取得失敗: {url}: {e}", file=sys.stderr)

    cutoff = today - dt.timedelta(days=LOOKBACK_DAYS)
    all_rows = [
        r for r in all_rows
        if cutoff <= dt.date.fromisoformat(r["listedDate"]) <= today
    ]
    all_rows.sort(key=lambda r: (r["listedDate"], r["code4"]), reverse=True)
    print(f"JPXから取得した対象IPO: {len(all_rows)}件")
    return all_rows


def parse_ipokabu_year(year, url):
    print(f"庶民のIPOを取得中: {year} {url}")
    html = http_get(url).decode("utf-8", "ignore")
    soup = BeautifulSoup(html, "html.parser")
    out = {}

    for table in soup.find_all("table"):
        header_text = clean_text(table.get_text(" ", strip=True))
        if "証券コード" not in header_text or "公開価格" not in header_text or "初値" not in header_text:
            continue

        for tr in table.find_all("tr"):
            cells = [clean_text(c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
            if not cells:
                continue
            row_text = " ".join(cells)

            code_match = re.search(r"\b(\d{3,4}[A-Z]?)\b", row_text)
            if not code_match:
                continue
            code = code_match.group(1)

            # Rating is a standalone S/A/B/C/D token.
            rating = None
            for c in cells:
                if c in ("S", "A", "B", "C", "D"):
                    rating = c
                    break

            # Market
            market = None
            for c in cells:
                if c in ALLOWED_MARKETS:
                    market = c
                    break

            # Dates: year is known from the page.
            md = re.search(r"\b(\d{1,2})/(\d{1,2})\b", row_text)
            if not md:
                continue
            listed_date = f"{year}-{int(md.group(1)):02d}-{int(md.group(2)):02d}"

            # Prices shown with 円: public price, profit, initial price.
            prices = []
            for c in cells:
                for m in re.findall(r"([\d,]+(?:\.\d+)?)円", c):
                    try:
                        prices.append(float(m.replace(",", "")))
                    except ValueError:
                        pass

            if len(prices) < 2:
                continue

            public_price = prices[0]
            initial_price = prices[-1]

            # Company name is the cell immediately after the code, stripped of
            # broker-link text when possible.
            name = ""
            code_pos = None
            for j, c in enumerate(cells):
                if code == c or code in c.split():
                    code_pos = j
                    break
            if code_pos is not None and code_pos + 1 < len(cells):
                name = cells[code_pos + 1]
            if not name:
                name = code

            out[code] = {
                "listedDate": listed_date,
                "publicPrice": public_price,
                "initialPrice": initial_price,
                "ipoRating": rating,
                "ipoSource": "庶民のIPO",
                "ipoSourceUrl": url,
                "marketFromIpoSite": market,
            }

    print(f"  {year}: {len(out)}件")
    return out


def fetch_ipokabu_data(today):
    result = {}
    for year, url in IPO_KABU_URLS.items():
        try:
            data = parse_ipokabu_year(year, url)
            result.update(data)
        except Exception as e:
            print(f"庶民のIPO取得失敗 {year}: {e}", file=sys.stderr)
    return result


def api_get_all(path, params=None):
    if not API_KEY:
        print("環境変数 JQUANTS_API_KEY が設定されていません。", file=sys.stderr)
        sys.exit(1)

    params = dict(params or {})
    results = []

    while True:
        qs = urllib.parse.urlencode(params)
        url = API_BASE + path + ("?" + qs if qs else "")
        req = urllib.request.Request(url, headers={"x-api-key": API_KEY})
        try:
            with urllib.request.urlopen(req, timeout=40) as res:
                body = json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            print(f"J-Quants APIエラー {e.code}: {url}", file=sys.stderr)
            print(e.read().decode("utf-8", "ignore"), file=sys.stderr)
            break
        except Exception as e:
            print(f"J-Quants通信エラー: {url}: {e}", file=sys.stderr)
            break

        results.extend(body.get("data", []))
        pk = body.get("pagination_key")
        if not pk:
            break
        params["pagination_key"] = pk
        time.sleep(REQUEST_INTERVAL_SEC)

    return results


def fetch_price_history(code):
    rows = api_get_all("/equities/bars/daily", {"code": code})
    history = []
    as_of = None
    latest_market_cap = None

    for r in rows:
        date = r.get("Date")
        close = r.get("C")
        adj_close = r.get("AdjC")
        price = close if close is not None else adj_close
        if date and price is not None:
            d = date[:10]
            history.append({"date": d, "price": price})
            as_of = d
            if r.get("MktCap") is not None:
                latest_market_cap = r.get("MktCap") * 1_000_000

    history.sort(key=lambda x: x["date"])
    return history, as_of, latest_market_cap


def to_yfinance_symbol(code):
    c = str(code).strip()
    if len(c) == 5 and c[-1] == "0":
        c = c[:4]
    return c + ".T"


def fetch_yfinance_history(code, start_date, end_date):
    if yf is None:
        return []
    symbol = to_yfinance_symbol(code)
    try:
        df = yf.download(
            symbol,
            start=start_date,
            end=end_date,
            interval="1d",
            auto_adjust=False,
            actions=False,
            progress=False,
            threads=False,
        )
        if df is None or df.empty:
            return []

        if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
            close_series = df["Close"].iloc[:, 0]
        else:
            close_series = df["Close"]

        result = []
        for idx, value in close_series.items():
            try:
                price = float(value)
            except (TypeError, ValueError):
                continue
            if price != price:
                continue
            result.append({"date": idx.strftime("%Y-%m-%d"), "price": price})
        return result
    except Exception as e:
        print(f"yfinance取得エラー {code}/{symbol}: {e}", file=sys.stderr)
        return []


def merge_price_histories(jq_history, yf_history, listed_date):
    by_date = {
        r["date"]: r for r in jq_history
        if r.get("date") and r.get("price") is not None
    }
    for r in yf_history:
        if r.get("date") and r.get("price") is not None:
            by_date.setdefault(r["date"], r)

    cutoff = dt.date.fromisoformat(listed_date)
    end = cutoff + dt.timedelta(days=RETENTION_DAYS)

    filtered = []
    for d in sorted(by_date):
        dd = dt.date.fromisoformat(d)
        if cutoff <= dd <= end:
            filtered.append(by_date[d])
    return filtered


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_financials(code):
    rows = api_get_all("/fins/summary", {"code": code})
    rows = [r for r in rows if r.get("DiscDate")]
    rows.sort(key=lambda r: r["DiscDate"])

    by_type = {}
    for r in rows:
        by_type.setdefault(r.get("CurPerType"), []).append(r)

    growth_map = {}
    for _, recs in by_type.items():
        recs = sorted(recs, key=lambda r: r.get("CurPerSt") or "")
        for i, r in enumerate(recs):
            prev = recs[i - 1] if i else None
            sales, op = num(r.get("Sales")), num(r.get("OP"))
            ps, po = (num(prev.get("Sales")), num(prev.get("OP"))) if prev else (None, None)
            rg = ((sales - ps) / ps * 100) if sales is not None and ps else None
            og = ((op - po) / po * 100) if op is not None and po else None
            growth_map[id(r)] = (rg, og)

    by_fy = {}
    for r in rows:
        by_fy.setdefault(r.get("CurFYEn"), []).append(r)

    revision_map = {}
    for _, recs in by_fy.items():
        recs = sorted(recs, key=lambda r: r["DiscDate"])
        for i, r in enumerate(recs):
            up = down = False
            if i:
                prev_fnp, cur_fnp = num(recs[i-1].get("FNP")), num(r.get("FNP"))
                if prev_fnp is not None and cur_fnp is not None:
                    up = cur_fnp > prev_fnp
                    down = cur_fnp < prev_fnp
            revision_map[id(r)] = (up, down)

    financials = []
    for r in rows:
        sales, op, np_, fnp = map(num, (r.get("Sales"), r.get("OP"), r.get("NP"), r.get("FNP")))
        rg, og = growth_map.get(id(r), (None, None))
        up, down = revision_map.get(id(r), (False, False))
        eq_ar, roe = num(r.get("EqAR")), num(r.get("ROE"))
        progress = (np_ / fnp * 100) if np_ is not None and fnp else None
        margin = (op / sales * 100) if op is not None and sales else None

        financials.append({
            "reportDate": r.get("DiscDate"),
            "revenue": sales,
            "revenueGrowth": round(rg, 1) if rg is not None else None,
            "opProfit": op,
            "opProfitGrowth": round(og, 1) if og is not None else None,
            "ordinaryProfit": num(r.get("OdP")),
            "netProfit": np_,
            "eps": num(r.get("EPS")),
            "opMargin": round(margin, 1) if margin is not None else None,
            "roe": round(roe * 100, 1) if roe is not None else None,
            "equityRatio": round(eq_ar * 100, 1) if eq_ar is not None else None,
            "opCF": num(r.get("CFO")),
            "freeCF": None,
            "guidance": (f"売上高予想 {r.get('FSales')} / 純利益予想 {r.get('FNP')}"
                         if r.get("FSales") or r.get("FNP") else None),
            "revisionUp": up,
            "revisionDown": down,
            "progressRate": round(progress, 1) if progress is not None else None,
        })
    return financials


def main():
    today = dt.date.today()

    # 1) IPO master is JPX, not J-Quants master.
    # This fixes the old method that could not know listing dates and also
    # preserves IPOs that were later delisted.
    jpx_ipos = fetch_jpx_ipos(today)

    # 2) Enrich with 庶民のIPO.
    ipokabu = fetch_ipokabu_data(today)

    # 3) Build output.
    output = []

    for ipo in jpx_ipos:
        code4 = ipo["code4"]
        jq_code = ipo["code"]
        extra = ipokabu.get(code4) or ipokabu.get(jq_code) or {}

        listed_date = ipo["listedDate"]

        # Prefer JPX for official public price; fall back to 庶民のIPO.
        public_price = ipo.get("publicPrice")
        if public_price is None:
            public_price = extra.get("publicPrice")

        initial_price = extra.get("initialPrice")
        initial_return = None
        if public_price and initial_price:
            initial_return = round((initial_price / public_price - 1) * 100, 2)

        print(f"{code4} {ipo['name']} ({listed_date}) 株価取得中…")

        time.sleep(REQUEST_INTERVAL_SEC)
        jq_history, jq_as_of, market_cap = fetch_price_history(jq_code)

        yf_end = today + dt.timedelta(days=1)
        yf_start = today - dt.timedelta(days=YFINANCE_LOOKBACK_DAYS)
        supplement_start = yf_start
        if jq_as_of:
            try:
                supplement_start = max(
                    yf_start,
                    dt.date.fromisoformat(jq_as_of) + dt.timedelta(days=1)
                )
            except ValueError:
                pass

        yf_history = fetch_yfinance_history(
            jq_code, supplement_start.isoformat(), yf_end.isoformat()
        )
        price_history = merge_price_histories(jq_history, yf_history, listed_date)

        as_of = price_history[-1]["date"] if price_history else jq_as_of
        current_price = price_history[-1]["price"] if price_history else None

        time.sleep(REQUEST_INTERVAL_SEC)
        financials = fetch_financials(jq_code)

        output.append({
            "code": jq_code,
            "code4": code4,
            "name": ipo["name"],
            "market": ipo["market"],
            "listedDate": listed_date,
            "publicPrice": public_price,
            "initialPrice": initial_price,
            "initialReturnPct": initial_return,
            "ipoRating": extra.get("ipoRating"),
            "ipoRatingSource": extra.get("ipoSource"),
            "ipoSourceUrl": extra.get("ipoSourceUrl"),
            "currentPrice": current_price,
            "priceAsOfDate": as_of,
            "marketCap": market_cap,
            "priceHistory": price_history,
            "financials": financials,
            "dataSource": "JPX + 庶民のIPO + J-Quants + yfinance",
            "dataRetrievedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        })

    os.makedirs(os.path.dirname(OUTPUT_PATH) or ".", exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"書き出し完了: {OUTPUT_PATH}（{len(output)}銘柄）")


if __name__ == "__main__":
    main()
