#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_twse_daily.py
由 GitHub Actions 排程每個交易日收盤後執行一次：
    1. 向證交所 OpenAPI 抓「上市個股日成交資訊」(STOCK_DAY_ALL)
    2. 算出每支股票的漲跌幅% (如果API有附漲跌欄位)
    3. 寫入 data/latest.json，給 cdp_scenario_builder.html 讀取

這支程式不需要任何API金鑰，純粹打公開資料的網址。
本機也可以直接執行測試：
    python fetch_twse_daily.py
"""
from __future__ import annotations
import json
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# 證交所「上市個股日成交資訊」OpenAPI（公開資料，免金鑰）
URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"

# 不同版本的API可能用不同的欄位名稱代表「漲跌價差」，多嘗試幾個
CHANGE_FIELD_CANDIDATES = ["Change", "PriceChange", "Diff", "漲跌價差"]

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "data", "latest.json")


def fetch_json(url: str) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw)


def to_float(value) -> float | None:
    """把證交所回傳的字串數字（可能含千分位逗號、+/-、空字串）轉成float"""
    if value is None:
        return None
    s = str(value).replace(",", "").strip()
    if s in ("", "--", "X0.00", "0.00X"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def extract_change(item: dict) -> float | None:
    for key in CHANGE_FIELD_CANDIDATES:
        if key in item:
            v = to_float(item[key])
            if v is not None:
                return v
    return None


def build_rows(raw: list[dict]) -> tuple[list[dict], bool]:
    """
    回傳 (rows, change_field_found)
    change_field_found=False 時代表API裡找不到漲跌欄位，
    這時用「(收盤-開盤)/開盤」當估計值，並標記 changePctIsEstimate=true
    """
    rows = []
    change_field_found = any(extract_change(item) is not None for item in raw[:20])

    for item in raw:
        code = item.get("Code")
        name = item.get("Name")
        close = to_float(item.get("ClosingPrice"))
        high = to_float(item.get("HighestPrice"))
        low = to_float(item.get("LowestPrice"))
        open_ = to_float(item.get("OpeningPrice"))
        vol_shares = to_float(item.get("TradeVolume"))

        if not code or close is None or high is None or low is None or vol_shares is None:
            continue

        change = extract_change(item)
        pct = None
        is_estimate = False
        if change is not None:
            prev_close = close - change
            if prev_close:
                pct = round(change / prev_close * 100, 2)
        elif open_ and open_ != 0:
            pct = round((close - open_) / open_ * 100, 2)
            is_estimate = True

        rows.append({
            "code": code,
            "name": name,
            "close": close,
            "high": high,
            "low": low,
            "open": open_,
            "volume": vol_shares / 1000,  # 股 -> 張，跟畫面上的「成交量門檻(張)」單位對齊
            "changePct": pct,
            "changePctIsEstimate": is_estimate,
        })
    return rows, change_field_found


def main():
    try:
        raw = fetch_json(URL)
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"[錯誤] 連線證交所API失敗：{e}")
        raise SystemExit(1)

    rows, change_field_found = build_rows(raw)
    if not change_field_found:
        print("[警告] API回傳資料裡找不到漲跌欄位，漲跌%已改用「(收盤-開盤)/開盤」估計，"
              "精確度較低（不是真正比昨收的漲跌幅），畫面上會標記為估計值。")

    tz = timezone(timedelta(hours=8))
    payload = {
        "updatedAt": datetime.now(tz).isoformat(),
        "source": URL,
        "rowCount": len(rows),
        "rows": rows,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)

    print(f"寫入完成：{len(rows)} 筆資料 -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
