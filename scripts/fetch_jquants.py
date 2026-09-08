#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
J-Quants API（無料プラン・V2）から、直近365日以内に新規上場したとみられる銘柄の
・過去株価（AdjC＝分割等調整済み終値を採用）
・財務情報（決算短信サマリー）
を取得し、IPOモニターアプリが読み込めるJSON形式で書き出します。

【上場日の特定方法について（重要）】
J-Quants API「上場銘柄一覧」(/v2/equities/master) には上場日という項目が
存在しません（公式仕様として提供されていません）。そのため本スクリプトでは、
「約365日前時点の銘柄一覧」と「現在（無料プランのため実際は最大12週間前）の
銘柄一覧」を比較し、新しく出現した銘柄コード＝直近で新規上場した銘柄、と
みなす方式を採用しています。まれに市場区分変更・銘柄コード変更等でも
新規出現として検出される場合がありますが、アプリ側で銘柄名を見て手動で
削除できます。

【注意】
- V2 APIの仕様（エンドポイント・フィールド名）は変更される可能性があります。
  最新は https://jpx-jquants.com/ja/spec をご確認ください。
- 無料プランのレートリミットは 5 リクエスト/分。
- 取得できなかった値は None のままにし、推測値で埋めません。
- 成長率(YoY)・進捗率・上方/下方修正は、開示された複数期のデータから
  このスクリプトが計算した推定値です。決算短信の記載と完全には一致しない
  場合があるため、精度を重視する場合はアプリの「決算PDFから読み取り」機能を
  ご利用ください。
"""

import os
import sys
import time
import json
import datetime
import urllib.request
import urllib.parse
import urllib.error

API_BASE = "https://api.jquants.com/v2"
API_KEY = os.environ.get("JQUANTS_API_KEY", "")
LOOKBACK_DAYS = int(os.environ.get("IPO_LOOKBACK_DAYS", "365"))
OUTPUT_PATH = os.environ.get("OUTPUT_PATH", "docs/latest.json")
REQUEST_INTERVAL_SEC = 13  # 5リクエスト/分 に余裕を持って対応


def api_get_all(path, params=None):
    """pagination_key を辿って全件取得する。'data' 配列を結合して返す。"""
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
            with urllib.request.urlopen(req, timeout=30) as res:
                body = json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            print(f"APIエラー {e.code} : {url}", file=sys.stderr)
            print(e.read().decode("utf-8", "ignore"), file=sys.stderr)
            break
        except Exception as e:
            print(f"通信エラー: {url} : {e}", file=sys.stderr)
            break
        chunk = body.get("data", [])
        results.extend(chunk)
        pk = body.get("pagination_key")
        if not pk:
            break
        params["pagination_key"] = pk
        time.sleep(REQUEST_INTERVAL_SEC)
    return results


def fetch_master(date_str=None):
    """上場銘柄一覧。date_str未指定なら実行可能な最新日（無料プランは遅延あり）。"""
    params = {}
    if date_str:
        params["date"] = date_str
    return api_get_all("/equities/master", params)


def fetch_price_history(code):
    """指定銘柄の全期間の株価（無料プランの提供範囲内）。"""
    rows = api_get_all("/equities/bars/daily", {"code": code})
    history = []
    as_of = None
    latest_market_cap = None
    for r in rows:
        date = r.get("Date")
        adj_close = r.get("AdjC")
        close = r.get("C")
        price = adj_close if adj_close is not None else close
        if date and price is not None:
            history.append({"date": date[:10], "price": price})
            as_of = date[:10]
            if r.get("MktCap") is not None:
                latest_market_cap = r.get("MktCap") * 1_000_000  # 百万円→円
    history.sort(key=lambda x: x["date"])
    return history, as_of, latest_market_cap


def num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


STOOQ_INTERVAL_SEC = 2  # stooqは明文化されたレート制限が無いため、節度をもって間隔を空ける


def to_stooq_symbol(jquants_code):
    """J-Quantsの5桁コード（末尾は種別コード）から、stooq用の伝統的な4桁コードを組み立てる。"""
    code = jquants_code
    if len(code) == 5:
        code = code[:4]
    return code.lower() + ".jp"


def fetch_stooq_latest(jquants_code):
    """
    stooq.comが自サイトで公開している「データダウンロード」機能のCSVを取得し、
    直近の終値・日付を返す。取得できない場合は (None, None) を返す
    （推測値は使わず、取得できなかった旨をそのまま伝える）。
    """
    symbol = to_stooq_symbol(jquants_code)
    url = f"https://stooq.com/q/d/l/?s={symbol}&i=d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (personal-use IPO tracker)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            text = res.read().decode("utf-8", "ignore")
    except Exception as e:
        print(f"stooq取得エラー（{jquants_code}）: {e}", file=sys.stderr)
        return None, None
    lines = [l for l in text.strip().splitlines() if l.strip()]
    if len(lines) < 2 or lines[0].startswith("No data") or "Exceeded" in lines[0]:
        return None, None
    last = lines[-1].split(",")
    if len(last) < 5:
        return None, None
    date_str, close_str = last[0], last[4]
    price = num(close_str)
    if price is None:
        return None, None
    return price, date_str


def fetch_financials(code):
    """指定銘柄の全期間の決算短信サマリーから、成長率・進捗率・修正状況を推定して整形する。"""
    rows = api_get_all("/fins/summary", {"code": code})
    # 開示日順に並べる
    rows = [r for r in rows if r.get("DiscDate")]
    rows.sort(key=lambda r: r["DiscDate"])

    # 同じ会計期間タイプ（1Q/2Q/3Q/4Q/FY）ごとにグルーピングしてYoY成長率を計算
    by_type = {}
    for r in rows:
        by_type.setdefault(r.get("CurPerType"), []).append(r)

    growth_map = {}  # DiscNo等のキーではなく id(r) で対応付け
    for per_type, recs in by_type.items():
        recs_sorted = sorted(recs, key=lambda r: r.get("CurPerSt") or "")
        for i, r in enumerate(recs_sorted):
            prev = recs_sorted[i - 1] if i > 0 else None
            sales = num(r.get("Sales"))
            op = num(r.get("OP"))
            prev_sales = num(prev.get("Sales")) if prev else None
            prev_op = num(prev.get("OP")) if prev else None
            rev_g = ((sales - prev_sales) / prev_sales * 100) if (sales is not None and prev_sales) else None
            op_g = ((op - prev_op) / prev_op * 100) if (op is not None and prev_op) else None
            growth_map[id(r)] = (rev_g, op_g)

    # 通期予想の修正判定（同一事業年度＝CurFYEn内でのFNPの変化を比較）
    by_fy = {}
    for r in rows:
        by_fy.setdefault(r.get("CurFYEn"), []).append(r)
    revision_map = {}
    for fy, recs in by_fy.items():
        recs_sorted = sorted(recs, key=lambda r: r["DiscDate"])
        for i, r in enumerate(recs_sorted):
            if i == 0:
                revision_map[id(r)] = (False, False)
                continue
            prev_fnp = num(recs_sorted[i - 1].get("FNP"))
            cur_fnp = num(r.get("FNP"))
            up = down = False
            if prev_fnp is not None and cur_fnp is not None:
                if cur_fnp > prev_fnp:
                    up = True
                elif cur_fnp < prev_fnp:
                    down = True
            revision_map[id(r)] = (up, down)

    financials = []
    for r in rows:
        sales = num(r.get("Sales"))
        op = num(r.get("OP"))
        np_ = num(r.get("NP"))
        fnp = num(r.get("FNP"))
        rev_g, op_g = growth_map.get(id(r), (None, None))
        up, down = revision_map.get(id(r), (False, False))
        eq_ar = num(r.get("EqAR"))
        roe = num(r.get("ROE"))
        progress = (np_ / fnp * 100) if (np_ is not None and fnp) else None
        op_margin = (op / sales * 100) if (op is not None and sales) else None
        financials.append({
            "reportDate": r.get("DiscDate"),
            "revenue": sales,
            "revenueGrowth": round(rev_g, 1) if rev_g is not None else None,
            "opProfit": op,
            "opProfitGrowth": round(op_g, 1) if op_g is not None else None,
            "ordinaryProfit": num(r.get("OdP")),
            "netProfit": np_,
            "eps": num(r.get("EPS")),
            "opMargin": round(op_margin, 1) if op_margin is not None else None,
            "roe": round(roe * 100, 1) if roe is not None else None,
            "equityRatio": round(eq_ar * 100, 1) if eq_ar is not None else None,
            "opCF": num(r.get("CFO")),
            "freeCF": None,  # J-Quantsは投資CFのみでフリーCFは非提供。CFO+CFIで概算可能だが今回は空欄とする
            "guidance": (f"売上高予想 {r.get('FSales')} / 純利益予想 {r.get('FNP')}"
                         if r.get("FSales") or r.get("FNP") else None),
            "revisionUp": up,
            "revisionDown": down,
            "progressRate": round(progress, 1) if progress is not None else None,
        })
    return financials


ALLOWED_MARKET_KEYWORDS = ["プライム", "スタンダード", "グロース"]
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "730"))  # 既定2年


def is_allowed_market(market_name):
    if not market_name:
        return False
    return any(kw in market_name for kw in ALLOWED_MARKET_KEYWORDS)


def is_common_stock_code(code):
    """
    J-Quantsの5桁銘柄コードは末尾1桁が株式の種類を表す。
    末尾が0の場合は普通株式、それ以外（優先株式・種類株式等）は除外する。
    5桁以外の形式は判定できないため対象に含める（保守的に除外しすぎない）。
    """
    if len(code) == 5:
        return code[-1] == "0"
    return True


def load_previous_output():
    """前回生成したdocs/latest.jsonがあれば、銘柄コード→初出日(firstSeenDate)を復元する。"""
    if not os.path.exists(OUTPUT_PATH):
        return {}
    try:
        with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"前回データの読み込みに失敗: {e}", file=sys.stderr)
        return {}
    result = {}
    for item in data:
        code = item.get("code")
        if not code:
            continue
        first_seen = item.get("firstSeenDate")
        if not first_seen:
            ra = item.get("dataRetrievedAt") or ""
            first_seen = ra[:10] if ra else datetime.date.today().isoformat()
        result[code] = first_seen
    return result


def main():
    today = datetime.date.today()
    lookback_date = (today - datetime.timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    print("現在の銘柄一覧を取得中…")
    current_master = fetch_master()
    if not current_master:
        print("銘柄一覧が空でした。APIキーやプラン設定をご確認ください。", file=sys.stderr)
    current_date = current_master[0].get("Date") if current_master else None
    print(f"現在の銘柄一覧: {len(current_master)}件（データ基準日: {current_date}）")
    current_by_code = {str(m.get("Code")).strip(): m for m in current_master if m.get("Code")}

    time.sleep(REQUEST_INTERVAL_SEC)
    print(f"{lookback_date} 時点の銘柄一覧を取得中…")
    old_master = fetch_master(lookback_date)
    print(f"{lookback_date}時点の銘柄一覧: {len(old_master)}件")

    old_codes = {str(m.get("Code")).strip() for m in old_master if m.get("Code")}
    newly_diffed = [c for c in current_by_code if c not in old_codes and is_common_stock_code(c)]
    print(f"新規出現とみなした銘柄: {len(newly_diffed)}件")

    SAFETY_LIMIT = int(os.environ.get("SAFETY_LIMIT", "300"))
    if len(newly_diffed) > SAFETY_LIMIT:
        print(f"新規出現件数が{SAFETY_LIMIT}件を超えました。"
              f"365日前データの取得に失敗している可能性が高いため、処理を中断します。"
              f"（old_master件数={len(old_master)}）", file=sys.stderr)
        sys.exit(1)

    # 前回すでに追跡していた銘柄（保持期間内）を引き継ぐ
    previous = load_previous_output()
    print(f"前回データに存在した銘柄: {len(previous)}件")

    # 追跡対象コード → 初出日（firstSeenDate）のマップを作る
    target_first_seen = dict(previous)
    for code in newly_diffed:
        target_first_seen.setdefault(code, today.isoformat())

    # 市場フィルタ（プライム/スタンダード/グロースのみ）＋優先株式の除外＋現在の一覧に存在しない銘柄を除外
    filtered_targets = {}
    for code, first_seen in target_first_seen.items():
        m = current_by_code.get(code)
        if not m:
            continue  # 現在の一覧に存在しない（上場廃止等）→対象から外す
        if not is_common_stock_code(code):
            continue  # 優先株式等（コード末尾が0以外）→対象から外す
        if not is_allowed_market(m.get("MktNm")):
            continue
        filtered_targets[code] = first_seen

    # 保持期間（既定2年）を超えたものは対象から外す
    final_targets = {}
    for code, first_seen in filtered_targets.items():
        try:
            fs_date = datetime.date.fromisoformat(first_seen)
        except ValueError:
            fs_date = today
        if (today - fs_date).days <= RETENTION_DAYS:
            final_targets[code] = first_seen

    print(f"最終的な処理対象銘柄（市場フィルタ・保持期間適用後）: {len(final_targets)}件")

    output = []
    for code, first_seen in final_targets.items():
        m = current_by_code[code]
        name = m.get("CoName")
        market = m.get("MktNm")

        time.sleep(REQUEST_INTERVAL_SEC)
        price_history, as_of, market_cap = fetch_price_history(code)
        time.sleep(REQUEST_INTERVAL_SEC)
        financials = fetch_financials(code)

        current_price = price_history[-1]["price"] if price_history else None
        price_source = "j-quants"
        price_as_of = as_of

        time.sleep(STOOQ_INTERVAL_SEC)
        stooq_price, stooq_date = fetch_stooq_latest(code)
        if stooq_price is not None:
            current_price = stooq_price
            price_as_of = stooq_date
            price_source = "stooq"
            if stooq_date and (not price_history or price_history[-1]["date"] != stooq_date):
                price_history.append({"date": stooq_date, "price": stooq_price})

        output.append({
            "code": code,
            "name": name,
            "market": market,
            "listedDate": None,  # J-Quantsからは取得不可。アプリ側で手入力/確認してください
            "firstSeenDate": first_seen,
            "currentPrice": current_price,
            "priceAsOfDate": price_as_of,
            "priceSourceOverride": price_source,
            "marketCap": market_cap,
            "priceHistory": price_history,
            "financials": financials,
            "dataSource": "J-Quants API（無料プラン・約12週間遅延）",
            "dataRetrievedAt": datetime.datetime.utcnow().isoformat() + "Z",
        })

    os.makedirs(os.path.dirname(OUTPUT_PATH) or ".", exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"書き出し完了: {OUTPUT_PATH}（{len(output)}銘柄）")


if __name__ == "__main__":
    main()
