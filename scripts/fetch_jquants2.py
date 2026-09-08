#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPOモニター用データ取得

データ方針
- IPO一覧：JPX「新規上場会社情報」から過去2年分を取得
- 対象市場：東証プライム／スタンダード／グロースのみ
- TOKYO PRO Market、テクニカル上場、優先株・種類株等は除外
- 株価：J-Quants Freeで取得できる過去2年分を主データとする
- J-Quants Freeの直近12週間の空白だけをyfinanceで補完
- 同一日の重複はJ-Quantsを優先
- 上場日から2年間の価格推移をJSONへ保存
"""

import os
import sys
import time
import json
import datetime as dt
import urllib.request
import urllib.parse
import urllib.error

import pandas as pd
import yfinance as yf

API_BASE = "https://api.jquants.com/v2"
API_KEY = os.environ.get("JQUANTS_API_KEY", "")
OUTPUT_PATH = os.environ.get("OUTPUT_PATH", "docs/latest.json")
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "730"))
RECENT_DAYS = int(os.environ.get("YFINANCE_RECENT_DAYS", "84"))
REQUEST_INTERVAL_SEC = 13  # J-Quants Free: 5 req/min
JPX_NEW_LISTINGS_URLS = [
    "https://www.jpx.co.jp/listing/stocks/new/index.html",
    "https://www.jpx.co.jp/listing/stocks/new/00-archives-01.html",
    "https://www.jpx.co.jp/listing/stocks/new/00-archives-02.html",
    "https://www.jpx.co.jp/listing/stocks/new/00-archives-03.html",
]
ALLOWED_MARKETS = {"プライム", "スタンダード", "グロース"}


def api_get_all(path, params=None):
    if not API_KEY:
        print("JQUANTS_API_KEY が設定されていません。", file=sys.stderr)
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
            print(f"J-Quants API error {e.code}: {url}", file=sys.stderr)
            print(e.read().decode("utf-8", "ignore"), file=sys.stderr)
            break
        except Exception as e:
            print(f"J-Quants communication error: {e}", file=sys.stderr)
            break
        results.extend(body.get("data", []))
        pk = body.get("pagination_key")
        if not pk:
            break
        params["pagination_key"] = pk
        time.sleep(REQUEST_INTERVAL_SEC)
    return results


def fetch_master(date_str=None):
    return api_get_all("/equities/master", {"date": date_str} if date_str else {})


def num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def is_allowed_market(market_name):
    if not market_name:
        return False
    return any(x in market_name for x in ALLOWED_MARKETS)


def is_common_stock_code(code):
    code = str(code or "").strip()
    # J-Quantsの5桁コードでは末尾0が普通株式。
    # 4桁コードは通常の株式コードとして扱う。
    return not (len(code) == 5 and code[-1] != "0")


def normalize_code(code):
    code = str(code).strip()
    return code[:-1] if len(code) == 5 and code[-1].isdigit() else code


def yfinance_symbol(code):
    return f"{normalize_code(code)}.T"


def fetch_jpx_ipo_list():
    """JPX公式の新規上場一覧を取得。2年以内の対象市場だけ返す。"""
    today = dt.date.today()
    cutoff = today - dt.timedelta(days=RETENTION_DAYS)
    rows = {}
    for url in JPX_NEW_LISTINGS_URLS:
        try:
            tables = pd.read_html(url)
        except Exception as e:
            print(f"JPX新規上場一覧の取得失敗: {url}: {e}", file=sys.stderr)
            continue
        if not tables:
            continue
        # 対象ページは通常1つの大きな表。列名の探索も行う。
        for table in tables:
            if table.empty:
                continue
            text = " ".join(str(x) for x in table.columns)
            if "コード" not in text and not any("コード" in str(x) for x in table.iloc[0].tolist()):
                continue
            # 2段ヘッダーを平坦化
            if isinstance(table.columns, pd.MultiIndex):
                table.columns = [" ".join(str(x) for x in col if str(x) != "nan").strip() for col in table.columns]
            else:
                table.columns = [str(x).strip() for x in table.columns]
            for _, r in table.iterrows():
                vals = [str(v).strip() for v in r.tolist()]
                joined = " | ".join(vals)
                # 日付、コード、対象市場を表行から抽出
                date_match = None
                for v in vals:
                    try:
                        d = pd.to_datetime(v, errors="raise").date()
                        if dt.date(2024, 1, 1) <= d <= today + dt.timedelta(days=365):
                            date_match = d
                            break
                    except Exception:
                        pass
                if not date_match or date_match < cutoff or date_match > today:
                    continue
                code = None
                for v in vals:
                    if v.endswith("A") and v[:-1].isdigit() and 3 <= len(v[:-1]) <= 4:
                        code = v
                        break
                    if v.isdigit() and len(v) == 4:
                        code = v
                        break
                if not code:
                    continue
                market = next((v for v in vals if v in ALLOWED_MARKETS), None)
                if not market:
                    continue
                # JPXはテクニカル上場に * / ** を付すため、表行の会社名等に記号があれば除外。
                if "*" in joined:
                    continue
                # 会社名はコード直前の文字列を優先。取得できなくても後でJ-Quantsから補完。
                name = None
                try:
                    idx = vals.index(code)
                    if idx > 0 and vals[idx - 1] and vals[idx - 1] not in ALLOWED_MARKETS:
                        name = vals[idx - 1]
                except ValueError:
                    pass
                rows[code] = {
                    "code4": code,
                    "listedDate": date_match.isoformat(),
                    "market": market,
                    "name": name,
                    "source": url,
                }
    return rows


def fetch_price_history_jquants(code):
    rows = api_get_all("/equities/bars/daily", {"code": code})
    history = []
    market_cap = None
    for r in rows:
        date = r.get("Date")
        price = r.get("AdjC") if r.get("AdjC") is not None else r.get("C")
        if date and price is not None:
            history.append({"date": date[:10], "price": price, "source": "j-quants"})
            if r.get("MktCap") is not None:
                market_cap = r.get("MktCap") * 1_000_000
    history.sort(key=lambda x: x["date"])
    return history, (history[-1]["date"] if history else None), market_cap


def fetch_price_history_yfinance(code4, start_date, end_date):
    """J-Quantsで欠ける直近部分のみ取得。調整後終値を使用。"""
    ticker = yfinance_symbol(code4)
    try:
        df = yf.download(
            ticker,
            start=start_date.isoformat(),
            end=(end_date + dt.timedelta(days=1)).isoformat(),
            auto_adjust=False,
            progress=False,
            threads=False,
        )
        if df is None or df.empty:
            return []
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        col = "Adj Close" if "Adj Close" in df.columns else "Close"
        out = []
        for idx, row in df.iterrows():
            value = row.get(col)
            if pd.isna(value):
                continue
            out.append({"date": pd.Timestamp(idx).date().isoformat(), "price": float(value), "source": "yfinance"})
        return out
    except Exception as e:
        print(f"yfinance取得失敗 {code4}: {e}", file=sys.stderr)
        return []


def merge_history(jq_history, yf_history):
    merged = {r["date"]: r for r in jq_history}
    # 同日ならJ-Quantsを優先
    for r in yf_history:
        merged.setdefault(r["date"], r)
    return [merged[d] for d in sorted(merged)]


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
            growth_map[id(r)] = (
                ((sales - ps) / ps * 100) if sales is not None and ps else None,
                ((op - po) / po * 100) if op is not None and po else None,
            )
    by_fy = {}
    for r in rows:
        by_fy.setdefault(r.get("CurFYEn"), []).append(r)
    revision_map = {}
    for _, recs in by_fy.items():
        recs = sorted(recs, key=lambda r: r["DiscDate"])
        for i, r in enumerate(recs):
            prev_fnp = num(recs[i-1].get("FNP")) if i else None
            cur_fnp = num(r.get("FNP"))
            revision_map[id(r)] = ((cur_fnp > prev_fnp) if prev_fnp is not None and cur_fnp is not None else False,
                                   (cur_fnp < prev_fnp) if prev_fnp is not None and cur_fnp is not None else False)
    result = []
    for r in rows:
        sales, op, np_ = num(r.get("Sales")), num(r.get("OP")), num(r.get("NP"))
        fnp = num(r.get("FNP"))
        rev_g, op_g = growth_map.get(id(r), (None, None))
        up, down = revision_map.get(id(r), (False, False))
        result.append({
            "reportDate": r.get("DiscDate"),
            "revenue": sales,
            "revenueGrowth": round(rev_g, 1) if rev_g is not None else None,
            "opProfit": op,
            "opProfitGrowth": round(op_g, 1) if op_g is not None else None,
            "ordinaryProfit": num(r.get("OdP")),
            "netProfit": np_,
            "eps": num(r.get("EPS")),
            "opMargin": round(op / sales * 100, 1) if op is not None and sales else None,
            "roe": round(num(r.get("ROE")) * 100, 1) if num(r.get("ROE")) is not None else None,
            "equityRatio": round(num(r.get("EqAR")) * 100, 1) if num(r.get("EqAR")) is not None else None,
            "opCF": num(r.get("CFO")),
            "freeCF": None,
            "guidance": (f"売上高予想 {r.get('FSales')} / 純利益予想 {r.get('FNP')}" if r.get("FSales") or r.get("FNP") else None),
            "revisionUp": up,
            "revisionDown": down,
            "progressRate": round(np_ / fnp * 100, 1) if np_ is not None and fnp else None,
        })
    return result


def main():
    today = dt.date.today()
    cutoff = today - dt.timedelta(days=RETENTION_DAYS)

    print("JPX公式の新規上場一覧を取得中…")
    ipo_rows = fetch_jpx_ipo_list()
    print(f"JPXから取得した対象IPO: {len(ipo_rows)}件")

    # J-Quants masterは会社名・市場等の補完に利用する。
    print("J-Quants銘柄マスターを取得中…")
    master = fetch_master()
    current_by_code = {str(m.get("Code")).strip(): m for m in master if m.get("Code")}
    print(f"J-Quants master: {len(current_by_code)}件")

    output = []
    for n, (code4, ipo) in enumerate(sorted(ipo_rows.items(), key=lambda x: x[1]["listedDate"]), 1):
        code5 = code4 if len(code4) == 5 else code4 + "0"
        m = current_by_code.get(code5) or current_by_code.get(code4)
        if m and not is_common_stock_code(str(m.get("Code"))):
            continue
        market = ipo["market"]
        name = (m.get("CoName") if m else None) or ipo.get("name") or code4
        listed_date = dt.date.fromisoformat(ipo["listedDate"])

        # J-Quants: 上場日から現在まで取得できる範囲（Freeは直近12週間を除く）。
        time.sleep(REQUEST_INTERVAL_SEC)
        jq_history, jq_asof, market_cap = fetch_price_history_jquants(code5)

        # yfinance: J-Quantsの最新日以降だけを補完。最大直近12週間。
        recent_start = today - dt.timedelta(days=RECENT_DAYS)
        if jq_asof:
            next_day = dt.date.fromisoformat(jq_asof) + dt.timedelta(days=1)
            yf_start = max(recent_start, next_day)
        else:
            yf_start = max(listed_date, recent_start)
        yf_history = fetch_price_history_yfinance(code4, yf_start, today)

        history = merge_history(jq_history, yf_history)
        # 上場日から2年間だけ保持
        history = [r for r in history if listed_date <= dt.date.fromisoformat(r["date"]) <= listed_date + dt.timedelta(days=RETENTION_DAYS)]

        current_price = history[-1]["price"] if history else None
        price_asof = history[-1]["date"] if history else None
        price_source = history[-1]["source"] if history else None

        # 財務情報。J-Quants Freeで取得可能な範囲。
        time.sleep(REQUEST_INTERVAL_SEC)
        financials = fetch_financials(code5)

        output.append({
            "code": code5,
            "code4": code4,
            "name": name,
            "market": market,
            "listedDate": ipo["listedDate"],
            "firstSeenDate": ipo["listedDate"],
            "currentPrice": current_price,
            "priceAsOfDate": price_asof,
            "priceSourceOverride": price_source,
            "marketCap": market_cap,
            "priceHistory": history,
            "financials": financials,
            "dataSource": "JPX新規上場一覧 + J-Quants Free（過去2年・直近12週間遅延） + yfinance（不足する直近12週間）",
            "dataRetrievedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        })
        print(f"[{n}/{len(ipo_rows)}] {code4} {name}: JQ={len(jq_history)}日 / yfinance={len(yf_history)}日")

    os.makedirs(os.path.dirname(OUTPUT_PATH) or ".", exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"書き出し完了: {OUTPUT_PATH} ({len(output)}銘柄)")


if __name__ == "__main__":
    main()
