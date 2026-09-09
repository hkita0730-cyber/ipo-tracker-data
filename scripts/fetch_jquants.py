#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
IPO tracker data builder (complete version).

Data sources
- JPX official new-listing pages: listing date, company, code, market, public price
- 庶民のIPO (ipokabu.net): rating, public price, initial price, initial return
- J-Quants Free: up to 2 years of delayed daily prices and financial summaries
- yfinance: supplements the latest ~12 weeks unavailable from J-Quants Free

Target
- TSE Prime / Standard / Growth only
- Excludes Tokyo PRO Market
- Excludes JPX technical listings (company name marked with *)
- Excludes preferred/class shares where identifiable

Retention
- IPOs listed within the last 730 calendar days
- Price history: listing date through listing date + 730 days

Important safety behavior
- If JPX pages are reachable but parsing returns zero IPOs, the workflow FAILS
  instead of silently overwriting docs/latest.json with an empty dataset.
"""

import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
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

# J-Quants Free has a 5 requests/minute limit.
REQUEST_INTERVAL_SEC = int(os.environ.get("JQUANTS_REQUEST_INTERVAL_SEC", "13"))

JPX_URLS = [
    "https://www.jpx.co.jp/listing/stocks/new/index.html",
    "https://www.jpx.co.jp/listing/stocks/new/00-archives-01.html",
    "https://www.jpx.co.jp/listing/stocks/new/00-archives-02.html",
    "https://www.jpx.co.jp/listing/stocks/new/00-archives-03.html",
]

# Current year + two archive years cover the requested 2-year window.
IPO_KABU_URLS = {
    2026: "https://ipokabu.net/ipo/",
    2025: "https://ipokabu.net/ipo/list2025",
    2024: "https://ipokabu.net/ipo/list2024",
}

ALLOWED_MARKETS = ("プライム", "スタンダード", "グロース")
CODE_RE = re.compile(r"^\d{3,4}[A-Z]?$", re.I)
DATE_RE = re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})")
JPX_PRICE_RE = re.compile(r"^\s*[\d,]+(?:\.\d+)?\s*(?:\(.*\))?\s*$")


def http_get(url, timeout=45, retries=3):
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 IPO-tracker/2.0",
                    "Accept-Language": "ja,en;q=0.8",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return res.read()
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(2 * attempt)
    raise last_error


def clean_text(value):
    text = unescape(str(value or ""))
    text = text.replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def number_from_price(text):
    if text is None:
        return None
    s = clean_text(text).replace("円", "").replace(",", "")
    m = re.match(r"^(\d+(?:\.\d+)?)", s)
    return float(m.group(1)) if m else None


def normalize_code(code):
    return clean_text(code).upper()


def jquants_code(code):
    code = normalize_code(code)
    # J-Quants V2 uses 5-character issue codes; ordinary 4-digit/A codes
    # are represented by adding the final share-class digit 0.
    if len(code) == 4:
        return code + "0"
    return code


def is_common_stock_code(code):
    c = normalize_code(code)
    if len(c) == 5 and c[-1].isdigit():
        return c[-1] == "0"
    return True


def allowed_market(market):
    return bool(market) and any(m in market for m in ALLOWED_MARKETS)


def extract_listing_date(text):
    """JPX date cells contain e.g. '2026/08/04 （2026/06/30）'."""
    m = DATE_RE.search(clean_text(text))
    if not m:
        return None
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def parse_jpx_page(url, today):
    print(f"JPX公式を取得中: {url}")
    html = http_get(url).decode("utf-8", "ignore")
    soup = BeautifulSoup(html, "html.parser")
    result = []

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue

        for i, tr in enumerate(rows):
            cells = [
                clean_text(c.get_text(" ", strip=True))
                for c in tr.find_all(["th", "td"])
            ]
            if not cells:
                continue

            # JPX displays the issue code as 1234 or 123A.
            code_idx = None
            code = None
            for j, cell in enumerate(cells):
                candidate = normalize_code(cell)
                if CODE_RE.fullmatch(candidate):
                    code_idx = j
                    code = candidate
                    break
            if code is None:
                continue

            listing_date = None
            for cell in cells:
                listing_date = extract_listing_date(cell)
                if listing_date:
                    break
            if not listing_date:
                continue

            try:
                listing_day = dt.date.fromisoformat(listing_date)
            except ValueError:
                continue

            if listing_day > today:
                continue

            # JPX sometimes inserts a separate "代表者インタビュー" link
            # cell between the company name and the issue code.  Therefore
            # do not assume the company name is always cells[code_idx - 1].
            # Pick the nearest preceding cell that is not a navigation/link
            # label such as 代表者インタビュー.
            name = ""
            for prev_cell in reversed(cells[:code_idx]):
                candidate_name = clean_text(prev_cell)
                if not candidate_name:
                    continue
                if candidate_name in ("代表者インタビュー", "詳細"):
                    continue
                name = candidate_name
                break

            technical_listing = "*" in name
            name = name.replace("*", "").strip()

            # In JPX's current table, the next row contains the market in its
            # first cell and the public-offer/sale price in its 4th cell.
            next_cells = []
            if i + 1 < len(rows):
                next_cells = [
                    clean_text(c.get_text(" ", strip=True))
                    for c in rows[i + 1].find_all(["th", "td"])
                ]

            combined = cells + next_cells
            market = ""
            for cell in combined:
                for candidate in ALLOWED_MARKETS:
                    if cell == candidate or candidate in cell:
                        market = candidate
                        break
                if market:
                    break

            if not allowed_market(market):
                continue
            if technical_listing:
                continue

            jq_code = jquants_code(code)
            if not is_common_stock_code(jq_code):
                continue

            # Prefer JPX's explicit public-offer/sale-price column.
            public_price = None
            if len(next_cells) >= 4:
                candidate = clean_text(next_cells[3])
                if JPX_PRICE_RE.fullmatch(candidate):
                    public_price = number_from_price(candidate)

            # Fallback: search the second row for a standalone numeric price.
            if public_price is None:
                for cell in next_cells:
                    candidate = clean_text(cell)
                    if not candidate or "OA" in candidate.upper():
                        continue
                    if JPX_PRICE_RE.fullmatch(candidate):
                        value = number_from_price(candidate)
                        if value is not None:
                            public_price = value
                            break

            result.append(
                {
                    "listedDate": listing_date,
                    "code4": code,
                    "code": jq_code,
                    "name": name,
                    "market": market,
                    "publicPrice": public_price,
                    "technicalListing": False,
                    "source": "JPX",
                    "sourceUrl": url,
                }
            )

    return result


def fetch_jpx_ipos(today):
    all_rows = []
    seen = set()
    page_success = 0

    for url in JPX_URLS:
        try:
            rows = parse_jpx_page(url, today)
            page_success += 1
            for row in rows:
                key = (row["listedDate"], row["code4"])
                if key not in seen:
                    seen.add(key)
                    all_rows.append(row)
        except Exception as exc:
            print(f"JPX取得失敗: {url}: {exc}", file=sys.stderr)

    if page_success == 0:
        raise RuntimeError("JPXの全ページ取得に失敗しました。空データは書き出しません。")

    cutoff = today - dt.timedelta(days=LOOKBACK_DAYS)
    filtered = [
        row
        for row in all_rows
        if cutoff <= dt.date.fromisoformat(row["listedDate"]) <= today
    ]
    filtered.sort(key=lambda row: (row["listedDate"], row["code4"]), reverse=True)

    print(f"JPXから取得した対象IPO: {len(filtered)}件")
    if not filtered:
        raise RuntimeError(
            "JPXページは取得できましたが対象IPOが0件でした。"
            "JPXのHTML構造が変わった可能性があるため、空のlatest.jsonは作成しません。"
        )
    return filtered


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

            code_match = re.search(r"\b(\d{3,4}[A-Z]?)\b", row_text, re.I)
            if not code_match:
                continue
            code = normalize_code(code_match.group(1))

            # Rating is a standalone token. The site uses S/A/B/C/D.
            rating = None
            for cell in cells:
                token = clean_text(cell).upper()
                if token in ("S", "A", "B", "C", "D"):
                    rating = token
                    break

            market = None
            for cell in cells:
                for candidate in ALLOWED_MARKETS:
                    if candidate in cell:
                        market = candidate
                        break
                if market:
                    break

            md = re.search(r"\b(\d{1,2})/(\d{1,2})\b", row_text)
            if not md:
                continue
            listed_date = f"{year}-{int(md.group(1)):02d}-{int(md.group(2)):02d}"

            # Extract prices from cells. In the IPO result table the relevant
            # order is public price -> initial-price sale profit -> initial price.
            prices = []
            for cell in cells:
                for match in re.findall(r"([\d,]+(?:\.\d+)?)円", cell):
                    try:
                        prices.append(float(match.replace(",", "")))
                    except ValueError:
                        pass

            if len(prices) < 2:
                continue

            public_price = prices[0]
            initial_price = prices[-1]

            out[code] = {
                "listedDate": listed_date,
                "publicPrice": public_price,
                "initialPrice": initial_price,
                "initialReturnPct": round((initial_price / public_price - 1) * 100, 2)
                if public_price
                else None,
                "ipoRating": rating,
                "ipoSource": "庶民のIPO",
                "ipoSourceUrl": url,
                "marketFromIpoSite": market,
            }

    print(f"  {year}: {len(out)}件")
    return out


def fetch_ipokabu_data():
    result = {}
    for year, url in IPO_KABU_URLS.items():
        try:
            result.update(parse_ipokabu_year(year, url))
        except Exception as exc:
            print(f"庶民のIPO取得失敗 {year}: {exc}", file=sys.stderr)
    return result


def api_get_all(path, params=None):
    if not API_KEY:
        raise RuntimeError("環境変数 JQUANTS_API_KEY が設定されていません。")

    params = dict(params or {})
    results = []

    while True:
        qs = urllib.parse.urlencode(params)
        url = API_BASE + path + ("?" + qs if qs else "")
        req = urllib.request.Request(url, headers={"x-api-key": API_KEY})
        try:
            with urllib.request.urlopen(req, timeout=45) as res:
                body = json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")
            raise RuntimeError(f"J-Quants API error {exc.code}: {detail}") from exc
        except Exception as exc:
            raise RuntimeError(f"J-Quants通信エラー: {exc}") from exc

        results.extend(body.get("data", []))
        pagination_key = body.get("pagination_key")
        if not pagination_key:
            break
        params["pagination_key"] = pagination_key
        time.sleep(REQUEST_INTERVAL_SEC)

    return results


def fetch_price_history(code):
    rows = api_get_all("/equities/bars/daily", {"code": code})
    history = []
    latest_market_cap = None

    for row in rows:
        date = row.get("Date")
        close = row.get("C")
        if date and close is not None:
            history.append({"date": date[:10], "price": num(close)})
        if row.get("MktCap") is not None:
            try:
                latest_market_cap = float(row["MktCap"]) * 1_000_000
            except (TypeError, ValueError):
                pass

    history = [x for x in history if x["price"] is not None]
    history.sort(key=lambda x: x["date"])
    as_of = history[-1]["date"] if history else None
    return history, as_of, latest_market_cap


def to_yfinance_symbol(code):
    c = normalize_code(code)
    if len(c) == 5 and c[-1] == "0":
        c = c[:4]
    return c + ".T"


def fetch_yfinance_history(code, start_date, end_date):
    if yf is None:
        print("yfinanceが利用できません。", file=sys.stderr)
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

        close_series = df["Close"]
        if hasattr(close_series, "columns"):
            close_series = close_series.iloc[:, 0]

        result = []
        for index, value in close_series.items():
            try:
                price = float(value)
            except (TypeError, ValueError):
                continue
            if price != price:
                continue
            result.append({"date": index.strftime("%Y-%m-%d"), "price": price})
        return result
    except Exception as exc:
        print(f"yfinance取得エラー {code}/{symbol}: {exc}", file=sys.stderr)
        return []


def merge_price_histories(jq_history, yf_history, listed_date):
    # J-Quants wins on overlapping dates; yfinance fills the recent gap.
    by_date = {row["date"]: row for row in jq_history if row.get("price") is not None}
    for row in yf_history:
        if row.get("date") and row.get("price") is not None:
            by_date.setdefault(row["date"], row)

    start = dt.date.fromisoformat(listed_date)
    end = start + dt.timedelta(days=RETENTION_DAYS)
    return [
        by_date[d]
        for d in sorted(by_date)
        if start <= dt.date.fromisoformat(d) <= end
    ]


def price_on_or_after(history, target_date, max_gap_days=15):
    if not history:
        return None
    target = dt.date.fromisoformat(target_date)
    for row in history:
        day = dt.date.fromisoformat(row["date"])
        if day >= target:
            if (day - target).days <= max_gap_days:
                return row
            return None
    return None


def build_milestones(history, listed_date):
    base = dt.date.fromisoformat(listed_date)
    result = {}
    for days in (30, 90, 180, 365, 730):
        target = (base + dt.timedelta(days=days)).isoformat()
        row = price_on_or_after(history, target)
        result[f"price{days}d"] = row["price"] if row else None
        result[f"price{days}dDate"] = row["date"] if row else None
    return result


def num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_financials(code):
    rows = api_get_all("/fins/summary", {"code": code})
    rows = [row for row in rows if row.get("DiscDate")]
    rows.sort(key=lambda row: row["DiscDate"])

    # Keep the latest disclosure for each fiscal period, while preserving
    # revision history for the revision flags.
    by_type = {}
    for row in rows:
        by_type.setdefault(row.get("CurPerType"), []).append(row)

    growth_map = {}
    for records in by_type.values():
        records = sorted(records, key=lambda row: row.get("DiscDate") or "")
        previous = None
        for row in records:
            sales = num(row.get("Sales"))
            op = num(row.get("OP"))
            prev_sales = num(previous.get("Sales")) if previous else None
            prev_op = num(previous.get("OP")) if previous else None
            revenue_growth = ((sales - prev_sales) / prev_sales * 100) if sales is not None and prev_sales else None
            op_growth = ((op - prev_op) / prev_op * 100) if op is not None and prev_op else None
            growth_map[id(row)] = (revenue_growth, op_growth)
            previous = row

    by_fy = {}
    for row in rows:
        by_fy.setdefault(row.get("CurFYEn"), []).append(row)

    revision_map = {}
    for records in by_fy.values():
        records = sorted(records, key=lambda row: row["DiscDate"])
        for i, row in enumerate(records):
            up = down = False
            if i:
                prev_fnp = num(records[i - 1].get("FNP"))
                cur_fnp = num(row.get("FNP"))
                if prev_fnp is not None and cur_fnp is not None:
                    up = cur_fnp > prev_fnp
                    down = cur_fnp < prev_fnp
            revision_map[id(row)] = (up, down)

    financials = []
    for row in rows:
        sales = num(row.get("Sales"))
        op = num(row.get("OP"))
        net_profit = num(row.get("NP"))
        forecast_np = num(row.get("FNP"))
        revenue_growth, op_growth = growth_map.get(id(row), (None, None))
        revision_up, revision_down = revision_map.get(id(row), (False, False))

        roe = num(row.get("ROE"))
        equity_ratio = num(row.get("EqAR"))
        progress = (net_profit / forecast_np * 100) if net_profit is not None and forecast_np else None
        margin = (op / sales * 100) if op is not None and sales else None

        financials.append(
            {
                "reportDate": row.get("DiscDate"),
                "revenue": sales,
                "revenueGrowth": round(revenue_growth, 1) if revenue_growth is not None else None,
                "opProfit": op,
                "opProfitGrowth": round(op_growth, 1) if op_growth is not None else None,
                "ordinaryProfit": num(row.get("OdP")),
                "netProfit": net_profit,
                "eps": num(row.get("EPS")),
                "opMargin": round(margin, 1) if margin is not None else None,
                "roe": round(roe * 100, 1) if roe is not None and abs(roe) <= 2 else roe,
                "equityRatio": round(equity_ratio * 100, 1) if equity_ratio is not None and abs(equity_ratio) <= 2 else equity_ratio,
                "opCF": num(row.get("CFO")),
                "freeCF": None,
                "guidance": (
                    f"売上高予想 {row.get('FSales')} / 純利益予想 {row.get('FNP')}"
                    if row.get("FSales") or row.get("FNP")
                    else None
                ),
                "revisionUp": revision_up,
                "revisionDown": revision_down,
                "progressRate": round(progress, 1) if progress is not None else None,
            }
        )
    return financials


def latest_annual_financial(financials):
    if not financials:
        return None
    # Most recent disclosed record. This is intentionally simple and
    # transparent; the UI can use the latest record for valuation/quality.
    return sorted(financials, key=lambda x: x.get("reportDate") or "")[-1]


def main():
    today = dt.date.today()

    # 1. JPX defines the IPO universe.
    jpx_ipos = fetch_jpx_ipos(today)

    # 2. 庶民のIPO enriches rating and initial price data.
    ipokabu = fetch_ipokabu_data()
    print(f"庶民のIPOから取得した合計: {len(ipokabu)}件")

    output = []

    for index, ipo in enumerate(jpx_ipos, start=1):
        code4 = ipo["code4"]
        jq_code = ipo["code"]
        extra = ipokabu.get(code4, {})
        listed_date = ipo["listedDate"]

        public_price = ipo.get("publicPrice")
        if public_price is None:
            public_price = extra.get("publicPrice")

        initial_price = extra.get("initialPrice")
        initial_return = extra.get("initialReturnPct")
        if initial_return is None and public_price and initial_price:
            initial_return = round((initial_price / public_price - 1) * 100, 2)

        print(f"[{index}/{len(jpx_ipos)}] {code4} {ipo['name']} ({listed_date})")

        # J-Quants historical price data.
        time.sleep(REQUEST_INTERVAL_SEC)
        jq_history, jq_as_of, market_cap = fetch_price_history(jq_code)

        # yfinance fills the latest period missing from J-Quants Free.
        yf_end = today + dt.timedelta(days=1)
        yf_start = today - dt.timedelta(days=YFINANCE_LOOKBACK_DAYS)
        supplement_start = yf_start
        if jq_as_of:
            try:
                supplement_start = max(
                    yf_start,
                    dt.date.fromisoformat(jq_as_of) + dt.timedelta(days=1),
                )
            except ValueError:
                pass

        yf_history = fetch_yfinance_history(
            jq_code,
            supplement_start.isoformat(),
            yf_end.isoformat(),
        )
        price_history = merge_price_histories(jq_history, yf_history, listed_date)

        current_price = price_history[-1]["price"] if price_history else None
        price_as_of = price_history[-1]["date"] if price_history else jq_as_of
        milestones = build_milestones(price_history, listed_date)

        # Financial summary.
        time.sleep(REQUEST_INTERVAL_SEC)
        financials = fetch_financials(jq_code)
        latest_fin = latest_annual_financial(financials)

        current_per = None
        if market_cap and latest_fin and latest_fin.get("netProfit"):
            np_ = latest_fin["netProfit"]
            if np_ > 0:
                current_per = round(market_cap / np_, 2)

        output.append(
            {
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
                "priceAsOfDate": price_as_of,
                "marketCap": market_cap,
                "currentPER": current_per,
                "priceVsPublicPct": round((current_price / public_price - 1) * 100, 2)
                if current_price is not None and public_price
                else None,
                "priceVsInitialPct": round((current_price / initial_price - 1) * 100, 2)
                if current_price is not None and initial_price
                else None,
                **milestones,
                "priceHistory": price_history,
                "financials": financials,
                "dataSource": "JPX + 庶民のIPO + J-Quants + yfinance",
                "dataRetrievedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
        )

    os.makedirs(os.path.dirname(OUTPUT_PATH) or ".", exist_ok=True)
    temp_path = OUTPUT_PATH + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(output, file, ensure_ascii=False, indent=2)
    os.replace(temp_path, OUTPUT_PATH)

    print(f"書き出し完了: {OUTPUT_PATH}（{len(output)}銘柄）")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
