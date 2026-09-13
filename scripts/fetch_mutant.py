#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Fetch the latest Mutant monthly report and publish IPO alerts/dropouts.

The PDF itself contains the official security codes in the "top 10 overview"
page.  Matching is therefore code-only; company-name fuzzy matching is never
used.  This deliberately follows V22's code-first data policy.
"""

from __future__ import annotations

import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from pypdf import PdfReader


ROOT = Path(__file__).resolve().parents[1]
IPO_FILE = ROOT / "docs" / "latest.json"
OUT = ROOT / "docs" / "mutant-latest.json"

FUND_CODE = "955248"
FUND_NAME = "ミュータント"
REPORT_API = (
    "https://www.amova-am.com/api/fund-japan"
    "?report=1&test=test&nri=1&funds[]=955248"
)
PDF_BASE = (
    "https://www.amova-am.com/files/fund/reports/"
    "955248/monthly/{filename}"
)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) "
        "AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9",
}
CODE_RE = re.compile(r"(?:\d{4}|\d{3}[A-Z])", re.I)


def get_bytes(url: str, timeout: int = 45) -> bytes:
    req = Request(url, headers=HEADERS)
    with urlopen(req, timeout=timeout) as response:
        return response.read()


def canonical_code(value: Any) -> str | None:
    s = str(value or "").strip().upper().replace(".T", "")
    m = re.fullmatch(r"((?:\d{4}|\d{3}[A-Z]))0", s)
    if m:
        s = m.group(1)
    return s if re.fullmatch(r"(?:\d{4}|\d{3}[A-Z])", s) else None


def latest_report() -> tuple[str, str]:
    payload = json.loads(get_bytes(REPORT_API).decode("utf-8"))
    reports = payload["data"]["funds"][FUND_CODE]["files"]["monthly"]
    valid = [
        r for r in reports
        if re.fullmatch(r"\d{6}\.pdf", str(r.get("filename", "")))
    ]
    if not valid:
        raise RuntimeError("monthly report was not found in the official API")
    latest = max(valid, key=lambda r: r["filename"])
    filename = latest["filename"]
    return filename[:6], PDF_BASE.format(filename=filename)


def extract_pages(pdf_bytes: bytes) -> list[str]:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text(extraction_mode="layout") or "")
        except TypeError:
            pages.append(page.extract_text() or "")
    return pages


def extract_as_of_date(pages: list[str]) -> str | None:
    for text in pages[:3]:
        m = re.search(r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日現在", text)
        if m:
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


def parse_top10(pages: list[str]) -> list[dict[str, Any]]:
    ratio_by_rank: dict[int, float] = {}
    name_by_rank: dict[int, str] = {}
    code_by_rank: dict[int, str] = {}

    for text in pages:
        if "株式組入上位10銘柄" in text and "銘柄概要" not in text:
            section = text.split("株式組入上位10銘柄", 1)[1]
            section = section.split("※個別", 1)[0]
            for line in section.splitlines():
                m = re.match(r"\s*(10|[1-9])\s+(.+?)\s+日本円\s+.+?\s+(\d+(?:\.\d+)?)%\s*$", line)
                if m:
                    rank = int(m.group(1))
                    name_by_rank[rank] = re.sub(r"\s+", " ", m.group(2)).strip()
                    ratio_by_rank[rank] = float(m.group(3))

        if "組入上位10銘柄の銘柄概要" in text:
            section = text.split("組入上位10銘柄の銘柄概要", 1)[1]
            for line in section.splitlines():
                m = re.match(
                    r"\s*(10|[1-9])\s+((?:\d{4}|\d{3}[A-Z]))\s+(.+?)\s*$",
                    line,
                    re.I,
                )
                if m:
                    rank = int(m.group(1))
                    code_by_rank[rank] = m.group(2).upper()
                    if rank not in name_by_rank:
                        name_by_rank[rank] = re.sub(r"\s+", " ", m.group(3)).strip()

    holdings = []
    for rank in range(1, 11):
        if rank not in code_by_rank or rank not in name_by_rank:
            raise RuntimeError(
                f"PDF parse safety stop: rank {rank} was not read completely"
            )
        holdings.append({
            "rank": rank,
            "code": code_by_rank[rank],
            "name": name_by_rank[rank],
            "ratio": ratio_by_rank.get(rank),
        })

    codes = [x["code"] for x in holdings]
    if len(codes) != 10 or len(codes) != len(set(codes)):
        raise RuntimeError("PDF parse safety stop: top 10 codes are duplicated")
    return holdings


def load_ipos() -> dict[str, dict[str, Any]]:
    if not IPO_FILE.exists():
        raise RuntimeError(f"IPO data is missing: {IPO_FILE}")
    raw = json.loads(IPO_FILE.read_text(encoding="utf-8"))
    items = raw if isinstance(raw, list) else raw.get("ipos", raw.get("stocks", []))
    result = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        code = canonical_code(item.get("code4") or item.get("code"))
        if code:
            result[code] = item
    return result


def load_previous() -> dict[str, Any]:
    if not OUT.exists():
        return {}
    try:
        raw = json.loads(OUT.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def build_output(
    report_month: str,
    pdf_url: str,
    pages: list[str],
    holdings: list[dict[str, Any]],
) -> dict[str, Any]:
    ipos = load_ipos()
    previous = load_previous()
    previous_holdings = {
        canonical_code(x.get("code")): x
        for x in previous.get("holdings", [])
        if isinstance(x, dict) and canonical_code(x.get("code"))
    }
    current_codes = {x["code"] for x in holdings}

    enriched = []
    ipo_alerts = []
    for item in holdings:
        row = dict(item)
        ipo = ipos.get(item["code"])
        row["isIpo"] = bool(ipo)
        row["ipoListedDate"] = ipo.get("listedDate") if ipo else None
        enriched.append(row)
        if ipo:
            ipo_alerts.append(dict(row))

    dropped = []
    if previous.get("reportMonth") and previous.get("reportMonth") != report_month:
        for code, item in previous_holdings.items():
            if code not in current_codes:
                dropped.append({
                    "previousRank": item.get("rank"),
                    "code": code,
                    "name": item.get("name"),
                    "wasIpo": bool(item.get("isIpo")),
                })
        dropped.sort(key=lambda x: x.get("previousRank") or 99)
    elif previous.get("reportMonth") == report_month:
        # Re-running the same report must not erase the already detected dropouts.
        dropped = previous.get("events", {}).get("dropped", [])

    return {
        "schemaVersion": 1,
        "fundCode": FUND_CODE,
        "fundName": FUND_NAME,
        "reportMonth": report_month,
        "asOfDate": extract_as_of_date(pages),
        "sourcePdfUrl": pdf_url,
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "holdings": enriched,
        "events": {
            "ipoAlerts": ipo_alerts,
            "dropped": dropped,
        },
    }


def main() -> None:
    report_month, pdf_url = latest_report()
    previous = load_previous()
    if previous.get("reportMonth") == report_month:
        print(f"[MUTANT] {report_month} is already current; no update")
        return

    pdf_bytes = get_bytes(pdf_url)
    pages = extract_pages(pdf_bytes)
    holdings = parse_top10(pages)
    result = build_output(report_month, pdf_url, pages, holdings)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(OUT)
    print(
        f"[MUTANT] report={report_month} top10={len(holdings)} "
        f"ipoAlerts={len(result['events']['ipoAlerts'])} "
        f"dropped={len(result['events']['dropped'])}"
    )


if __name__ == "__main__":
    main()
