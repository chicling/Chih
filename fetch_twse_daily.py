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

# 證交所「三大法人買賣超日報」(T86)，用來算外資買超。
# 注意：這份資料公布時間較晚（官方說法：有時下午4-5點，有時要到晚上9-10點），
# 排程時間要排晚一點才抓得到，見 .github/workflows/daily-fetch.yml 裡的cron設定。
T86_URL_TEMPLATE = "https://www.twse.com.tw/fund/T86?response=json&date={date}&selectType=ALL"

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


def fetch_foreign_net_buy(date_str: str) -> tuple[dict[str, float], str | None]:
    """
    抓「三大法人買賣超日報」(T86)，算出每支股票的外資買超(張)。
    外資買超 = 外陸資買賣超股數(不含外資自營商) + 外資自營商買賣超股數，股數除以1000轉成張。

    回傳 (code -> 外資買超張數 的字典, 實際資料日期字串或None)
    抓不到時回傳 ({}, None)，不會讓整支程式失敗（這份資料公布較晚，偶爾抓不到是正常的）。
    """
    try:
        data = fetch_json(T86_URL_TEMPLATE.format(date=date_str))
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as e:
        # ValueError 也涵蓋 json.JSONDecodeError：
        # 非交易日這個網址常常回傳空白內容，不是合法JSON，要在這裡攔住，不能讓它整支程式炸掉
        print(f"[警告] 抓外資買賣超(T86)失敗（{date_str}）：{e}")
        return {}, None

    if not isinstance(data, dict) or "fields" not in data or "data" not in data:
        print("[警告] 外資買賣超(T86) 回應格式不符預期（可能今天還沒公布），這次先沒有外資買超欄位")
        return {}, None

    fields = data["fields"]

    def idx(name: str) -> int:
        for i, f in enumerate(fields):
            if name in f:
                return i
        return -1

    code_i = idx("證券代號")
    fb1_i = idx("外陸資買賣超股數")       # 不含外資自營商，金額最大宗
    fb2_i = idx("外資自營商買賣超股數")    # 外資自營商，金額通常很小

    if code_i < 0:
        print("[警告] 外資買賣超(T86) 找不到證券代號欄位，跳過這項資料")
        return {}, None

    result: dict[str, float] = {}
    for row in data.get("data", []):
        try:
            code = str(row[code_i]).strip()
            v1 = to_float(row[fb1_i]) if fb1_i >= 0 else 0.0
            v2 = to_float(row[fb2_i]) if fb2_i >= 0 else 0.0
            result[code] = round(((v1 or 0.0) + (v2 or 0.0)) / 1000, 1)  # 股 -> 張
        except (IndexError, ValueError):
            continue

    actual_date = data.get("date")  # 證交所回應裡通常會附實際資料日期(YYYYMMDD)
    return result, actual_date


def fetch_foreign_net_buy_with_retry(tz, max_days_back: int = 7) -> tuple[dict[str, float], str | None]:
    """
    從今天開始往前找，直到抓到有資料的那一天為止（處理週末/假日手動測試的情況）。
    """
    for delta in range(max_days_back):
        d = datetime.now(tz) - timedelta(days=delta)
        date_str = d.strftime("%Y%m%d")
        result, actual_date = fetch_foreign_net_buy(date_str)
        if result:
            return result, actual_date
    return {}, None


def build_rows(raw: list[dict], foreign_map: dict[str, float]) -> tuple[list[dict], bool]:
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
            "foreignNetBuy": foreign_map.get(code),  # 張；抓不到時是 None
        })
    return rows, change_field_found


def main():
    try:
        raw = fetch_json(URL)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as e:
        print(f"[錯誤] 連線證交所API失敗：{e}")
        raise SystemExit(1)

    tz = timezone(timedelta(hours=8))
    today_str = datetime.now(tz).strftime("%Y%m%d")

    foreign_map, t86_date = fetch_foreign_net_buy_with_retry(tz)
    if not foreign_map:
        print("[提示] 往前找了好幾天還是沒抓到外資買超資料，股票資料仍會照常產生，"
              "外資買超欄位會是空值。")

    rows, change_field_found = build_rows(raw, foreign_map)
    if not change_field_found:
        print("[警告] API回傳資料裡找不到漲跌欄位，漲跌%已改用「(收盤-開盤)/開盤」估計，"
              "精確度較低（不是真正比昨收的漲跌幅），畫面上會標記為估計值。")

    trade_date_display = t86_date or today_str  # YYYYMMDD，畫面上會轉成 YYYY-MM-DD 顯示
    payload = {
        "updatedAt": datetime.now(tz).isoformat(),
        "tradeDate": trade_date_display,
        "source": URL,
        "rowCount": len(rows),
        "rows": rows,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)

    print(f"寫入完成：{len(rows)} 筆資料，外資買超抓到 {len(foreign_map)} 筆 -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
