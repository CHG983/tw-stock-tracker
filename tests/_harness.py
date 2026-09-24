#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_harness.py — 測試共用工具：以 TWSE 真實回應的樣本（tests/fixtures/）取代所有網路請求。

檔名不以 test_ 開頭，因此不會被 unittest discovery 收集。

核心設計
--------
`FakeAPI` 攔截 `urllib.request.urlopen`，依「端點路徑」回傳對應的樣本檔內容，
並支援**逐端點故障注入**（HTTP 500、timeout、空回應、HTML 錯誤頁、壞 JSON、
欄位缺失、數值無法解析、只回少量資料列…），用來驗證「抓取失敗 → 非 0 結束且
不破壞原檔」。

樣本檔由 tests/capture_fixtures.py 從 TWSE 實際擷取；測試本身永不連網。
"""
from __future__ import annotations

import collections
import contextlib
import io
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURES = os.path.join(HERE, "fixtures")
SCRIPTS = os.path.join(ROOT, "scripts")

if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import update_snapshot as us  # noqa: E402

TZ_TAIPEI = timezone(timedelta(hours=8))

# 24 組 SNAP 區塊（build_regions 回傳鍵集合）
EXPECTED_REGIONS = {
    "TITLE", "HEROTAG", "IDXROW", "KPIS", "BREADTH", "BRDATE", "SVG", "CHARTSUB",
    "MASTRIP", "CXLIST", "CXMKEY", "CXNOTE", "LISTGAIN", "LISTLOSE", "LISTTSMC",
    "RANKBAR", "RANKNOTE", "FOOTA", "FOOTB", "FOOTCOPY", "HTML_STATUS",
    "HTML_LASTUPD", "JS_SNAPAT", "JS_STATINIT",
}

# 端點 → 樣本檔（FMTQIK 依 ?date=YYYYMM01 的月份取樣本）
PATH_STOCK = "/rwd/zh/afterTrading/STOCK_DAY_ALL"
PATH_MI = "/rwd/zh/afterTrading/MI_INDEX"
PATH_FMT = "/rwd/zh/afterTrading/FMTQIK"
PATH_5MIN = "/rwd/zh/TAIEX/MI_5MINS_HIST"
ALL_PATHS = (PATH_STOCK, PATH_MI, PATH_FMT, PATH_5MIN)


def fixture_path(rel: str) -> str:
    return os.path.join(FIXTURES, rel)


def read_fixture(rel: str) -> str:
    with open(fixture_path(rel), encoding="utf-8") as f:
        return f.read()


def meta() -> dict:
    return json.loads(read_fixture("meta.json"))


def data_date() -> str:
    """樣本對應的資料日期（AD 格式）。"""
    return meta()["data_date"]


# --------------------------------------------------------------------------
# 故障注入函式：輸入原始樣本內容，輸出被破壞後的內容（或拋出例外）
# --------------------------------------------------------------------------
def _as_json(text: str) -> dict:
    return json.loads(text)


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _f_empty(body, params):
    return ""


def _f_html(body, params):
    return "<!DOCTYPE html><html><body>503 Service Unavailable</body></html>"


def _f_badjson(body, params):
    return '{"stat": "OK", "data": [1, 2,'


def _f_mi_stat_bad(body, params):
    """stat != OK（例如假日查無資料）。"""
    j = _as_json(body)
    j["stat"] = "很抱歉，沒有符合條件的資料!"
    return _dump(j)


def _f_mi_fields_stripped(body, params):
    """把 tables 的 fields 全部清空 → 表頭辨識必須失敗。"""
    j = _as_json(body)
    for t in (j.get("tables") or []):
        t["fields"] = []
    return _dump(j)


def _f_fmt_drop_last_month(body, params):
    """回傳空資料（等同該月無交易日）。"""
    j = _as_json(body)
    j["data"] = []
    j["stat"] = "OK"
    return _dump(j)


def _f_stock_duplicate_date(body, params):
    """資料日期欄位格式錯誤 → roc_to_ad 應拋錯。"""
    return body.replace('"1150924"', '"115/09/24"', 1)


def _f_null_tables(body, params):
    j = _as_json(body)
    j["tables"] = None
    return _dump(j)


def _f_empty_tables(body, params):
    j = _as_json(body)
    j["tables"] = [{"title": None, "fields": [], "data": []}]
    return _dump(j)


def _f_mi_drop_up(body, params):
    """移除漲跌家數表的『上漲』列 → 必須被視為缺欄位。"""
    j = _as_json(body)
    for t in (j.get("tables") or []):
        rows = t.get("data") or []
        if rows and any(str(r[0]).startswith("上漲") for r in rows):
            t["data"] = [r for r in rows if not str(r[0]).startswith("上漲")]
    return _dump(j)


def _f_mi_drop_total(body, params):
    """移除成交統計的『總計』與『證券合計』列 → 必須被視為缺欄位。"""
    j = _as_json(body)
    for t in (j.get("tables") or []):
        rows = t.get("data") or []
        if rows and any("成交金額" in str(c) for c in (t.get("fields") or [])):
            t["data"] = [r for r in rows
                         if not (str(r[0]).startswith("總計") or str(r[0]).startswith("證券合計"))]
    return _dump(j)


def _f_mi_drop_ordinal(body, params):
    j = _as_json(body)
    for t in (j.get("tables") or []):
        rows = t.get("data") or []
        if rows and any("成交金額" in str(c) for c in (t.get("fields") or [])):
            t["data"] = [r for r in rows if not str(r[0]).startswith("1.一般股票")]
    return _dump(j)


def _f_mi_bad_count(body, params):
    """把『上漲』的整體市場家數換成無法解析的怪字串。"""
    j = _as_json(body)
    for t in (j.get("tables") or []):
        for r in (t.get("data") or []):
            if str(r[0]).startswith("上漲"):
                r[1] = "\u2014"          # em dash，非數字
                r[2] = "\u2014"
    return _dump(j)


def _f_stock_empty_data(body, params):
    """只留標題列 → 資料列數不足。"""
    lines = body.splitlines()
    return "\n".join(lines[:1]) + "\n"


def _f_stock_few_rows(body, params):
    lines = body.splitlines()
    return "\n".join(lines[:11]) + "\n"


def _f_stock_header_only(body, params):
    return ""


def _f_stock_bad_close(body, params):
    """把某檔個股的收盤價換成 N/A → 該檔應被排除（而非讓整支程式崩潰）。"""
    import csv
    import io as _io
    rows = list(csv.reader(_io.StringIO(body)))
    if len(rows) > 1:
        rows[1][8] = "N/A"
    out = _io.StringIO()
    csv.writer(out, lineterminator="\n").writerows(rows)
    return out.getvalue()


def _f_stock_bad_date(body, params):
    rows = body.splitlines()
    if len(rows) > 1:
        rows[1] = rows[1].replace('"1150924"', '"?BAD?"', 1)
    return "\n".join(rows) + "\n"


def _f_fmt_empty(body, params):
    j = _as_json(body)
    j["data"] = []
    return _dump(j)


def _f_fmt_stat_bad(body, params):
    j = _as_json(body)
    j["stat"] = "很抱歉，沒有符合條件的資料!"
    return _dump(j)


def _f_fmt_bad_close(body, params):
    j = _as_json(body)
    if j.get("data"):
        j["data"][len(j["data"]) // 2][4] = "--"
    return _dump(j)


def _f_fmt_bad_amt(body, params):
    j = _as_json(body)
    if j.get("data"):
        j["data"][len(j["data"]) // 2][2] = ""
    return _dump(j)


def _f_fmt_missing_recent(body, params):
    """移除最後 25 個交易日 → 歷史長度不足（MA20 無法在視窗內有值）。"""
    j = _as_json(body)
    if j.get("data"):
        j["data"] = j["data"][:5]
    return _dump(j)


def _f_5min_empty(body, params):
    j = _as_json(body)
    j["data"] = []
    return _dump(j)


def _f_5min_bad_ohlc(body, params):
    j = _as_json(body)
    if j.get("data"):
        j["data"][-1][4] = "--"
    return _dump(j)


def _f_5min_shift_date(body, params):
    """把最後一列日期改成別的日期 → 找不到當日資料。"""
    j = _as_json(body)
    if j.get("data"):
        j["data"][-1][0] = "115/01/02"
    return _dump(j)


_FAULT_FUNCS = {
    "empty_body": _f_empty,
    "html_error": _f_html,
    "bad_json": _f_badjson,
    "mi_stat_bad": _f_mi_stat_bad,
    "mi_fields_stripped": _f_mi_fields_stripped,
    "fmt_empty_month": _f_fmt_drop_last_month,
    "stock_full_roc_date": _f_stock_duplicate_date,
    "null_tables": _f_null_tables,
    "empty_tables": _f_empty_tables,
    "mi_drop_up": _f_mi_drop_up,
    "mi_drop_total": _f_mi_drop_total,
    "mi_drop_ordinal": _f_mi_drop_ordinal,
    "mi_bad_count": _f_mi_bad_count,
    "stock_empty_data": _f_stock_empty_data,
    "stock_few_rows": _f_stock_few_rows,
    "stock_header_only": _f_stock_header_only,
    "stock_bad_close": _f_stock_bad_close,
    "stock_bad_date": _f_stock_bad_date,
    "fmt_empty": _f_fmt_empty,
    "fmt_stat_bad": _f_fmt_stat_bad,
    "fmt_bad_close": _f_fmt_bad_close,
    "fmt_bad_amt": _f_fmt_bad_amt,
    "fmt_missing_recent": _f_fmt_missing_recent,
    "5min_empty": _f_5min_empty,
    "5min_bad_ohlc": _f_5min_bad_ohlc,
    "5min_shift_date": _f_5min_shift_date,
}

# 會直接拋出例外的故障
_FAULT_ERRORS = {
    "http_500": lambda url: urllib.error.HTTPError(url, 500, "Internal Server Error", {}, None),
    "http_404": lambda url: urllib.error.HTTPError(url, 404, "Not Found", {}, None),
    "timeout": lambda url: TimeoutError("timed out"),
    "urlerror": lambda url: urllib.error.URLError("connection reset by peer"),
    "conn_refused": lambda url: ConnectionRefusedError(111, "Connection refused"),
}

FAULT_NAMES = sorted(list(_FAULT_FUNCS) + list(_FAULT_ERRORS))


class FakeResponse:
    """模擬 urlopen() 回傳的 context manager 物件。"""

    status = 200
    headers: dict = {}

    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeAPI:
    """以樣本檔取代 TWSE；支援逐端點故障注入與呼叫計數。"""

    def __init__(self, faults: dict | None = None):
        self.faults: dict[str, str] = dict(faults or {})
        self.calls: list[str] = []
        self.attempts: collections.Counter = collections.Counter()
        self._months = {m for m in meta()["months"]}

    # --- 設定 ---------------------------------------------------------
    def set_fault(self, path: str, name: str) -> None:
        assert name in FAULT_NAMES, f"未知故障名稱：{name}"
        self.faults[path] = name

    def clear_fault(self, path: str) -> None:
        self.faults.pop(path, None)

    def reset_counts(self) -> None:
        self.calls.clear()
        self.attempts.clear()

    # --- 解析請求 -----------------------------------------------------
    @staticmethod
    def parse(url: str) -> tuple[str, dict]:
        rest = url[len(us.BASE):] if url.startswith(us.BASE) else url
        if "?" in rest:
            path, q = rest.split("?", 1)
            params = dict(kv.split("=", 1) for kv in q.split("&") if "=" in kv)
        else:
            path, params = rest, {}
        return path, params

    def _body_for(self, path: str, params: dict) -> str:
        if path == PATH_STOCK:
            return read_fixture("stock_day_all.csv")
        if path == PATH_MI:
            return read_fixture("mi_index_ms.json")
        if path == PATH_5MIN:
            return read_fixture("mi_5mins_hist.json")
        if path == PATH_FMT:
            m = str(params.get("date", ""))[:6]
            if m not in self._months:
                raise AssertionError(f"樣本缺少月份 {m}；請重跑 capture_fixtures.py")
            return read_fixture(os.path.join("fmtqik", f"{m}.json"))
        raise AssertionError(f"未預期的端點：{path}")

    # --- urlopen 替身 -------------------------------------------------
    def urlopen(self, req, timeout=None, context=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        path, params = self.parse(url)
        self.calls.append(url)
        self.attempts[path] += 1

        fault = self.faults.get(path)
        if fault:
            if fault in _FAULT_ERRORS:
                raise _FAULT_ERRORS[fault](url)
            body = self._body_for(path, params)
            body = _FAULT_FUNCS[fault](body, params)
            return FakeResponse(body.encode("utf-8"))

        return FakeResponse(self._body_for(path, params).encode("utf-8"))

    def __enter__(self):
        # 同時攔截 urlopen 與 time.sleep（重試等待會讓測試變慢）
        self._p1 = mock.patch.object(urllib.request, "urlopen", self.urlopen)
        self._p2 = mock.patch.object(us.time, "sleep", lambda *_: None)
        self._p1.start()
        self._p2.start()
        return self

    def __exit__(self, *exc):
        self._p2.stop()
        self._p1.stop()
        return False


# --------------------------------------------------------------------------
# 執行 main() 並擷取輸出
# --------------------------------------------------------------------------
class RunResult:
    def __init__(self, rc: int, out: str, err: str):
        self.rc, self.out, self.err = rc, out, err

    @property
    def changed(self) -> str | None:
        import re
        m = re.search(r"CHANGED=([01])", self.out)
        return m.group(1) if m else None

    @property
    def wrote(self) -> bool:
        return "已更新" in self.out

    def __repr__(self):
        return f"<RunResult rc={self.rc} CHANGED={self.changed}>\n{self.out}\n{self.err}"


def run_cli(argv: list[str]) -> RunResult:
    """在行程內呼叫 update_snapshot.main()，擷取 stdout／stderr 與回傳碼。"""
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", ["update_snapshot.py"] + list(argv)):
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = us.main()
    return RunResult(rc, out.getvalue(), err.getvalue())


# --------------------------------------------------------------------------
# HTML 測試檔
# --------------------------------------------------------------------------
def repo_index_html() -> str:
    with open(os.path.join(ROOT, "index.html"), encoding="utf-8") as f:
        return f.read()


def write_html(path: str, text: str | None = None) -> str:
    with open(path, "w", encoding="utf-8") as f:
        f.write(repo_index_html() if text is None else text)
    return path


def read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


# --------------------------------------------------------------------------
# 凍結時間（驗證時間戳邏輯）
# --------------------------------------------------------------------------
class FrozenDatetime(datetime):
    """datetime.datetime 的子類，now() 固定回傳指定時刻。"""

    fixed: datetime | None = None

    @classmethod
    def now(cls, tz=None):  # noqa: D102
        if cls.fixed is None:
            return super().now(tz)
        return cls.fixed if tz is None else cls.fixed.astimezone(tz)


@contextlib.contextmanager
def freeze_now(moment: datetime):
    old = FrozenDatetime.fixed
    FrozenDatetime.fixed = moment
    with mock.patch.object(us, "datetime", FrozenDatetime):
        yield moment
    FrozenDatetime.fixed = old


def now_taipei() -> datetime:
    return datetime.now(TZ_TAIPEI)


# --------------------------------------------------------------------------
# 獨立實作（測試用交叉驗證，不呼叫 update_snapshot 的函式）
# --------------------------------------------------------------------------
def independent_history_from_fixtures() -> list[dict]:
    """從樣本檔獨立重建加權指數歷史（AD 日期、收盤、成交金額億元），
    不使用 update_snapshot 的解析函式，以便交叉比對。"""
    acc: dict[str, dict] = {}
    for m in sorted(meta()["months"]):
        j = json.loads(read_fixture(os.path.join("fmtqik", f"{m}.json")))
        assert j["stat"] == "OK"
        for r in j["data"]:
            y, mo, d = r[0].split("/")
            ad = f"{int(y) + 1911:04d}/{int(mo):02d}/{int(d):02d}"
            acc[ad] = {
                "date": ad,
                "close": float(r[4].replace(",", "")),
                "amt": float(r[2].replace(",", "")) / 1e8,
                "chg": float(r[5].replace(",", "")),
            }
    return sorted(acc.values(), key=lambda x: x["date"])


def independent_sma(vals: list[float], n: int) -> list[float | None]:
    out = []
    for i in range(len(vals)):
        out.append(sum(vals[i - n + 1:i + 1]) / n if i >= n - 1 else None)
    return out


def independent_crossovers(rows: list[dict], n: int = 5, m: int = 20) -> list[dict]:
    closes = [r["close"] for r in rows]
    a, b = independent_sma(closes, n), independent_sma(closes, m)
    out = []
    for i in range(1, len(rows)):
        if a[i] is None or b[i] is None or a[i - 1] is None or b[i - 1] is None:
            continue
        d0, d1 = a[i - 1] - b[i - 1], a[i] - b[i]
        if d0 < 0 <= d1:
            out.append({"date": rows[i]["date"], "kind": "g"})
        elif d0 > 0 >= d1:
            out.append({"date": rows[i]["date"], "kind": "d"})
    return out
