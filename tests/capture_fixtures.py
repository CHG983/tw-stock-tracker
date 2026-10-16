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
import sys
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIX = os.path.join(HERE, "fixtures")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import update_snapshot as us  # noqa: E402

TZ_TAIPEI = timezone(timedelta(hours=8))


def months_back(n: int, today=None) -> list[tuple[int, int]]:
    """與 update_snapshot.get_index_history() 相同的月份推算，確保樣本可重播。

    n 一律取自 update_snapshot.HISTORY_MONTHS，不得在此寫死，否則樣本窗口
    與程式實際請求的窗口會默默騗開。
    """
    today = today or datetime.now(TZ_TAIPEI).date()
    out = []
    for back in range(n - 1, -1, -1):
        y, m = today.year, today.month - back
        while m <= 0:
            m += 12
            y -= 1
        out.append((y, m))
    return out


def cur_month_back(back: int) -> tuple[int, int]:
    t = datetime.now(TZ_TAIPEI).date()
    y, m = t.year, t.month - back
    while m <= 0:
        m += 12
        y -= 1
    return y, m


def _fetch_aligned(max_attempts: int = 15, wait_s: float = 3.0):
    """取得「兩端點日期對齊」的 STOCK_DAY_ALL 快照。

    TWSE 的 STOCK_DAY_ALL 與 FMTQIK 由不同後端非同步更新，盤中可能出現
     「一端已到當日、另一端尚停在前一交易日」的震盪狀態。若在該瞬間擷取樣本，
    樣本會自相矛盾（data_date 與 FMTQIK 最後一日不同），讓整套測試因擷取時機
    而非程式邏輯而失敗（2026-10-15 實測即撞上）。
    因此先重試到兩者日期一致再寫檔。
    """
    txt = ad = None
    for attempt in range(1, max_attempts + 1):
        txt = us.fetch("/rwd/zh/afterTrading/STOCK_DAY_ALL", {"response": "json"})
        import csv
        import io
        data = [r for r in list(csv.reader(io.StringIO(txt)))[1:] if r and len(r) >= 10]
        ad = us.roc_to_ad(data[0][0])
        ad2 = None
        for back in (0, 1):
            y, m = cur_month_back(back)
            j = json.loads(us.fetch("/rwd/zh/afterTrading/FMTQIK",
                                    {"date": f"{y}{m:02d}01", "response": "json"}))
            rows = j.get("data") or []
            if rows:
                ad2 = us.roc_to_ad(rows[-1][0])
                break
        if ad == ad2:
            print(f"  [sync] 第 {attempt} 次嘗試：兩端點均為 {ad}")
            break
        print(f"  [sync] 第 {attempt} 次嘗試：STOCK_DAY_ALL {ad} vs FMTQIK {ad2} → 重試",
              file=sys.stderr)
        time.sleep(wait_s)
    else:
        print(f"  [sync] 警告：{max_attempts} 次重試後仍未對齊（{ad} / {ad2}），仍繼續擷取",
              file=sys.stderr)
    return txt, ad


def main() -> int:
    os.makedirs(FIX, exist_ok=True)
    months = months_back(us.HISTORY_MONTHS)
    meta: dict = {
        "captured_at_taipei": datetime.now(TZ_TAIPEI).strftime("%Y/%m/%d %H:%M:%S"),
        "source_base": us.BASE,
        "months": [f"{y}{m:02d}" for y, m in months],
        "files": {},
    }

    def save(name: str, text: str, path: str, params: dict) -> None:
        full = os.path.join(FIX, name)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(text)
        meta["files"][name] = {
            "path": path,
            "params": params,
            "bytes": len(text.encode("utf-8")),
        }
        print(f"  saved {name:34s} {len(text.encode('utf-8')):>9,d} bytes")

    def read_fix(rel: str) -> str:
        with open(os.path.join(FIX, rel), encoding="utf-8") as f:
            return f.read()

    print("[1/5] STOCK_DAY_ALL（全市場每日收盤行情，CSV）")
    txt, ad = _fetch_aligned()
    save("stock_day_all.csv", txt,
         "/rwd/zh/afterTrading/STOCK_DAY_ALL", {"response": "json"})

    # 資料日期一律取自 STOCK_DAY_ALL 自帶的日期欄位（端點忽略 ?date=）
    import csv
    import io
    data = [r for r in list(csv.reader(io.StringIO(txt)))[1:] if r and len(r) >= 10]
    print(f"      資料日期 {ad}；資料列 {len(data):,d}")
    meta["data_date"] = ad
    meta["stock_day_all_rows"] = len(data)

    print("[2/5] MI_INDEX type=MS（成交金額、漲跌家數）")
    params = {"date": ad.replace("/", ""), "type": "MS", "response": "json"}
    save("mi_index_ms.json", us.fetch("/rwd/zh/afterTrading/MI_INDEX", params),
         "/rwd/zh/afterTrading/MI_INDEX", params)

    print("[3/5] FMTQIK（加權指數歷史日資料，{n} 個月）".format(n=us.HISTORY_MONTHS))
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

    print("[4/5] MI_5MINS_HIST（當月逐日開高低收）")
    params = {"response": "json"}
    save("mi_5mins_hist.json", us.fetch("/rwd/zh/TAIEX/MI_5MINS_HIST", params),
         "/rwd/zh/TAIEX/MI_5MINS_HIST", params)

    # BFI82U：三大法人買賣超（逐日查詢，每天一份樣本）。
    # 交易日清單取自剛存下的 FMTQIK 樣本，確保與 update_snapshot 實際查詢的日期一致。
    print(f"[5/5] BFI82U（近 {us.INST_WINDOW} 個交易日三大法人買賣超）")
    days: list[str] = []
    for m in meta["months"]:
        j = json.loads(read_fix(m))
        for r in (j.get("data") or []):
            days.append(us.roc_to_ad(r[0]))
    days = sorted(set(days))[-us.INST_WINDOW:]
    if len(days) < us.INST_WINDOW:
        print(f"      !! FMTQIK 樣本交易日不足 {us.INST_WINDOW} 日（僅 {len(days)} 日）", file=sys.stderr)
        return 1
    for d in days:
        p = {"dayDate": d.replace("/", ""), "type": "day", "response": "json"}
        t = us.fetch("/rwd/zh/fund/BFI82U", p)
        if json.loads(t).get("stat") != "OK":
            print(f"      !! BFI82U {p['dayDate']} stat 非 OK", file=sys.stderr)
            return 1
        save(os.path.join("bfi82u", f"{p['dayDate']}.json"), t,
             "/rwd/zh/fund/BFI82U", p)

    with open(os.path.join(FIX, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2, sort_keys=True)
    total = sum(v["bytes"] for v in meta["files"].values())
    print(f"\n完成：{len(meta['files'])} 個樣本檔，合計 {total:,d} bytes → {FIX}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
