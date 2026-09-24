#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_snapshot.py — 以臺灣證券交易所（TWSE）公開資料更新 index.html 的內建快照。

設計原則
--------
1. 只改寫 <!--SNAP:NAME--> ... <!--/SNAP:NAME--> 之間的内容；marker 之外的位元組保證不變，
   因此 HTML 結構、CSS、前端 JS 全部不受影響。
2. 所有數字都取自 TWSE 實際回應。任何一步抓取、解析或一致性驗證失敗 → 直接 raise，
   程式以非 0 結束且「不寫入任何檔案」（workflow 隨之失敗、原檔完整保留）。
   絕不以空值、0 或示意數字取代真實資料。
3. 日期一致性：STOCK_DAY_ALL 會忽略 ?date= 並回傳「最後交易日」，
   故一律以其回傳資料自帶的日期為準，再以該日期查 MI_INDEX／FMTQIK，
   避免非交易日或盤中出現兩個端點不同交易日的錯配。
4. 寫入採 tempfile + os.replace，為原子操作。

Usage:
    python3 scripts/update_snapshot.py [--file index.html] [--dry-run]
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

# --------------------------------------------------------------------------
# 常數：座標系統完全沿用 v5（viewBox 0 0 1000 420，60 點等距）
# --------------------------------------------------------------------------
BASE = "https://www.twse.com.tw"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

X0, X1 = 62.0, 984.0                    # x 範圍
N_WINDOW = 60                           # 走勢圖交易日數
HISTORY_DAYS = 260                      # 均線／交叉計算區間（交易日）
PRICE_LO_Y, PRICE_HI_Y = 240.8, 57.7    # 主圖最下／最上格線
AREA_FLOOR_Y = 250.0
VOL_BASE_Y = 380.0
VOL_BUDGET = 94.0                       # 成交量可用高度 380 → 286
LABEL_ROWS = (18.0, 38.0)               # 標籤交錯高度：先試上排，再試下排
LABEL_MIN_GAP = 70.0                    # 同排最小水平間距（標籤寬 58）
LABEL_HALF = 29.0
CROSS_COLOR = {"g": "#a35f05", "d": "#0b6b7e"}
CROSS_TEXT = {"g": "#8a4f04", "d": "#0a5f70"}
WEEK_CN = ["一", "二", "三", "四", "五", "六", "日"]
MINUS = "\u2212"                        # U+2212，頁面統一使用的負號

# 位於 <script> 內的區塊必須用 JS 區塊註解標記：HTML 註解（<!-- -->）在
# <script> 內會被瀏覽器視為「單行註解」，會把整個 JS 敘述註解掉而使頁面失效。
JS_REGIONS = {"JS_SNAPAT", "JS_STATINIT"}

TZ_TAIPEI = timezone(timedelta(hours=8))

_SSL_OK = ssl.create_default_context()
_SSL_LAX = ssl.create_default_context()
_SSL_LAX.check_hostname = False
_SSL_LAX.verify_mode = ssl.CERT_NONE


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def fetch(path: str, params: dict | None = None, *, tries: int = 3) -> str:
    q = "" if not params else "?" + "&".join(f"{k}={v}" for k, v in params.items())
    url = BASE + path + q
    last: Exception | None = None
    for attempt in range(1, tries + 1):
        for ctx in (_SSL_OK, _SSL_LAX):
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": UA,
                    "Accept": "application/json, text/csv, text/plain, */*",
                    "Accept-Language": "zh-TW,zh;q=0.9",
                })
                with urllib.request.urlopen(req, timeout=90, context=ctx) as r:
                    if r.status != 200:
                        raise urllib.error.HTTPError(url, r.status, "non-200", r.headers, None)
                    raw = r.read()
                if not raw:
                    raise RuntimeError("空回應")
                return raw.decode("utf-8-sig", errors="replace")
            except Exception as e:  # noqa: BLE001
                last = e
        if attempt < tries:
            time.sleep(1.5 * attempt)
    raise RuntimeError(f"TWSE 端點取得失敗：{url}（{type(last).__name__}: {last}）")


def fetch_json(path: str, params: dict | None = None) -> dict:
    txt = fetch(path, params)
    if txt.lstrip().startswith("<"):
        raise RuntimeError(f"TWSE 回傳非 JSON（疑似 HTML 錯誤頁）：{path}")
    try:
        return json.loads(txt)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"TWSE JSON 解析失敗：{path}（{e}）") from e


# --------------------------------------------------------------------------
# 數值 / 格式（必須與頁面內嵌 JS 的輸出格式完全一致）
# --------------------------------------------------------------------------
def n2(x) -> float | None:
    if x is None:
        return None
    s = re.sub(r"<[^>]*>", "", str(x))
    s = s.replace(",", "").replace(MINUS, "-").replace("%", "").strip()
    if s in ("", "-", "--", "nan", "NaN", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def f2(x: float | None) -> str:
    return "0.00" if x is None else f"{x:,.2f}"


def f0(x: float | None) -> str:
    return "0" if x is None else f"{int(round(x)):,d}"


def sgn(x: float | None) -> str:
    if x is None or x == 0:
        return "flat"
    return "up" if x > 0 else "down"


def pts(x: float | None) -> str:
    if x is None:
        return "0.00"
    return ("+" if x > 0 else (MINUS if x < 0 else "")) + f2(abs(x))


def pct(x: float | None) -> str:
    if x is None:
        return "0.00%"
    return ("+" if x > 0 else (MINUS if x < 0 else "")) + f"{abs(x):.2f}%"


def yn(x: float) -> str:
    return f"{x:.1f}"


def esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def roc_to_ad(s: str) -> str:
    m = re.fullmatch(r"(\d{3})[/-]?(\d{2})[/-]?(\d{2})", str(s).strip())
    if not m:
        raise RuntimeError(f"無法解析民國日期：{s!r}")
    return f"{int(m.group(1)) + 1911:04d}/{int(m.group(2)):02d}/{int(m.group(3)):02d}"


def ad_to_dt(ad: str) -> date:
    y, m, d = (int(v) for v in ad.split("/"))
    return date(y, m, d)


# --------------------------------------------------------------------------
# 抓取
# --------------------------------------------------------------------------
def get_stock_day_all() -> tuple[list[list[str]], str]:
    """全市場每日收盤行情（CSV）。回傳 (資料列, 資料日期 AD)。"""
    txt = fetch("/rwd/zh/afterTrading/STOCK_DAY_ALL", {"response": "json"})
    rows = list(csv.reader(io.StringIO(txt)))
    if len(rows) < 2:
        raise RuntimeError("STOCK_DAY_ALL 回傳空白")
    data = [r for r in rows[1:] if r and len(r) >= 10]
    if len(data) < 500:
        raise RuntimeError(f"STOCK_DAY_ALL 資料列過少（{len(data)}），疑似端點異常")
    return data, roc_to_ad(data[0][0])


def rank_universe(rows: list[list[str]]) -> list[dict]:
    """上市普通股母體。規則與頁面內嵌 JS 的 rankUniverse() 完全一致。"""
    out: list[dict] = []
    for r in rows:
        if not r or len(r) < 10:
            continue
        code = str(r[1] or "").strip()
        name = str(r[2] or "").strip()
        if not re.fullmatch(r"\d{4}", code):
            continue
        if re.fullmatch(r"0\d{3}", code):
            continue
        if re.search(r"-DR$", name):
            continue
        vol, amt, close, chg = n2(r[3]), n2(r[4]), n2(r[8]), n2(r[9])
        if vol is None or vol <= 0 or close is None or chg is None:
            continue
        prev = close - chg
        if not prev > 0:
            continue
        out.append({"code": code, "name": name, "close": close, "chg": chg, "prev": prev,
                    "pct": chg / prev * 100.0, "vol": vol, "amt": amt,
                    "open": n2(r[5]), "high": n2(r[6]), "low": n2(r[7])})
    return out


def _parse_cnt(s) -> tuple[int | None, int | None]:
    m = re.match(r"\s*([\d,]+)\s*(?:\(([\d,]+)\))?", str(s))
    if not m:
        return None, None
    return (int(m.group(1).replace(",", "")),
            int(m.group(2).replace(",", "")) if m.group(2) else None)


def get_market_stats(ad_date: str) -> dict:
    """MI_INDEX type=MS：成交金額、漲跌家數（整體市場 + 股票）。"""
    j = fetch_json("/rwd/zh/afterTrading/MI_INDEX",
                   {"date": ad_date.replace("/", ""), "type": "MS", "response": "json"})
    stat = breadth = None
    for t in (j.get("tables") or []):
        if not (t and t.get("data")):
            continue
        cols = [str(c) for c in (t.get("fields") or []) if c is not None]
        first_cells = [str(c) for c in t["data"][0] if c is not None]
        # The identifying label lives in `title` for some tables and in the
        # first column header / first cell for others, so match against all.
        sig = "|".join([str(t.get("title") or "")] + cols + first_cells)
        if stat is None and ("成交統計" in sig or "成交金額(元)" in sig):
            stat = t["data"]
        if breadth is None and ("漲跌證券數合計" in sig or "漲跌證券數" in sig
                                or "整體市場" in sig):
            breadth = t["data"]
    if not stat or not breadth:
        raise RuntimeError("MI_INDEX type=MS 缺少成交統計或漲跌家數表")

    total_amt = ordinal_amt = None
    for r in stat:
        nm = str(r[0])
        if nm.startswith("總計") or nm.startswith("證券合計"):
            total_amt = n2(r[1])
        elif nm.startswith("1.一般股票"):
            ordinal_amt = n2(r[1])
    if total_amt is None:
        raise RuntimeError("MI_INDEX 成交統計缺少「總計」列")
    if ordinal_amt is None:
        raise RuntimeError("MI_INDEX 成交統計缺少「1.一般股票」列")

    out: dict = {"total_amt": total_amt, "ordinal_amt": ordinal_amt}
    # 第 1 欄＝整體市場、第 2 欄＝股票
    for r in breadth:
        key = str(r[0])
        if len(r) < 3:
            continue
        if key.startswith("上漲"):
            out["mkt_up"], out["mkt_up_stop"] = _parse_cnt(r[1])
            out["up"], out["up_stop"] = _parse_cnt(r[2])
        elif key.startswith("下跌"):
            out["mkt_down"], out["mkt_down_stop"] = _parse_cnt(r[1])
            out["down"], out["down_stop"] = _parse_cnt(r[2])
        elif key.startswith("持平"):
            out["mkt_flat"], _ = _parse_cnt(r[1])
            out["flat"], _ = _parse_cnt(r[2])
    need = ("mkt_up", "mkt_down", "mkt_flat", "up", "down", "flat")
    missing = [k for k in need if out.get(k) is None]
    if missing:
        raise RuntimeError(f"MI_INDEX 漲跌家數表缺少欄位：{missing}")
    return out


def get_index_history() -> list[dict]:
    """FMTQIK：近 14 個月加權指數歷史日資料。"""
    today = datetime.now(TZ_TAIPEI).date()
    acc: dict[str, dict] = {}
    for back in range(13, -1, -1):
        y, m = today.year, today.month - back
        while m <= 0:
            m += 12
            y -= 1
        j = fetch_json("/rwd/zh/afterTrading/FMTQIK",
                       {"date": f"{y}{m:02d}01", "response": "json"})
        if j.get("stat") != "OK":
            raise RuntimeError(f"FMTQIK {y}-{m:02d} stat={j.get('stat')!r}")
        for r in (j.get("data") or []):
            close = n2(r[4])
            if close is None:
                raise RuntimeError(f"FMTQIK {r[0]} 收盤指數無法解析：{r[4]!r}")
            # FMTQIK「成交金額」單位為元；走勢圖副圖以億元為單位，必須在此換算，
            # 否則成交量軸會多出 8 個數量級。
            amt_yuan = n2(r[2])
            if amt_yuan is None:
                raise RuntimeError(f"FMTQIK {r[0]} 成交金額無法解析：{r[2]!r}")
            ad = roc_to_ad(r[0])
            acc[ad] = {"date": ad, "close": close, "amt": amt_yuan / 1e8, "chg": n2(r[5])}
    rows = sorted(acc.values(), key=lambda x: ad_to_dt(x["date"]))
    # MA20 needs 19 prior days before the HISTORY_DAYS window can be fully valued.
    if len(rows) < HISTORY_DAYS + 19:
        raise RuntimeError(f"FMTQIK 歷史資料不足（僅 {len(rows)} 筆，"
                           f"至少需要 {HISTORY_DAYS + 19} 筆）")
    return rows


def get_today_ohlc(ad_date: str) -> dict:
    """MI_5MINS_HIST：當月逐日開高低收，取指定日期（用於交叉驗證）。"""
    j = fetch_json("/rwd/zh/TAIEX/MI_5MINS_HIST", {"response": "json"})
    for r in (j.get("data") or []):
        if roc_to_ad(r[0]) == ad_date:
            o, h, l, c = n2(r[1]), n2(r[2]), n2(r[3]), n2(r[4])
            if None in (o, h, l, c):
                raise RuntimeError(f"MI_5MINS_HIST {ad_date} OHLC 無法解析：{r}")
            return {"open": o, "high": h, "low": l, "close": c}
    raise RuntimeError(f"MI_5MINS_HIST 找不到 {ad_date} 的資料")


# --------------------------------------------------------------------------
# 計算
# --------------------------------------------------------------------------
def sma(vals: list[float], n: int) -> list[float | None]:
    out: list[float | None] = []
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def find_crossovers(rows: list[dict], ma5: list, ma20: list, scan_from: int) -> list[dict]:
    out: list[dict] = []
    diff = [None if (a is None or b is None) else a - b for a, b in zip(ma5, ma20)]
    for i in range(1, len(rows)):
        d0, d1 = diff[i - 1], diff[i]
        if d0 is None or d1 is None or i < scan_from:
            continue
        if d0 < 0 and d1 >= 0:
            kind = "g"
        elif d0 > 0 and d1 <= 0:
            kind = "d"
        else:
            continue
        out.append({"date": rows[i]["date"], "kind": kind, "close": rows[i]["close"],
                    "ma5": ma5[i], "ma20": ma20[i]})
    return out


def axis_layout(values: list[float]) -> dict:
    """
    價格軸：沿用 v5 的「等距多格線」語法（原為 step 2000、5 格、底 240.8、頂 57.7），
    但依實際資料位移／擴張，確保任何點都不超出圖面。格子數一律為偶數，
    使圖面中心線落在一條格線上。
    """
    lo_v, hi_v = min(values), max(values)
    span, mid = hi_v - lo_v, (hi_v + lo_v) / 2.0
    chosen = None
    for k, step in ((4, 2000.0), (6, 2000.0), (8, 2000.0),
                    (4, 5000.0), (6, 5000.0), (8, 5000.0),
                    (10, 5000.0), (6, 10000.0), (8, 10000.0), (10, 10000.0)):
        if k * step >= span * 1.04:
            chosen = (k, step)
            break
    if chosen is None:
        k, step = 10, 20000.0
    else:
        k, step = chosen
    centre = round(mid / step) * step
    lo = centre - (k / 2.0) * step
    hi = centre + (k / 2.0) * step
    guard = 0
    while (lo > lo_v or hi < hi_v) and guard < 50:
        if lo > lo_v:
            lo -= step
            hi -= step
        else:
            lo += step
            hi += step
        guard += 1
    if lo > lo_v or hi < hi_v:
        raise RuntimeError(f"價格軸無法覆蓋資料區間（{lo_v}–{hi_v}）")
    return {"lo": lo, "hi": hi, "step": step,
            "ticks": [lo + i * step for i in range(k + 1)]}


def price_y(p: float, ax: dict) -> float:
    return PRICE_LO_Y - (p - ax["lo"]) / (ax["hi"] - ax["lo"]) * (PRICE_LO_Y - PRICE_HI_Y)


def vol_layout(max_amt: float) -> dict:
    vmax = None
    for cand in (5000.0, 10000.0, 15000.0, 20000.0, 25000.0, 30000.0,
                 40000.0, 50000.0, 75000.0, 100000.0):
        if cand >= max_amt * 1.02:
            vmax = cand
            break
    if vmax is None:
        vmax = float(int(max_amt * 1.02 // 5000 + 1) * 5000)
    scale = VOL_BUDGET / vmax
    return {"vmax": vmax, "scale": scale,
            "ticks": [vmax / 3.0, vmax / 3.0 * 2, vmax],
            "y_of": lambda a, s=scale: VOL_BASE_Y - a * s}


def x_of(i: int) -> float:
    return X0 + i * (X1 - X0) / (N_WINDOW - 1)


def assign_label_rows(crosses_in_win: list[dict]) -> dict[int, float]:
    """
    貪婪交錯：每個交叉先試上排（y=18），與同排既有標籤水平距離不足時改試下排（y=38）。
    輸入已按 wi 升序排序，確保結果穩定可重現。
    """
    used: dict[float, list[float]] = {r: [] for r in LABEL_ROWS}
    out: dict[int, float] = {}
    for c in crosses_in_win:
        x = x_of(c["wi"])
        for row in LABEL_ROWS:
            if all(abs(x - px) >= LABEL_MIN_GAP for px in used[row]):
                used[row].append(x)
                out[c["wi"]] = row
                break
        else:  # 兩排都太近 → 選「最空」的一排
            row = max(LABEL_ROWS,
                      key=lambda r: min((abs(x - px) for px in used[r]), default=1e9))
            used[row].append(x)
            out[c["wi"]] = row
    return out


# --------------------------------------------------------------------------
# SVG
# --------------------------------------------------------------------------
def svg_region(win, win_ma5, win_ma20, win_prev, crosses, ax, vl, rows) -> str:
    import html as _html

    xs = [x_of(i) for i in range(N_WINDOW)]
    ys_c = [price_y(r["close"], ax) for r in win]
    ys_5 = [price_y(v, ax) for v in win_ma5]
    ys_20 = [price_y(v, ax) for v in win_ma20]
    L: list[str] = []
    A = L.append

    # --- <svg> wrapper -----------------------------------------------------
    # NOTE: the wrapper must be emitted HERE. The injector marks the whole
    # <svg>...</svg> block, so this function's output replaces it entirely —
    # omitting the wrapper would leave every chart child as a bare, unknown
    # HTML element with nothing painted.
    lo = min(r["close"] for r in win)
    hi = max(r["close"] for r in win)
    up_days = sum(1 for i, r in enumerate(win)
                  if win_prev[i] is not None and r["close"] > win_prev[i])
    dn_days = sum(1 for i, r in enumerate(win)
                  if win_prev[i] is not None and r["close"] < win_prev[i])
    amax = max((r["amt"] for r in win if r.get("amt") is not None), default=0.0)
    xt = [(c["date"], "黃金" if c["kind"] == "g" else "死亡") for c in crosses]
    parts = [
        f"針對加權指數近 {N_WINDOW} 個交易日（{win[0]['date']} 至 {win[-1]['date']}）"
        f"的收盤走勢、MA5／MA20 均線與成交量圖。最新收盤 {f2(win[-1]['close'])} 點，"
        f"MA5 {f2(win_ma5[-1])} 點、MA20 {f2(win_ma20[-1])} 點。"
        f"期間最低 {f2(lo)} 點、最高 {f2(hi)} 點。"
        f"下方柱狀圖為每日成交金額，最高 {f0(amax)} 億元，"
        f"紅柱代表收盤較前一日上漲、綠柱代表下跌，{up_days} 天上漲、{dn_days} 天下跌。"
    ]
    if xt:
        parts.append("圖上另以垂直虛線與圓點標示區間內 " + str(len(xt)) + " 次均線交叉："
                     + "、".join(f"{d} {k}交叉" for d, k in xt)
                     + "，日期標籤採交錯高度以避免重疊。")
    else:
        parts.append("此區間內 MA5 與 MA20 未發生交叉。")
    aria = _html.escape("".join(parts), quote=True)

    A(f'<svg viewBox="0 0 1000 420" role="img" aria-label="{aria}">')
    A(f'        <title>加權指數近 {N_WINDOW} 個交易日收盤走勢、MA5／MA20 均線、'
      f'均線交叉標示與成交量（成交金額）</title>')

    A("        <!-- ===== 主圖：水平格線與 Y 軸標籤（指數點） ===== -->")
    for t in reversed(ax["ticks"]):
        y = yn(price_y(t, ax))
        A(f'        <line class="grid-l" x1="62" y1="{y}" x2="984" y2="{y}"/>')
    for t in reversed(ax["ticks"]):
        A(f'        <text class="axis-t" x="54" y="{yn(price_y(t, ax) + 4.0)}" '
          f'text-anchor="end">{f0(t)}</text>')

    A("")
    A(f'        <!-- ===== 均線交叉垂直導引線（區間內 {len(crosses)} 次） ===== -->')
    A('        <g class="cx-guides" aria-hidden="true">')
    for c in crosses:
        y1 = 34.0 if rows[c["wi"]] == LABEL_ROWS[0] else 54.0
        kind = "golden" if c["kind"] == "g" else "death"
        A(f'          <line class="cx-guide is-{kind}" x1="{yn(xs[c["wi"]])}" '
          f'y1="{yn(y1)}" x2="{yn(xs[c["wi"]])}" y2="238"/>')
    A("        </g>")

    A("")
    A('        <!-- ===== 收盤指數：面積與折線 ===== -->')
    A(f'        <path class="area-f" d="M {yn(xs[0])},{yn(AREA_FLOOR_Y)} L '
      + " ".join(f"{yn(x)},{yn(y)}" for x, y in zip(xs, ys_c))
      + f' L {yn(xs[-1])},{yn(AREA_FLOOR_Y)} Z"/>')
    A('        <polyline class="line-p" points="'
      + " ".join(f"{yn(x)},{yn(y)}" for x, y in zip(xs, ys_c)) + '"/>')

    A("")
    A("        <!-- ===== MA20（長均線，底層） ===== -->")
    A('        <polyline class="line-ma20" points="'
      + " ".join(f"{yn(x)},{yn(y)}" for x, y in zip(xs, ys_20)) + '"/>')
    A(f'        <circle class="dot-ma20" cx="{yn(xs[-1])}" cy="{yn(ys_20[-1])}" r="3.6"/>')

    A("")
    A("        <!-- ===== MA5（短均線，上層） ===== -->")
    A('        <polyline class="line-ma5" points="'
      + " ".join(f"{yn(x)},{yn(y)}" for x, y in zip(xs, ys_5)) + '"/>')
    A(f'        <circle class="dot-ma5" cx="{yn(xs[-1])}" cy="{yn(ys_5[-1])}" r="3.6"/>')

    A("")
    A("        <!-- ===== 均線交叉點標記與日期標籤（交錯高度避免重疊） ===== -->")
    A('        <g class="cx-marks">')
    for c in crosses:
        t = "黃金交叉" if c["kind"] == "g" else "死亡交叉"
        A('          <g class="cx-mark">')
        A(f'            <title>{c["date"]} {t}，當日收盤 {f2(c["close"])} 點，'
          f'MA5 {f2(c["ma5"])} 點、MA20 {f2(c["ma20"])} 點</title>')
        A(f'            <circle class="cx-dot" cx="{yn(xs[c["wi"]])}" cy="{yn(ys_c[c["wi"]])}" '
          f'r="5.4" fill="{CROSS_COLOR[c["kind"]]}"/>')
        A("          </g>")
    A("        </g>")
    A('        <g class="cx-labels">')
    for c in crosses:
        row = rows[c["wi"]]
        A(f'          <g class="cx-lb is-{"golden" if c["kind"] == "g" else "death"}">')
        A(f'            <rect class="cx-pill" x="{yn(xs[c["wi"]] - LABEL_HALF)}" y="{yn(row)}" '
          f'width="58" height="16" rx="4" style="stroke:{CROSS_COLOR[c["kind"]]}"/>')
        A(f'            <text class="cx-pill-t" x="{yn(xs[c["wi"]])}" y="{yn(row + 11.4)}" '
          f'text-anchor="middle" style="fill:{CROSS_TEXT[c["kind"]]}">{c["date"]}</text>')
        A("          </g>")
    A("        </g>")

    A("")
    A("        <!-- ===== 起點與終點標註 ===== -->")
    A(f'        <circle cx="{yn(xs[0])}" cy="{yn(ys_c[0])}" r="3.4" fill="#2b6094"/>')
    A(f'        <circle class="last-d" cx="{yn(xs[-1])}" cy="{yn(ys_c[-1])}" r="4.6"/>')
    # 終點標註：放在折線終點「下方」的空白處。
    # 原本放在 top-43.2（圖頂），會與下方的交叉日期標籤在水平方向重疊（實測
    # 2026/09/21 的標籤與本數值框 overlap 38x9 px）。移到終點下方可完全避開，
    # 且仍緊鄰折線終點、易於對照。
    badge_y = min(240.0, ys_c[-1] + 21.0)
    A(f'        <text class="axis-t" x="978.0" y="{yn(badge_y)}" '
      f'text-anchor="end" style="font-weight:700;fill:#14304f">{f2(win[-1]["close"])}</text>')

    A("")
    A("        <!-- ===== 副圖：成交量（成交金額，億元） ===== -->")
    A('        <text class="axis-t sub-chart-t" x="62" y="273.0">成交量（成交金額，億元）</text>')
    A(f'        <line class="vol-base" x1="62" y1="{yn(VOL_BASE_Y)}" x2="984" y2="{yn(VOL_BASE_Y)}"/>')
    for t in reversed(vl["ticks"]):
        A(f'        <line class="grid-l" x1="62" y1="{yn(vl["y_of"](t))}" x2="984" y2="{yn(vl["y_of"](t))}"/>')
    for t in reversed(vl["ticks"]):
        A(f'        <text class="axis-t" x="54" y="{yn(vl["y_of"](t) + 4.0)}" '
          f'text-anchor="end">{f0(t)}</text>')
    for i, r in enumerate(win):
        if r.get("amt") is None:
            raise RuntimeError(f'{r["date"]} 缺少成交金額，無法繪製成交量柱')
        y = vl["y_of"](r["amt"])
        h = VOL_BASE_Y - y
        if h < 0:
            raise RuntimeError(f'{r["date"]} 成交金額 {r["amt"]} 超出成交量軸上限 {vl["vmax"]}')
        pc = win_prev[i]
        cls = "v-up" if (pc is not None and r["close"] > pc) else "v-down"
        A(f'        <rect class="{cls}" x="{yn(xs[i] - 4.7)}" y="{yn(y)}" width="9.4" '
          f'height="{yn(h)}"><title>{r["date"].replace("/", "-")} 成交金額 '
          f'{f2(r["amt"])} 億元</title></rect>')

    A("")
    A("        <!-- ===== X 軸標籤（主副圖共用） ===== -->")
    for idx in (0, 12, 24, 36, 48, N_WINDOW - 1):
        anchor = "start" if idx == 0 else ("end" if idx == N_WINDOW - 1 else "middle")
        A(f'        <text class="axis-t" x="{yn(xs[idx])}" y="404.0" '
          f'text-anchor="{anchor}">{win[idx]["date"][5:]}</text>')

    A("      </svg>")
    return "\n".join(L)


# --------------------------------------------------------------------------
# 其它區塊
# --------------------------------------------------------------------------
def rl_row(rank: int, r: dict) -> str:
    k = sgn(r["chg"])
    bar = min(100.0, abs(r["pct"]) / 10 * 100)
    return (f'      <div class="rl-row" data-code="{esc(r["code"])}" data-pct="{r["pct"]:.2f}">'
            f'<span class="rl-no num">{rank}</span>'
            f'<span class="rl-id"><span class="num">{esc(r["code"])}</span>'
            f'<b>{esc(r["name"])}</b></span>'
            f'<span class="rl-px num {k}">{f2(r["close"])}</span>'
            f'<span class="rl-dl num {k}">{pts(r["chg"])}</span>'
            f'<span class="rl-pc"><span class="chip {k} num">{pct(r["pct"])}</span></span>'
            f'<span class="rl-bar" aria-hidden="true"><i class="{k}" '
            f'style="width:{bar:.1f}%"></i></span></div>')


def tsmc_block(r: dict) -> str:
    k = sgn(r["chg"])
    bar = min(50.0, abs(r["pct"]) * 5)
    amt_yi = (r["amt"] or 0) / 1e8
    return (
        '      <div class="wl-head" aria-hidden="true">\n'
        '          <span>代號</span><span>名稱 / 產業</span>\n'
        '          <span class="r">收盤價</span><span class="r">漲跌</span>'
        '<span class="r">漲跌幅</span>\n'
        '          <span class="r">成交量</span><span class="r amt">成交金額</span>\n'
        '        </div>\n'
        f'      <div class="stock" data-code="{esc(r["code"])}"><div class="stock-main">\n'
        f'        <span class="s-code num">{esc(r["code"])}</span>\n'
        f'        <span class="s-name"><b>{esc(r["name"])}</b><em>半導體</em></span>\n'
        f'        <span class="s-price num {k}">{f2(r["close"])}</span>\n'
        f'        <span class="s-delta num {k}">{pts(r["chg"])}</span>\n'
        f'        <span class="s-pct"><span class="chip {k} num">{pct(r["pct"])}</span></span>\n'
        f'        <span class="s-vol num">{f0(r["vol"] / 1000)}<small>張</small></span>\n'
        f'        <span class="s-amt num">{amt_yi:.2f}<small>億</small></span>\n'
        f'      </div><div class="stock-sub">'
        f'<span>開 <b>{f2(r["open"])}</b></span><span>高 <b>{f2(r["high"])}</b></span>'
        f'<span>低 <b>{f2(r["low"])}</b></span><span>昨收 <b>{f2(r["prev"])}</b></span>'
        f'<span>成交金額 <b>{amt_yi:.2f} 億</b></span></div>\n'
        f'      <div class="rbar" aria-hidden="true"><i class="{k}" '
        f'style="width:{bar:.2f}%"></i></div></div>')


def rank_bar(records: list[dict]) -> str:
    out = []
    for r in records:
        k = sgn(r["chg"])
        w = min(50.0, abs(r["pct"]) * 5)
        out.append(
            f'      <div class="rk" data-code="{esc(r["code"])}" data-pct="{r["pct"]:.2f}">'
            f'<span class="rk-id"><span>{esc(r["code"])}</span>{esc(r["name"])}</span>\n'
            f'        <span class="rk-track"><i class="{k}" style="width:{w:.2f}%"></i></span>\n'
            f'        <span class="rk-v num {k}">{pct(r["pct"])}</span></div>')
    return "\n".join(out)


# --------------------------------------------------------------------------
# 組裝 marker 內容
# --------------------------------------------------------------------------
def build_regions(cfg: dict) -> dict[str, str]:
    rows, ad = cfg["rows"], cfg["date"]
    dt = ad_to_dt(ad)
    hist = cfg["hist"]
    win, win_ma5, win_ma20 = cfg["win"], cfg["win_ma5"], cfg["win_ma20"]
    win_prev, prev_close, prev_ad = cfg["win_prev"], cfg["prev_close"], cfg["prev_ad"]
    close, ohlc, mkt = cfg["today_close"], cfg["ohlc"], cfg["mkt"]
    c_all, c_win = cfg["crosses_all"], cfg["crosses_in_win"]
    gain, lose, tsmc, uni_n = cfg["gain"], cfg["lose"], cfg["tsmc"], cfg["uni_n"]
    ax, vl, lrows = cfg["ax"], cfg["vl"], cfg["label_rows"]
    last5, last20 = win_ma5[-1], win_ma20[-1]
    hist_dates = [r["date"] for r in hist]
    h_start, h_end = hist_dates[0], hist_dates[-1]

    chg = close - prev_close
    pchg = chg / prev_close * 100
    k = sgn(chg)
    R: dict[str, str] = {}

    R["TITLE"] = f"<title>台股追蹤・加權指數與當日漲跌幅排行（{ad}）</title>"
    R["HEROTAG"] = ('<span class="tag" id="hero-tag">集中市場・收盤</span>')

    R["IDXROW"] = (
        '    <div class="idx-row">\n'
        f'      <div class="idx-val num is-{k}" id="idx-val">{f2(close)}</div>\n'
        '      <div class="idx-delta">\n'
        f'        <span class="d1 num is-{k}" id="idx-delta">{pts(chg)}\u3000{pct(pchg)}</span>\n'
        f'        <span class="d2" id="idx-prev">較前一交易日（{prev_ad[5:]} 收 '
        f'{f2(prev_close)}）</span>\n'
        '      </div>\n'
        '    </div>\n'
        f'    <p class="hero-note" id="idx-note">當日振幅 '
        f'{f2(ohlc["high"] - ohlc["low"])} 點\u3000|\u3000開盤 {f2(ohlc["open"])}'
        f'\u3000最高 {f2(ohlc["high"])}\u3000最低 {f2(ohlc["low"])}</p>')

    up, up_stop = mkt["up"], mkt["up_stop"]
    dn, dn_stop = mkt["down"], mkt["down_stop"]
    fl = mkt["flat"]
    tot_n = up + dn + fl
    wu, wd, wf = up / tot_n * 100, dn / tot_n * 100, fl / tot_n * 100

    R["KPIS"] = (
        '    <dl class="kpis">\n'
        '      <div class="kpi">\n'
        '        <dt>成交金額（集中市場總計）</dt>\n'
        f'        <dd class="num" id="kpi-amt">{mkt["total_amt"] / 1e8:,.2f}<small>億元</small></dd>\n'
        '      </div>\n'
        '      <div class="kpi">\n'
        '        <dt>其中一般股票</dt>\n'
        f'        <dd class="num" id="kpi-stock">{mkt["ordinal_amt"] / 1e8:,.2f}<small>億元</small></dd>\n'
        '      </div>\n'
        '      <div class="kpi">\n'
        '        <dt>上漲家數（上市股票）</dt>\n'
        f'        <dd class="num is-up" id="kpi-up">{f0(up)}<small>家</small></dd>\n'
        '      </div>\n'
        '      <div class="kpi">\n'
        '        <dt>下跌家數（上市股票）</dt>\n'
        f'        <dd class="num is-down" id="kpi-down">{f0(dn)}<small>家</small></dd>\n'
        '      </div>\n'
        '    </dl>')

    R["BRDATE"] = f'<span class="sub" id="br-date">上市股票・{ad}</span>'
    R["BREADTH"] = (
        '    <div class="breadth-top">\n'
        '      <div class="bstat">\n'
        '        <span class="lb">上漲</span>\n'
        f'        <span class="vv num up" id="br-up">{f0(up)}</span>\n'
        f'        <span class="xs" id="br-up-x">含漲停 {f0(up_stop or 0)} 家</span>\n'
        '      </div>\n'
        '      <div class="bstat">\n'
        '        <span class="lb">下跌</span>\n'
        f'        <span class="vv num down" id="br-down">{f0(dn)}</span>\n'
        f'        <span class="xs" id="br-down-x">含跌停 {f0(dn_stop or 0)} 家</span>\n'
        '      </div>\n'
        '      <div class="bstat" style="margin-left:auto">\n'
        '        <span class="lb">持平</span>\n'
        f'        <span class="vv num" style="color:var(--flat)" id="br-flat">{f0(fl)}</span>\n'
        f'        <span class="xs" id="br-flat-x">合計 {f0(tot_n)} 家</span>\n'
        '      </div>\n'
        '    </div>\n'
        '\n'
        f'    <div class="bbar" id="bbar" role="img" aria-label="上市股票漲跌家數比例：'
        f'上漲 {f0(up)} 家（{wu:.1f}%）、下跌 {f0(dn)} 家（{wd:.1f}%）、'
        f'持平 {f0(fl)} 家（{wf:.1f}%）">\n'
        f'      <div class="b-up" id="bbar-up" style="width:{wu:.1f}%">{wu:.1f}%</div>\n'
        f'      <div class="b-down" id="bbar-down" style="width:{wd:.1f}%">{wd:.1f}%</div>\n'
        f'      <div class="b-flat" id="bbar-flat" style="width:{wf:.1f}%">{wf:.1f}%</div>\n'
        '    </div>\n'
        '    <div class="bscale"><span>0%</span><span>50%</span><span>100%</span></div>\n'
        '\n'
        '    <p class="bfoot" id="br-market">\n'
        '      <b>整體市場</b>（含 ETF、權證、ETN、受益證券等全部上市有價證券）：'
        f'上漲 <b class="num is-up">{f0(mkt["mkt_up"])}</b> 家'
        f'（漲停 {f0(mkt["mkt_up_stop"] or 0)}）、'
        f'下跌 <b class="num is-down">{f0(mkt["mkt_down"])}</b> 家'
        f'（跌停 {f0(mkt["mkt_down_stop"] or 0)}）、'
        f'持平 <b class="num">{f0(mkt["mkt_flat"])}</b> 家。\n'
        '    </p>')

    n_up_days = sum(1 for i in range(N_WINDOW)
                    if win_prev[i] is not None and win[i]["close"] > win_prev[i])
    n_dn_days = N_WINDOW - n_up_days
    R["CHARTSUB"] = (f'<span class="sub">近 {N_WINDOW} 個交易日收盤'
                     f'（{win[0]["date"]} – {win[-1]["date"]}）・'
                     f'區間內 {len(c_win)} 次均線交叉</span>')
    R["SVG"] = svg_region(win, win_ma5, win_ma20, win_prev, c_win, ax, vl, lrows)

    order = ("收盤 &gt; MA5 &gt; MA20（多頭排列）" if close > last5 > last20 else
             "收盤 &lt; MA5 &lt; MA20（空頭排列）" if close < last5 < last20 else
             "均線糾結（多空未明）")
    recent = c_all[-1] if c_all else None
    if recent:
        r_txt = f'{recent["date"]} {"黃金交叉" if recent["kind"] == "g" else "死亡交叉"}'
        r_col = "#a35f05" if recent["kind"] == "g" else "#0b6b7e"
    else:
        r_txt, r_col = "無（區間內未出現交叉）", "#65717e"
    R["MASTRIP"] = (
        '    <div class="ma-strip">\n'
        f'      <span><i class="sw" style="background:#2b6094"></i>最新收盤 '
        f'<b class="num">{f2(close)}</b></span>\n'
        f'      <span><i class="sw" style="background:#c2760b"></i>MA5 '
        f'<b class="num">{f2(last5)}</b></span>\n'
        f'      <span><i class="sw" style="background:#0f7f8b"></i>MA20 '
        f'<b class="num">{f2(last20)}</b></span>\n'
        f'      <span class="pos">{order}</span>\n'
        f'      <span class="cx-recent"><i class="sw" style="background:{r_col}"></i>'
        f'最近一次交叉 <b>{r_txt}</b>'
        f'（{win[0]["date"]}–{win[-1]["date"]} 區間內共 {len(c_win)} 次）</span>\n'
        '    </div>')

    if c_win:
        items = []
        for c in c_win:
            cls = "g" if c["kind"] == "g" else "d"
            lbl = "黃金" if c["kind"] == "g" else "死亡"
            items.append(f'<span class="k {cls}"><i aria-hidden="true"></i>'
                         f'{c["date"][5:]} {lbl}</span>')
        R["CXMKEY"] = (f'    <p class="cx-mkey" aria-label="圖上標示的 {len(c_win)} 次均線'
                       f'交叉日期，由左至右">圖上 {len(c_win)} 次交叉（由左至右）：\n      '
                       + "\n      ".join(items) + '\n    </p>')
    else:
        R["CXMKEY"] = ('    <p class="cx-mkey" aria-label="走勢圖區間內沒有均線交叉">'
                       f'走勢圖區間（{win[0]["date"]}–{win[-1]["date"]}）內沒有 '
                       f'MA5／MA20 交叉。</p>')

    in_win = {c["date"] for c in c_win}
    body = []
    for c in sorted(c_all, key=lambda x: ad_to_dt(x["date"]), reverse=True):
        cls = "g" if c["kind"] == "g" else "d"
        lbl = "黃金交叉" if c["kind"] == "g" else "死亡交叉"
        win_cls = " win" if c["date"] in in_win else ""
        flag = ('<span class="cx-flag on">已標示</span>' if c["date"] in in_win
                else '<span class="cx-flag">區間外</span>')
        body.append(
            f'      <div class="cx-row{win_cls}">\n'
            f'        <span class="cx-dt">{c["date"]}</span>\n'
            f'        <span class="cx-type {cls}"><span class="dot" aria-hidden="true">'
            f'</span>{lbl}</span>\n'
            f'        <span class="cx-close">{f2(c["close"])}</span>\n'
            f'        <span class="cx-ma">MA5 {f2(c["ma5"])} · MA20 {f2(c["ma20"])}</span>\n'
            f'        {flag}\n'
            '      </div>')
    rows_html = (['      <div class="cx-row hd">', '        <span>交叉日期</span>',
                  '        <span>類型</span>', '        <span>當日收盤</span>',
                  '        <span>均線數值（交叉當日）</span>', '        <span>走勢圖</span>',
                  '      </div>'] + body)
    R["CXLIST"] = ('    <div class="cx-list" role="group" aria-label="MA5 與 MA20 交叉事件清單，'
                   f'共 {len(c_all)} 筆，其中 {len(c_win)} 筆落在走勢圖區間內">\n'
                   + "\n".join(rows_html) + '\n    </div>')

    n_gold = sum(1 for c in c_all if c["kind"] == "g")
    R["CXNOTE"] = (
        '      <div class="cx-note">\n'
        '      <p><b>交叉如何判定：</b>以加權指數<b>每日收盤值</b>計算 MA5（5 日簡單移動平均）'
        '與 MA20（20 日簡單移動平均），逐日比較兩者差值。差值<b>由負轉正</b>'
        '（MA5 由下往上穿越 MA20）記為<b class="tg">黃金交叉</b>——短期均線轉強、'
        '上漲動能增強，市場多解讀為偏多訊號；差值<b>由正轉負</b>'
        '（MA5 由上往下穿越 MA20）記為<b class="td">死亡交叉</b>——短期均線轉弱、'
        '下跌動能增強，市場多解讀為偏空訊號。</p>\n'
        '      <p><b>重要提醒：</b>MA5／MA20 皆為<b>落後指標</b>，訊號必然出現在趨勢形成之後，'
        '且短均線與長均線在盤整區間容易反覆交叉，產生假訊號。均線交叉僅供參考，'
        '不宜單獨作為買賣依據。</p>\n'
        f'      <p><b>資料來源與計算區間：</b>臺灣證券交易所 <code>exchangeReport/FMTQIK</code> '
        f'加權指數歷史日資料 <b>{len(hist_dates)} 個交易日</b>（{h_start} – {h_end}）。'
        f'MA5／MA20 以全部 {len(hist_dates)} 日計算後，於走勢圖呈現 '
        f'{win[0]["date"]} – {win[-1]["date"]} 區間。上表列出該 {len(hist_dates)} 日內的'
        f'<b>全部 {len(c_all)} 筆</b>交叉事件（黃金交叉 {n_gold} 筆、'
        f'死亡交叉 {len(c_all) - n_gold} 筆），其中 <b>{len(c_win)} 筆</b>'
        f'落在走勢圖區間內並於圖上標示（標籤採交錯高度避免重疊）；其餘 '
        f'{len(c_all) - len(c_win)} 筆以「區間外」標註，未畫在圖上。</p>\n'
        '      <p><b>自動更新：</b>本區塊由 <code>scripts/update_snapshot.py</code> 於 '
        'GitHub Actions 排程執行時，以證交所實際資料重新計算並寫回，非人工輸入。</p>\n'
        '      <p><b>關於走勢圖：</b>圖上的日期標籤為 SVG 內部座標，會隨圖表等比縮放；'
        '視窗寬度小於 <b>1180px</b> 時字級會小於約 9.4px 而難以閱讀，因此於該寬度以下'
        '<b>隱藏圖上標籤</b>，改以圖表下方的交叉摘要（彩色點＋日期＋類型）與上方清單閱讀'
        '交叉日期與數值。視窗寬度足夠時則直接於圖上顯示日期標籤。</p>\n'
        '    </div>')

    R["RANKNOTE"] = (
        '    <p class="note-src">排名依據：臺灣證券交易所「每日收盤行情」之個股<b>收盤價</b>與<b>漲跌價差</b>，'
        '以<b>漲跌幅百分比</b>由高至低（漲幅）／由低至高（跌幅）排序。母體為'
        f'<b>上市普通股</b>，排除 ETF、ETN、受益證券與 TDR，並排除'
        f'<b>無成交、無收盤價或無漲跌價差</b>之個股，共 '
        f'<b id="uni-n">{f0(uni_n)}</b> 檔。資料日期 <b id="uni-d">{ad}</b>。'
        '本區塊由 GitHub Actions 每日自動以證交所資料重新抓取與排序。</p>')
    R["LISTGAIN"] = ('\n      <div class="rl-list" id="list-gain" aria-label="當日漲幅前十名">'
                      + "\n" + "\n".join(rl_row(i + 1, r) for i, r in enumerate(gain))
                      + "\n      </div>")
    R["LISTLOSE"] = ('\n      <div class="rl-list" id="list-lose" aria-label="當日跌幅前十名">'
                      + "\n" + "\n".join(rl_row(i + 1, r) for i, r in enumerate(lose))
                      + "\n      </div>")
    R["LISTTSMC"] = ('\n      <div class="wl" id="list-tsmc">'
                     + "\n" + tsmc_block(tsmc) + "\n\n      </div>")

    seen, uq = set(), []
    for r in gain + lose + [tsmc]:
        if r["code"] not in seen:
            seen.add(r["code"])
            uq.append(r)
    uq.sort(key=lambda x: (-x["pct"], x["code"]))
    R["RANKBAR"] = '\n      <div class="rank">' + "\n" + rank_bar(uq) + "\n    </div>"

    R["FOOTA"] = (
        '        <h3>資料時間</h3>\n'
        f'        <p>{ad}（星期{WEEK_CN[dt.weekday()]}）13:30 收盤</p>\n'
        f'        <p>資料擷取時間：{cfg["fetched"]}（UTC+8）</p>\n'
        f'        <p>前一交易日：{prev_ad}</p>\n'
        f'        <p>走勢圖區間：{win[0]["date"]} – {win[-1]["date"]}（{N_WINDOW} 個交易日）</p>\n'
        f'        <p>MA 交叉計算區間：{h_start} – {h_end}（{len(hist_dates)} 個交易日）</p>\n'
        '      </div>')

    R["FOOTB"] = (
        '        <h3>資料處理說明</h3>\n'
        '        <p><b>漲跌幅排行</b>取 <b>STOCK_DAY_ALL</b>（每日收盤行情），以個股'
        '<b>收盤價</b>與<b>漲跌價差</b>換算漲跌幅百分比後排序。母體為<b>上市普通股</b>，'
        '排除 ETF／ETN／受益證券與 TDR，並排除無成交、無收盤價、無漲跌價差者，共 '
        f'<b>{f0(uni_n)}</b> 檔；排序為漲幅由高至低、跌幅由低至高，'
        '同名次以股票代號升冪為次要鍵。</p>\n'
        '        <p>成交量為「成交股數 ÷ 1,000」換算為張；成交金額為'
        '「成交金額 ÷ 10<sup>8</sup>」換算為億元。</p>\n'
        f'        <p>走勢圖取 <b>近 {N_WINDOW} 個交易日</b>'
        f'（{win[0]["date"]}–{win[-1]["date"]}）之收盤指數與成交金額，來源為 <b>FMTQIK</b>；'
        f'收盤指數並與 <b>MI_5MINS_HIST</b> 逐日交叉驗證。</p>\n'
        f'        <p><b>MA5／MA20</b> 為簡單移動平均，以完整歷史（{len(hist_dates)} 個交易日，'
        f'{h_start}–{h_end}）計算後取最近 {N_WINDOW} 日呈現，因此區間內每一點都有值，'
        f'無起點斷線。均線單位為指數點。</p>\n'
        f'        <p><b>MA 交叉事件</b>同樣由上述 {len(hist_dates)} 日歷史計算：以每日收盤值'
        f'求 MA5 與 MA20，逐日比較其差值，差值由負轉正（MA5 上穿 MA20）記為'
        f'<b>黃金交叉</b>、由正轉負（MA5 下穿 MA20）記為<b>死亡交叉</b>，共 '
        f'<b>{len(c_all)} 筆</b>，全數由實際收盤資料算出，未經人工指定；其中 '
        f'{len(c_win)} 筆落在走勢圖的 {N_WINDOW} 日區間內並於圖上以垂直虛線、圓點與'
        f'日期標籤標示。</p>\n'
        f'        <p>成交量副圖採<b>成交金額（億元）</b>作為量能指標，柱色依'
        f'<b>當日收盤相對前一交易日</b>漲跌著色（紅漲、綠跌，台股慣例）；'
        f'本區間 {n_up_days} 天上漲、{n_dn_days} 天下跌。</p>\n'
        '        <p><b>即時更新：</b>點「重新整理」後，前端以 <code>fetch</code> 直接呼叫上列 '
        '<code>www.twse.com.tw</code> 端點。實測 <code>mis.twse.com.tw</code>'
        '（getStockInfo.jsp）未回傳 <code>Access-Control-Allow-Origin</code>，'
        '瀏覽器跨來源 <code>fetch</code> 會被 CORS 阻擋，且該端點不支援 JSONP callback'
        '（回傳內容不會包成 callback(...)），故<b>不採用</b>；本頁改用同樣由證交所提供、'
        '且具 CORS 標頭的 <code>www.twse.com.tw</code> 端點。盤中為'
        '<b>延遲約 5 秒至 1 分鐘</b>之資料，非逐筆即時報價。</p>\n'
        '        <p><b>大盤即時</b>採 MI_5MINS_INDEX 期間最後一筆之發行量加權股價指數；'
        '<b>個股即時</b>採 STOCK_DAY_ALL 之收盤價、漲跌價差、成交股數與成交金額'
        '（換算為張與億元），並<b>重新計算漲跌幅排行</b>；漲跌家數與成交金額採 '
        'MI_INDEX type=MS。</p>\n'
        '        <p><b>排行排除規則：</b>非 4 碼代號（ETF／ETN／受益證券）、'
        '<code>0xxx</code> ETF、名稱結尾 <code>-DR</code> 之 TDR、成交股數為 0 或空白、'
        '收盤價空白（官方列為「未成交」或「無比價」），以及漲跌價差無法解析者，'
        '一律不納入排行。</p>\n'
        '        <p><b>MA5／MA20 均線與成交量副圖仍為內建快照</b>，不隨「重新整理」變動'
        '（歷史日資料端點於盤後更新，非盤中即時）；但會由 GitHub Actions 每日排程'
        '自動更新，因此重新載入頁面即可看到最新收盤的均線與成交量。</p>\n'
        '        <p>若 API 無法連線（離線、證交所端點異常或 CORS 變更），頁面會自動'
        '<b>退回內建快照</b>並以紅色提示標示，不會顯示空白或錯誤數字。</p>\n'
        '      </div>')

    R["FOOTCOPY"] = (
        '    <p class="copy">台股追蹤｜加權指數＆自選股概況（即時更新）\u3000·\u3000'
        '資料來源：臺灣證券交易所（TWSE）公開資料\u3000·\u3000'
        f'內建快照最後更新：{cfg["fetched"]}（由 GitHub Actions 每日自動更新）</p>')

    R["HTML_STATUS"] = (f'<span id="status-text">目前顯示 <b>靜態快照</b>（內建，{ad} 13:30 收盤）'
                        f'— 點右上角「重新整理」取得最新報價</span>')
    R["HTML_LASTUPD"] = (f'<b class="upd" id="last-updated">{cfg["fetched"]}</b>')
    R["JS_SNAPAT"] = f"var SNAPSHOT_AT = '{cfg['fetched']}';"
    R["JS_STATINIT"] = f"setStatus('', '{R['HTML_STATUS']}');"
    return R


def _region_styles(name: str):
    """Marker syntax depends on the host language: regions inside <script> must
    use JS block comments, because an HTML comment inside a <script> element is
    treated as a LINE comment by browsers and would silently break the JS."""
    js = (r"/\*SNAP:" + re.escape(name) + r"\*/",
          r"/\*SNAP:/" + re.escape(name) + r"\*/")
    htm = (r"<!--SNAP:" + re.escape(name) + r"-->",
           r"<!--/SNAP:" + re.escape(name) + r"-->")
    if name in JS_REGIONS:
        return [js, htm]
    return [htm, js]


def _region_body(html: str, name: str):
    """Return the body between a region's markers, or None."""
    for o, c in _region_styles(name):
        m = re.search(r"(" + o + r")(.*?)(" + c + r")", html, re.S)
        if m:
            return m
    return None


def apply_regions(html: str, regions: dict[str, str]) -> str:
    for name, content in regions.items():
        rx = re.compile(r"(" + _region_styles(name)[0][0] + r")(.*?)("
                        + _region_styles(name)[0][1] + r")", re.S)
        html, n = rx.subn(lambda m, c=content: m.group(1) + c + m.group(3), html)
        if n != 1:
            raise RuntimeError(f"marker {name} 出現 {n} 次（預期 1 次）")
    return html


def verify_output(html: str, regions: dict[str, str]) -> None:
    script = re.search(r"<script>(.*?)</script>", html, re.S)
    if script and "<!--SNAP:" in script.group(1):
        raise RuntimeError("<script> 內出現 HTML 註解 marker，會使 JS 變成註解")
    for name in regions:
        body = _region_body(html, name)
        if body and not body.group(2).strip():
            raise RuntimeError(f"偵測到空白 marker 區塊：{name}")
    for tag in ("<html", "</html>", "<style", "</style>", "<script", "</script>", "</svg>"):
        if tag not in html:
            raise RuntimeError(f"輸出缺少必要標籤：{tag}")
    m = re.search(r'id="idx-val">([^<]*)<', html)
    if not m or not re.fullmatch(r"[\d,]+\.\d{2}", m.group(1).strip()):
        raise RuntimeError(f"idx-val 數值異常：{m.group(1) if m else '缺失'!r}")
    m = re.search(r'id="kpi-amt">([^<]*)<', html)
    if not m or not re.fullmatch(r"[\d,]+\.\d{2}", m.group(1).strip()):
        raise RuntimeError(f"kpi-amt 數值異常：{m.group(1) if m else '缺失'!r}")
    for tag in ("None", "nan", "NaN", "undefined", "{{"):
        if tag in "".join(regions.values()):
            raise RuntimeError(f"輸出內容含異常字串：{tag}")
    for region in ("SVG", "CXLIST", "LISTGAIN", "LISTLOSE", "LISTTSMC", "RANKBAR", "FOOTB"):
        body = _region_body(html, region)
        if not body or len(body.group(2).strip()) < 50:
            raise RuntimeError(f"{region} 內容過短或空白")


    # --- 圖表完整性 -------------------------------------------------------
    # 這幾項是回歸防護：整個 SVG 區塊必須自帶 <svg>/</svg> wrapper 與必要元素。
    # 少了 wrapper，所有圖表子元素會變成未知的 HTML 元素而完全不繪製；未閉合的
    # HTML 註解則會吞掉後續元素。兩者都會讓圖表空白，但元素「數量」仍可能正確，
    # 因此必須逐項檢查類別是否存在。
    svg = _region_body(html, "SVG")
    if svg is None:
        raise RuntimeError("缺少 SNAP:SVG 區塊")
    body = svg.group(2)
    if "<svg" not in body or "</svg>" not in body:
        raise RuntimeError("SVG 區塊缺少 <svg> 或 </svg> wrapper，圖表將無法繪製")
    for need, label in (('viewBox="0 0 1000 420"', "viewBox"),
                        ("<title>", "SVG <title>（無障礙）"),
                        ('role="img"', 'role="img"'),
                        ('class="area-f"', "收盤面積 area-f"),
                        ('class="line-p"', "收盤折線 line-p"),
                        ('class="line-ma5"', "MA5 折線"),
                        ('class="line-ma20"', "MA20 折線"),
                        ('class="cx-dot"', "均線交叉標記"),
                        ('class="v-up"', "紅色量柱"),
                        ('class="v-down"', "綠色量柱")):
        if need not in body:
            raise RuntimeError(f"SVG 區塊缺少 {label}（{need}）")
    if body.count("<rect") < N_WINDOW:
        raise RuntimeError(f"成交量柱不足：{body.count('<rect')} 根（預期 {N_WINDOW}）")
    if body.count("<polyline") < 3:
        raise RuntimeError(f"折線不足：{body.count('<polyline')} 條（預期 3）")
    if not re.search(r'<svg[^>]*role="img"', body):
        raise RuntimeError("<svg> 缺少 role=\"img\"（無障礙）")

    # 未閉合的 HTML 註解（例如 "<!-- ===== 標題 =====" 少了 -->）會把後面整段
    # 標記吞進註解，造成元素消失卻不報錯。
    for m in re.finditer(r"<!--", html):
        if "-->" not in html[m.start():m.start() + 400]:
            raise RuntimeError("偵測到未閉合的 HTML 註解，會吞掉後續元素："
                               + html[m.start():m.start() + 60].replace("\n", " "))

def meta_stripped(html: str) -> str:
    """Remove the volatile "last updated" values so two runs on the same
    trading day compare equal (otherwise every run would look changed and
    create an empty commit)."""
    return re.sub(r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}", "<TS>", html)


def atomic_write(path: str, text: str) -> None:
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".snapshot-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def collect() -> dict:
    print("[1/5] 抓取 STOCK_DAY_ALL（全市場每日收盤行情）…")
    rows, ad = get_stock_day_all()
    print(f"      → {len(rows):,d} 檔，資料日期 {ad}")

    print("[2/5] 抓取 MI_INDEX type=MS（成交金額、漲跌家數）…")
    mkt = get_market_stats(ad)
    print(f"      → 總成交金額 {mkt['total_amt'] / 1e8:,.2f} 億元、"
          f"整體 上漲 {mkt['mkt_up']:,d} / 下跌 {mkt['mkt_down']:,d} / "
          f"持平 {mkt['mkt_flat']:,d}；股票 上漲 {mkt['up']:,d} / "
          f"下跌 {mkt['down']:,d} / 持平 {mkt['flat']:,d}")

    print("[3/5] 抓取 FMTQIK（近 14 個月加權指數歷史）…")
    hist = get_index_history()
    hd = [r["date"] for r in hist]
    print(f"      → {len(hist):,d} 個交易日（{hd[0]} – {hd[-1]}）")

    print("[4/5] 抓取 MI_5MINS_HIST（當日開高低收）並做一致性驗證…")
    ohlc = get_today_ohlc(ad)
    if hd[-1] != ad:
        raise RuntimeError(f"FMTQIK 最後一日 {hd[-1]} 與 STOCK_DAY_ALL 日期 {ad} 不一致")
    if abs(hist[-1]["close"] - ohlc["close"]) > 0.005:
        raise RuntimeError(f"收盤指數不一致：FMTQIK {hist[-1]['close']} vs "
                           f"MI_5MINS_HIST {ohlc['close']}")
    calc_chg = hist[-1]["close"] - hist[-2]["close"]
    if hist[-1]["chg"] is None or abs(hist[-1]["chg"] - calc_chg) > 0.02:
        raise RuntimeError(f"{ad} 漲跌點數與前後收盤差不符：FMTQIK {hist[-1]['chg']} vs "
                           f"{calc_chg:.2f}")
    print(f"      → 開 {ohlc['open']:,.2f} 高 {ohlc['high']:,.2f} "
          f"低 {ohlc['low']:,.2f} 收 {ohlc['close']:,.2f}；"
          f"前一日 {hist[-2]['date']} 收 {hist[-2]['close']:,.2f}"
          f"（漲跌 {hist[-1]['chg']:+.2f} 點 一致）")

    closes = [r["close"] for r in hist]
    ma5, ma20 = sma(closes, 5), sma(closes, 20)
    c_all = find_crossovers(hist, ma5, ma20, max(0, len(hist) - HISTORY_DAYS))
    win_i = list(range(len(hist) - N_WINDOW, len(hist)))
    win = [hist[i] for i in win_i]
    win_ma5, win_ma20 = [ma5[i] for i in win_i], [ma20[i] for i in win_i]
    win_prev = [(hist[i - 1]["close"] if i > 0 else None) for i in win_i]
    if any(v is None for v in win_ma5 + win_ma20):
        raise RuntimeError("走勢圖區間內仍有均線空值，歷史長度不足")

    pos = {r["date"]: n for n, r in enumerate(win)}
    c_win = []
    for c in c_all:
        if c["date"] in pos:
            c2 = dict(c)
            c2["wi"] = pos[c["date"]]
            c_win.append(c2)
    c_win.sort(key=lambda x: x["wi"])
    lrows = assign_label_rows(c_win)

    print("[5/5] 計算母體、漲跌幅排行與圖表座標 …")
    uni = rank_universe(rows)
    if len(uni) < 100:
        raise RuntimeError(f"可用個股母體過少（{len(uni)} 檔）")
    gain = sorted(uni, key=lambda x: (-x["pct"], x["code"]))[:10]
    lose = sorted(uni, key=lambda x: (x["pct"], x["code"]))[:10]
    if len(gain) < 10 or len(lose) < 10:
        raise RuntimeError(f"排行結果不足（漲 {len(gain)} / 跌 {len(lose)}）")
    tsmc = next((r for r in uni if r["code"] == "2330"), None)
    if tsmc is None:
        raise RuntimeError("母體中找不到 2330 台積電")

    ax = axis_layout([v for v in (closes[-N_WINDOW:] + win_ma5 + win_ma20) if v is not None])
    amts = [r["amt"] for r in win if r["amt"] is not None]
    if len(amts) != N_WINDOW:
        raise RuntimeError(f"走勢圖區間成交金額不完整（{len(amts)}/{N_WINDOW}）")
    vl = vol_layout(max(amts))

    print(f"      → 母體 {len(uni):,d} 檔；漲幅首 {gain[0]['code']} "
          f"{gain[0]['pct']:+.2f}%；跌幅首 {lose[0]['code']} {lose[0]['pct']:+.2f}%；"
          f"2330 收 {tsmc['close']:,.2f}（{tsmc['pct']:+.2f}%）")
    print(f"      → 走勢圖 {win[0]['date']}–{win[-1]['date']}；"
          f"MA5 {win_ma5[-1]:,.2f}、MA20 {win_ma20[-1]:,.2f}；"
          f"區間內交叉 {len(c_win)} 筆／全期 {len(c_all)} 筆；"
          f"價格軸 {ax['lo']:,.0f}–{ax['hi']:,.0f}（{len(ax['ticks'])} 格）；"
          f"量軸上限 {vl['vmax']:,.0f} 億")
    return {"rows": rows, "date": ad, "mkt": mkt, "hist": hist, "ohlc": ohlc,
            "today_close": ohlc["close"], "prev_close": hist[-2]["close"],
            "prev_ad": hist[-2]["date"], "win": win, "win_ma5": win_ma5,
            "win_ma20": win_ma20, "win_prev": win_prev, "crosses_all": c_all,
            "crosses_in_win": c_win, "label_rows": lrows, "uni_n": len(uni),
            "gain": gain, "lose": lose, "tsmc": tsmc, "ax": ax, "vl": vl}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="index.html")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force-meta", action="store_true",
                    help="也重新寫入「資料擷取時間」（即使資料本身無變化）")
    args = ap.parse_args()

    path = args.file
    if not os.path.exists(path):
        print(f"ERROR: 找不到 {path}", file=sys.stderr)
        return 2
    src = open(path, encoding="utf-8").read()
    if "SNAP:SVG" not in src:
        print(f"ERROR: {path} 缺少 SNAP marker，請先執行 inject_markers.py", file=sys.stderr)
        return 2

    try:
        cfg = collect()
        # 資料本身無變化時保留既有的「資料擷取時間」，避免每個排程日都因時間戳
        # 而產生一次實質無變化的 commit（例如週末／假日重跑）。
        if args.force_meta:
            stamp = datetime.now(TZ_TAIPEI).strftime("%Y/%m/%d %H:%M:%S")
            source = "--force-meta"
        else:
            found = re.findall(r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}", src)
            if not found:
                raise RuntimeError("檔案中找不到既有的資料擷取時間，請以 --force-meta 執行")
            stamp = min(found)
            source = "沿用檔案中既有時間戳"
        cfg["fetched"] = stamp
        print(f"      資料擷取時間：{stamp}（{source}）")
        regions = build_regions(cfg)
        out = apply_regions(src, regions)
        verify_output(out, regions)
    except Exception as e:  # noqa: BLE001
        print(f"\nERROR: {type(e).__name__}: {e}", file=sys.stderr)
        print("未寫入任何檔案（原檔保留）。", file=sys.stderr)
        return 1

    data_changed = meta_stripped(out) != meta_stripped(src)
    meta_changed = out != src
    changed = data_changed or meta_changed
    print(f"\n輸出 {len(out):,d} bytes（原 {len(src):,d}）；"
          f"資料{'已變更' if data_changed else '無變更'}、"
          f"時間戳{'已變更' if meta_changed else '相同'}")
    if args.dry_run:
        print("dry-run：不寫入檔案。")
    else:
        if changed:
            atomic_write(path, out)
            print(f"已更新 {path}")
        else:
            print(f"{path} 內容無變化，未寫入。")
    # CHANGED=1 代表「有實際資料變更」，workflow 只在 CHANGED=1 時建立 commit。
    print(f"CHANGED={'1' if data_changed else '0'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
