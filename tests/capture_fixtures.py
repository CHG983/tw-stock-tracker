#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
capture_fixtures.py — 向臺灣證券交易所（TWSE）公開端點擷取「真實」原始回應，
存成 tests/fixtures/ 下的樣本檔，供 test_update_snapshot.py 在離線環境下重播。

為什麼要擷取真實回應而不是手寫假資料
--------------------------------------
手寫的假資料只驗證「我以為 API 長什麼樣」，真實回應的欄位順序、千分位逗號、
民國日期格式、CSV 引號規則、`tables` 陣列結構才是會讓解析器壞掉的東西。
本腳本只在「開發者手動執行」時連網；測試本身永不連網。

重新擷取（當 TWSE 格式變動時）：
    python3 tests/capture_fixtures.py

本檔不以 test_ 開頭，因此不會被 unittest discovery 收集，CI 不會執行它。
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIX = os.path.join(HERE, "fixtures")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import update_snapshot as us  # noqa: E402

TZ_TAIPEI = timezone(timedelta(hours=8))


def months_back(n: int) -> list[tuple[int, int]]:
    """與 update_snapshot.get_index_history() 相同的月份推算，確保樣本可重播。"""
    today = datetime.now(TZ_TAIPEI).date()
    out = []
    for back in range(n - 1, -1, -1):
        y, m = today.year, today.month - back
        while m <= 0:
            m += 12
            y -= 1
        out.append((y, m))
    return out


def main() -> int:
    os.makedirs(FIX, exist_ok=True)
    months = months_back(14)
    meta: dict = {
        "captured_at_taipei": datetime.now(TZ_TAIPEI).strftime("%Y/%m/%d %H:%M:%S"),
        "source_base": us.BASE,
        "months": [f"{y}{m:02d}" for y, m in months],
        "files": {},
    }

    def save(name: str, text: str, path: str, params: dict) -> None:
        full = os.path.join(FIX, name)
        with open(full, "w", encoding="utf-8") as f:
            f.write(text)
        meta["files"][name] = {
            "path": path,
            "params": params,
            "bytes": len(text.encode("utf-8")),
        }
        print(f"  saved {name:34s} {len(text.encode('utf-8')):>9,d} bytes")

    print("[1/4] STOCK_DAY_ALL（全市場每日收盤行情，CSV）")
    txt = us.fetch("/rwd/zh/afterTrading/STOCK_DAY_ALL", {"response": "json"})
    save("stock_day_all.csv", txt,
         "/rwd/zh/afterTrading/STOCK_DAY_ALL", {"response": "json"})

    # 資料日期一律取自 STOCK_DAY_ALL 自帶的日期欄位（端點忽略 ?date=）
    import csv
    import io
    data = [r for r in list(csv.reader(io.StringIO(txt)))[1:] if r and len(r) >= 10]
    ad = us.roc_to_ad(data[0][0])
    print(f"      資料日期 {ad}；資料列 {len(data):,d}")
    meta["data_date"] = ad
    meta["stock_day_all_rows"] = len(data)

    print("[2/4] MI_INDEX type=MS（成交金額、漲跌家數）")
    params = {"date": ad.replace("/", ""), "type": "MS", "response": "json"}
    save("mi_index_ms.json", us.fetch("/rwd/zh/afterTrading/MI_INDEX", params),
         "/rwd/zh/afterTrading/MI_INDEX", params)

    print("[3/4] FMTQIK（加權指數歷史日資料，14 個月）")
    os.makedirs(os.path.join(FIX, "fmtqik"), exist_ok=True)
    for y, m in months:
        p = {"date": f"{y}{m:02d}01", "response": "json"}
        t = us.fetch("/rwd/zh/afterTrading/FMTQIK", p)
        j = json.loads(t)
        if j.get("stat") != "OK":
            print(f"      !! FMTQIK {y}{m:02d} stat={j.get('stat')!r}", file=sys.stderr)
            return 1
        save(os.path.join("fmtqik", f"{y}{m:02d}.json"), t,
             "/rwd/zh/afterTrading/FMTQIK", p)

    print("[4/4] MI_5MINS_HIST（當月逐日開高低收）")
    params = {"response": "json"}
    save("mi_5mins_hist.json", us.fetch("/rwd/zh/TAIEX/MI_5MINS_HIST", params),
         "/rwd/zh/TAIEX/MI_5MINS_HIST", params)

    with open(os.path.join(FIX, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2, sort_keys=True)
    total = sum(v["bytes"] for v in meta["files"].values())
    print(f"\n完成：{len(meta['files'])} 個樣本檔，合計 {total:,d} bytes → {FIX}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
