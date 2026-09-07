#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
J-Quants API（無料プラン）から、直近365日以内に上場した銘柄の
・上場銘柄情報
・過去株価（無料プランのため直近12週間は取得不可＝データ基準日が過去になる）
・財務情報（四半期累計値）
を取得し、IPOモニターアプリが読み込めるJSON形式で書き出します。

【重要な注意】
- J-Quants APIの正式な仕様（エンドポイント名・パラメータ名・レスポンスの
  フィールド名）は変更される場合があります。本スクリプトは執筆時点で
  確認できた情報をもとにした「たたき台」です。実行してエラーになる場合は
  必ず公式ドキュメント（https://jpx-jquants.com/ja/spec）で最新のエンド
  ポイント仕様をご確認のうえ、該当箇所を書き換えてください。
- 無料プランのレートリミットは 5 リクエスト/分 です。銘柄数が多い場合は
  time.sleep で間隔を空けています。
- 取得できなかった値は None のままにし、推測値で埋めることはしません。
- 本スクリプトはJ-Quants APIの利用規約に従い、取得データを「閲覧可能な形で
  第三者に再配布」しない前提（＝あなた個人のiPadアプリでのみ使用）で
  設計されています。GitHubリポジトリを公開にする場合、このJSON出力を
  公開ディレクトリに置くこと自体は「あなた個人が加工した派生データを
  自分のアプリで使う」目的の範囲内かご自身でも規約をご確認ください。
  不安な場合はリポジトリをPrivateにし、GitHub Pro（月4ドル程度）で
  Private リポジトリのGitHub Pagesを使う運用にしてください。
"""

import os
import sys
import time
import json
import datetime
import urllib.request
import urllib.error

API_BASE = "https://api.jquants.com/v2"
API_KEY = os.environ.get("JQUANTS_API_KEY", "")
LOOKBACK_DAYS = int(os.environ.get("IPO_LOOKBACK_DAYS", "365"))
OUTPUT_PATH = os.environ.get("OUTPUT_PATH", "docs/latest.json")
REQUEST_INTERVAL_SEC = 13  # 5リクエスト/分 の制限に余裕を持って対応


def api_get(path, params=None):
    if not API_KEY:
        print("環境変数 JQUANTS_API_KEY が設定されていません。", file=sys.stderr)
        sys.exit(1)
    url = API_BASE + path
    if params:
        qs = urllib.parse.urlencode(params)
        url = url + "?" + qs
    req = urllib.request.Request(url, headers={"x-api-key": API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"APIエラー {e.code} : {url}", file=sys.stderr)
        print(e.read().decode("utf-8", "ignore"), file=sys.stderr)
        return None


def fetch_listed_master():
    """上場銘柄一覧を取得。フィールド名は要確認（例: Code, CompanyName, MarketCode, ListingDate 等）。"""
    data = api_get("/equities/master")
    if not data:
        return []
    # レスポンスの実際のキー名に合わせて調整してください
    return data.get("info", data.get("equities", []))


def within_lookback(listing_date_str, lookback_days):
    if not listing_date_str:
        return False
    try:
        d = datetime.datetime.strptime(listing_date_str[:10], "%Y-%m-%d").date()
    except ValueError:
        return False
    return (datetime.date.today() - d).days <= lookback_days


def fetch_price_history(code):
    data = api_get("/equities/bars/daily", {"code": code})
    if not data:
        return [], None
    bars = data.get("daily_quotes", data.get("bars", []))
    history = []
    as_of = None
    for b in bars:
        date = b.get("Date") or b.get("date")
        close = b.get("Close") or b.get("close")
        if date and close is not None:
            history.append({"date": date[:10], "price": close})
            as_of = date[:10]
    return history, as_of


def fetch_financials(code):
    data = api_get("/fins/summary", {"code": code})
    if not data:
        return []
    records = data.get("statements", data.get("summary", []))
    financials = []
    for r in records:
        financials.append({
            "reportDate": (r.get("DisclosedDate") or r.get("disclosed_date") or "")[:10],
            "revenue": r.get("NetSales") or r.get("Sales"),
            "opProfit": r.get("OperatingProfit") or r.get("OP"),
            "ordinaryProfit": r.get("OrdinaryProfit") or r.get("OdP"),
            "netProfit": r.get("Profit") or r.get("NP"),
            "eps": r.get("EarningsPerShare") or r.get("EPS"),
            # 成長率(YoY)は前年同期のレコードとの比較が必要。
            # J-Quants側にYoYフィールドが無い場合はここで自前計算してください。
            "revenueGrowth": None,
            "opProfitGrowth": None,
            "opMargin": None,
            "roe": None,
            "equityRatio": r.get("EquityToAssetRatio"),
            "opCF": r.get("CashFlowsFromOperatingActivities"),
            "freeCF": None,
            "guidance": None,
            "revisionUp": None,
            "revisionDown": None,
            "progressRate": None,
        })
    return financials


def main():
    master = fetch_listed_master()
    targets = [m for m in master if within_lookback(
        m.get("ListingDate") or m.get("listing_date"), LOOKBACK_DAYS)]

    print(f"直近{LOOKBACK_DAYS}日以内の上場銘柄: {len(targets)}件")

    output = []
    for m in targets:
        code = m.get("Code") or m.get("code")
        if not code:
            continue
        name = m.get("CompanyName") or m.get("company_name")
        market = m.get("MarketCodeName") or m.get("market")
        listed_date = (m.get("ListingDate") or m.get("listing_date") or "")[:10]

        time.sleep(REQUEST_INTERVAL_SEC)
        price_history, as_of = fetch_price_history(code)
        time.sleep(REQUEST_INTERVAL_SEC)
        financials = fetch_financials(code)

        current_price = price_history[-1]["price"] if price_history else None

        output.append({
            "code": code,
            "name": name,
            "market": market,
            "listedDate": listed_date,
            "currentPrice": current_price,
            "priceAsOfDate": as_of,
            "priceHistory": price_history,
            "financials": financials,
            "dataSource": "J-Quants API（無料プラン・約12週間遅延）",
            "dataRetrievedAt": datetime.datetime.utcnow().isoformat() + "Z",
        })

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"書き出し完了: {OUTPUT_PATH}（{len(output)}銘柄）")


if __name__ == "__main__":
    import urllib.parse
    main()
