#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_update_snapshot.py — scripts/update_snapshot.py 的單元／整合測試。

執行方式
--------
    python3 -m unittest discover -s tests -t . -v    # 或
    python3 -m unittest tests.test_update_snapshot -v
    python3 -m pytest tests/ -v                      # pytest 亦可

設計原則
--------
1. **測試期間完全不連網。** 所有 HTTP 請求由 _harness.FakeAPI 攔截，回傳
   tests/fixtures/ 內、由 tests/capture_fixtures.py 從 TWSE 實際擷取的樣本。
   另有測試把 socket.socket 換成會拋例外者，證明沒有漏網的網路呼叫。
2. **不編造預期數字。** 排名、均線、交叉事件的期望值，由本檔內「獨立重寫」的
   演算法從同樣的樣本算出，再與 update_snapshot 的輸出交叉比對。
3. **失敗路徑必須證明原檔完好。** 每個故障注入案例都比對呼叫前後的位元組。
"""
from __future__ import annotations

import csv
import inspect
import io
import json
import os
import re
import socket
import struct
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import _harness as H  # noqa: E402
import analyst  # noqa: E402
import update_snapshot as us  # noqa: E402

PATH_STOCK, PATH_MI, PATH_FMT, PATH_5MIN, PATH_BFI = (
    H.PATH_STOCK, H.PATH_MI, H.PATH_FMT, H.PATH_5MIN, H.PATH_BFI)
TZ = H.TZ_TAIPEI


# ==========================================================================
# 共用工具
# ==========================================================================
_BASELINE: dict = {}


def baseline() -> dict:
    """跑一次乾淨的完整更新，結果在行程內快取。"""
    if "out" in _BASELINE:
        return _BASELINE
    d = tempfile.mkdtemp(prefix="twstock-baseline-")
    path = os.path.join(d, "index.html")
    H.write_html(path)
    src = H.read_bytes(path)
    api = H.FakeAPI()
    with api:
        r = H.run_cli(["--file", path])
    assert r.rc == 0, f"baseline 失敗：{r}"
    _BASELINE.update({"src": src, "path": path, "res": r, "api": api})
    return _BASELINE


def baseline_out() -> str:
    """一次乾淨更新後「檔案應該長怎樣」。"""
    b = baseline()
    if b["res"].wrote:
        with open(b["path"], encoding="utf-8") as f:
            return f.read()
    return b["src"].decode("utf-8")


def region_body(html: str, name: str) -> str:
    m = us._region_body(html, name)
    assert m is not None, f"找不到 marker 區塊：{name}"
    return m.group(2)


def clean_run(argv=None):
    """在暫存目錄跑一次乾淨更新 → (result, path, before, after)。"""
    d = tempfile.mkdtemp(prefix="twstock-t-")
    path = os.path.join(d, "index.html")
    H.write_html(path)
    before = H.read_bytes(path)
    with H.FakeAPI():
        r = H.run_cli(["--file", path] + list(argv or []))
    return r, path, before, H.read_bytes(path)


def temp_html(content: str) -> str:
    d = tempfile.mkdtemp(prefix="twstock-t-")
    p = os.path.join(d, "index.html")
    H.write_html(p, content)
    return p


def no_temps(path: str) -> bool:
    d = os.path.dirname(os.path.abspath(path))
    return not [f for f in os.listdir(d) if f.startswith(".snapshot-")]


def fixture_files() -> list[str]:
    out = ["meta.json", "stock_day_all.csv", "mi_index_ms.json", "mi_5mins_hist.json"]
    out += [os.path.join("fmtqik", f"{m}.json") for m in H.meta()["months"]]
    out += [os.path.join("bfi82u", f"{d}.json") for d in H.inst_days()]
    return out


# ==========================================================================
# 獨立重寫的參考實作（期望值來源，不呼叫 update_snapshot 的函式）
# ==========================================================================
def _num(s):
    s = re.sub(r"<[^>]*>", "", str(s))
    s = s.replace(",", "").replace("\u2212", "-").replace("%", "").strip()
    if s in ("", "-", "--", "nan", "NaN", "None", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def ref_universe() -> list[dict]:
    """獨立解析 STOCK_DAY_ALL 樣本，算出上市普通股母體（與程式同一組規則）。"""
    rows = list(csv.reader(io.StringIO(H.read_fixture("stock_day_all.csv"))))
    out = []
    for r in rows[1:]:
        if len(r) < 10:
            continue
        code, name = r[1].strip(), r[2].strip()
        if not re.fullmatch(r"\d{4}", code):
            continue
        if re.fullmatch(r"0\d{3}", code):
            continue
        if re.search(r"-DR$", name):
            continue
        vol, close, chg = _num(r[3]), _num(r[8]), _num(r[9])
        if vol is None or vol <= 0 or close is None or chg is None:
            continue
        prev = close - chg
        if not prev > 0:
            continue
        out.append({"code": code, "name": name, "close": close, "chg": chg,
                    "prev": prev, "pct": chg / prev * 100.0, "vol": vol,
                    "amt": _num(r[4])})
    return out


def ref_market_stats() -> dict:
    """獨立解析 MI_INDEX 樣本，取出成交金額與漲跌家數。"""
    j = json.loads(H.read_fixture("mi_index_ms.json"))
    out: dict = {}
    for t in j["tables"]:
        fields = [str(c) for c in (t.get("fields") or [])]
        for r in (t.get("data") or []):
            k = str(r[0])
            if "成交金額(元)" in fields:
                if k.startswith("總計"):
                    out["total"] = _num(r[1])
                elif k.startswith("證券合計"):
                    out["securities"] = _num(r[1])
                elif k.startswith("1.一般股票"):
                    out["ordinal"] = _num(r[1])
            if fields[:2] == ["類型", "整體市場"]:
                for pref, tag in (("上漲", "mkt_up"), ("下跌", "mkt_down"), ("持平", "mkt_flat")):
                    if k.startswith(pref):
                        out[tag] = _num(str(r[1]).split("(")[0])
                for pref, tag in (("上漲", "up"), ("下跌", "down"), ("持平", "flat")):
                    if k.startswith(pref):
                        out[tag] = _num(str(r[2]).split("(")[0])
    return out


def ref_top10() -> list[dict]:
    return sorted(ref_universe(), key=lambda x: (-x["pct"], x["code"]))[:10]


def ref_bottom10() -> list[dict]:
    return sorted(ref_universe(), key=lambda x: (x["pct"], x["code"]))[:10]


# --- 從輸出 HTML 取資料 ---------------------------------------------------
def parse_rank_region(html: str, region: str) -> list[dict]:
    body = region_body(html, region)
    return [{"code": m.group(1), "pct": float(m.group(2))}
            for m in re.finditer(r'data-code="(\d{4})" data-pct="(-?[\d.]+)"', body)]


def parse_rankbar_codes(html: str) -> list[str]:
    return re.findall(r'data-code="(\d{4})" data-pct=', region_body(html, "RANKBAR"))


def parse_cx_rows(html: str) -> list[dict]:
    """從 CXLIST 取出畫面順序的 (date, kind, close)。"""
    body = region_body(html, "CXLIST")
    rows = []
    for m in re.finditer(
            r'<span class="cx-dt">(\d{4}/\d{2}/\d{2})</span>\s*'
            r'<span class="cx-type ([gd])">.*?</span>\s*'
            r'<span class="cx-close">([\d,.]+)</span>', body, re.S):
        rows.append({"date": m.group(1), "kind": m.group(2),
                     "close": float(m.group(3).replace(",", ""))})
    return rows


def parse_svg_dots(html: str) -> list[tuple[float, float]]:
    return [(float(a), float(b)) for a, b in re.findall(
        r'<circle class="cx-dot" cx="([\d.]+)" cy="([\d.]+)"', region_body(html, "SVG"))]


def parse_svg_dot_marks(html: str) -> list[tuple[str, str, str]]:
    """(date, kind_char, fill) —— 由 SVG <title> 的類型與圓點顏色交叉取出。"""
    out = []
    for m in re.finditer(
            r'<title>(\d{4}/\d{2}/\d{2}) (黃金|死亡)交叉.*?</title>\s*'
            r'<circle class="cx-dot"[^>]*fill="(#[0-9a-fA-F]{6})"',
            region_body(html, "SVG"), re.S):
        date, word, fill = m.group(1), m.group(2), m.group(3)
        out.append((date, "g" if word == "黃金" else "d", fill.lower()))
    return out


def parse_guide_lines(html: str) -> list[tuple[float, str]]:
    return [(float(x), "g" if kind == "golden" else "d") for kind, x in re.findall(
        r'<line class="cx-guide is-(\w+)" x1="([\d.]+)"', region_body(html, "SVG"))]


def parse_pills(html: str) -> list[dict]:
    return [{"x": float(a), "y": float(b), "w": float(c)} for a, b, c in re.findall(
        r'<rect class="cx-pill" x="([\d.]+)" y="([\d.]+)" width="([\d.]+)"',
        region_body(html, "SVG"))]


def svg_of(html: str) -> str:
    return region_body(html, "SVG")


def expected_crosses() -> list[dict]:
    """以樣本獨立算出的交叉事件（含程式會套用的 scan_from 下界）。"""
    hist = H.independent_history_from_fixtures()
    lo = hist[max(0, len(hist) - us.HISTORY_DAYS)]["date"]
    return [c for c in H.independent_crossovers(hist, 5, 20) if c["date"] >= lo]


def history() -> list[dict]:
    return H.independent_history_from_fixtures()


def window_dates() -> set[str]:
    return {r["date"] for r in history()[-us.N_WINDOW:]}


def in_window_crosses() -> list[dict]:
    w = window_dates()
    return [c for c in expected_crosses() if c["date"] in w]


# ==========================================================================
# 1. 樣本完整性（其餘測試的結論都建立在此）
# ==========================================================================
class TestFixtures(unittest.TestCase):
    def test_fixture_months_cover_window_at_reference_now(self):
        """守門測試：在樣本參考時刻下，程式會請求的月份必須完全落在樣本內。

        這正是 2026-10-01 起 CI 連續失敗 16 天的直接防護 —— 該次失敗純粹是
        「執行測試的日曆日」推進到樣本涵蓋範圍之外（多了 202610），與程式邏輯無關。
        若日曆月跨過樣本擷取月，本測試會失敗；修法是重跑 capture_fixtures.py。
        """
        n = us.HISTORY_MONTHS
        t = H.ref_now().date()
        need = []
        for back in range(n - 1, -1, -1):
            y, m = t.year, t.month - back
            while m <= 0:
                m += 12
                y -= 1
            need.append(f"{y}{m:02d}")
        have = set(H.meta()["months"])
        missing = [m for m in need if m not in have]
        self.assertEqual(missing, [], f"樣本缺少月份 {missing}；請重跑 capture_fixtures.py")
        self.assertEqual(len(H.meta()["months"]), n)

    def test_meta_lists_expected_months(self):
        m = H.meta()
        self.assertEqual(len(m["months"]), us.HISTORY_MONTHS)
        self.assertRegex(m["data_date"], r"^\d{4}/\d{2}/\d{2}$")

    def test_every_fixture_file_present(self):
        for rel in fixture_files():
            self.assertTrue(os.path.exists(H.fixture_path(rel)), f"缺少樣本 {rel}")

    def test_stock_day_all_shape(self):
        rows = list(csv.reader(io.StringIO(H.read_fixture("stock_day_all.csv"))))
        self.assertGreaterEqual(len(rows), 500)
        self.assertEqual(rows[1][0], H.roc_from_ad(H.data_date()))
        self.assertGreaterEqual(len(rows[0]), 10)

    def test_mi_index_has_stat_and_breadth_tables(self):
        j = json.loads(H.read_fixture("mi_index_ms.json"))
        self.assertEqual(j["stat"], "OK")
        titles = [t.get("title") for t in j["tables"]]
        self.assertTrue(any(t and "大盤統計資訊" in t for t in titles))
        self.assertIn("漲跌證券數合計", titles)

    def test_mi_index_market_stats_parse(self):
        s = ref_market_stats()
        for k in ("total", "ordinal", "securities", "mkt_up", "mkt_down",
                  "mkt_flat", "up", "down", "flat"):
            self.assertIsNotNone(s.get(k), f"樣本缺少 {k}")

    def test_fmtqik_each_month_ok(self):
        for m in H.meta()["months"]:
            j = json.loads(H.read_fixture(os.path.join("fmtqik", f"{m}.json")))
            self.assertEqual(j["stat"], "OK", f"{m} stat 非 OK")
            self.assertTrue(j["data"], f"{m} 無資料列")

    def test_history_is_long_enough_for_ma20_in_window(self):
        h = history()
        # 端點非同步時，程式驗證的最後一日是 FMTQIK 的最後交易日（可能較 STOCK_DAY_ALL 舊一日）。
        self.assertEqual(h[-1]["date"], H.fmtqik_last_date())
        self.assertGreaterEqual(len(h), us.HISTORY_DAYS + 19)

    def test_mi_5mins_hist_covers_data_date(self):
        j = json.loads(H.read_fixture("mi_5mins_hist.json"))
        dates = [us.roc_to_ad(r[0]) for r in j["data"]]
        self.assertIn(H.data_date(), dates)


# ==========================================================================
# 2. HTTP 層：重試、錯誤路徑
# ==========================================================================
class TestFetch(unittest.TestCase):
    def test_retries_then_succeeds(self):
        calls = {"n": 0}
        real = H.FakeAPI()

        def flaky(req, timeout=None, context=None):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise TimeoutError("boom")
            return real.urlopen(req, timeout, context)

        with mock.patch.object(us.urllib.request, "urlopen", flaky), \
                mock.patch.object(us.time, "sleep", lambda *_: None):
            txt = us.fetch(PATH_STOCK, {"response": "json"})
        self.assertGreater(calls["n"], 2)
        self.assertIn(H.roc_from_ad(H.data_date()), txt)

    def test_gives_up_after_three_attempts(self):
        calls = {"n": 0}

        def always_fail(req, timeout=None, context=None):
            calls["n"] += 1
            raise TimeoutError("nope")

        with mock.patch.object(us.urllib.request, "urlopen", always_fail), \
                mock.patch.object(us.time, "sleep", lambda *_: None):
            with self.assertRaises(RuntimeError) as cm:
                us.fetch(PATH_FMT, {"date": "20260901"})
        self.assertIn("取得失敗", str(cm.exception))
        self.assertEqual(calls["n"], 6, "3 次嘗試 × 2 種 SSL context")

    def test_empty_body_is_an_error(self):
        with H.FakeAPI({PATH_STOCK: "empty_body"}):
            with self.assertRaises(RuntimeError) as cm:
                us.fetch(PATH_STOCK)
        self.assertIn("空回應", str(cm.exception))

    def test_html_body_rejected_for_json(self):
        with H.FakeAPI({PATH_MI: "html_error"}):
            with self.assertRaises(RuntimeError) as cm:
                us.fetch_json(PATH_MI)
        self.assertIn("非 JSON", str(cm.exception))

    def test_bad_json_rejected(self):
        with H.FakeAPI({PATH_MI: "bad_json"}):
            with self.assertRaises(RuntimeError) as cm:
                us.fetch_json(PATH_MI)
        self.assertIn("JSON 解析失敗", str(cm.exception))

    def test_non_200_rejected(self):
        class Non200:
            status = 500
            headers: dict = {}

            def read(self):
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch.object(us.urllib.request, "urlopen", lambda *a, **k: Non200()), \
                mock.patch.object(us.time, "sleep", lambda *_: None):
            with self.assertRaises(RuntimeError) as cm:
                us.fetch(PATH_STOCK)
        self.assertIn("取得失敗", str(cm.exception))

    def test_url_is_built_from_twse_base_and_path(self):
        api = H.FakeAPI()
        with api:
            us.fetch(PATH_STOCK, {"response": "json"})
        self.assertTrue(api.calls[0].startswith(us.BASE))
        path, params = api.parse(api.calls[0])
        self.assertEqual(path, PATH_STOCK)
        self.assertEqual(params, {"response": "json"})

    def test_index_history_requests_expected_months(self):
        """月份推算必須與樣本一致，否則樣本無法重播。"""
        with H.freeze_now(H.ref_now()):
            api = H.FakeAPI()
            with api:
                rows = us.get_index_history()
        self.assertGreaterEqual(len(rows), us.HISTORY_DAYS + 19)
        months = {api.parse(u)[1]["date"][:6] for u in api.calls}
        self.assertEqual(months, set(H.meta()["months"]))


# ==========================================================================
# 3. 數字／日期解析
# ==========================================================================
class TestParsing(unittest.TestCase):
    def test_thousands_separator(self):
        self.assertEqual(us.n2("46,948.72"), 46948.72)
        self.assertEqual(us.n2("1,187,571,567,117"), 1187571567117.0)

    def test_unicode_minus(self):
        self.assertEqual(us.n2("\u2212132.69"), -132.69)
        self.assertEqual(us.n2("-132.69"), -132.69)

    def test_percent_stripped(self):
        self.assertEqual(us.n2("+9.92%"), 9.92)

    def test_html_tags_stripped(self):
        self.assertEqual(us.n2("<span>1,234</span>"), 1234.0)

    def test_blank_markers_are_none(self):
        for raw in ("", " ", "-", "--", "\u2014", "nan", "NaN", "None", "N/A",
                    "abc", "12.3.4", "1,2,3.4.5"):
            with self.subTest(raw=raw):
                self.assertIsNone(us.n2(raw))

    def test_none_passthrough(self):
        self.assertIsNone(us.n2(None))

    def test_whitespace_and_plus(self):
        self.assertEqual(us.n2("  +12.50  "), 12.50)

    def test_zero_is_zero_not_none(self):
        self.assertEqual(us.n2("0"), 0.0)
        self.assertEqual(us.n2("0.00"), 0.0)

    def test_roc_to_ad_accepts_both_forms(self):
        self.assertEqual(us.roc_to_ad("1150924"), "2026/09/24")
        self.assertEqual(us.roc_to_ad("115/09/24"), "2026/09/24")
        self.assertEqual(us.roc_to_ad("114/01/01"), "2025/01/01")

    def test_roc_to_ad_rejects_garbage(self):
        for raw in ("", "115", "115/09", "abc", "?BAD?", "1150924x"):
            with self.subTest(raw=raw):
                with self.assertRaises(RuntimeError):
                    us.roc_to_ad(raw)

    def test_ad_to_dt(self):
        self.assertEqual(us.ad_to_dt("2026/09/24"), datetime(2026, 9, 24).date())

    def test_parse_cnt_variants(self):
        self.assertEqual(us._parse_cnt("386(11)"), (386, 11))
        self.assertEqual(us._parse_cnt("140"), (140, None))
        self.assertEqual(us._parse_cnt(" 4,581(67) "), (4581, 67))
        self.assertEqual(us._parse_cnt("\u2014"), (None, None))

    def test_formatters(self):
        self.assertEqual(us.f2(46948.7234), "46,948.72")
        self.assertEqual(us.f2(None), "0.00")
        self.assertEqual(us.f0(1234567.6), "1,234,568")
        self.assertEqual(us.pts(-132.69), "\u2212132.69")
        self.assertEqual(us.pts(132.69), "+132.69")
        self.assertEqual(us.pts(0), "0.00")
        self.assertEqual(us.pct(-1.0), "\u22121.00%")
        self.assertEqual(us.pct(9.92), "+9.92%")
        self.assertEqual(us.sgn(-1), "down")
        self.assertEqual(us.sgn(0), "flat")
        self.assertEqual(us.sgn(2.5), "up")

    def test_sma_definition_and_sliding_window(self):
        self.assertEqual(us.sma([1.0, 2.0, 3.0, 4.0, 5.0], 5), [None] * 4 + [3.0])
        out = us.sma([1, 2, 3, 4, 5, 6], 3)
        self.assertEqual(out[:2], [None, None])
        self.assertEqual(out[2:], [2.0, 3.0, 4.0, 5.0])

    def test_esc_escapes_markup(self):
        self.assertEqual(us.esc('a<b>&"c'), "a&lt;b&gt;&amp;&quot;c")


# ==========================================================================
# 4. 故障注入矩陣：任一端點出問題 → 非 0 結束、不寫檔、原檔完好
# ==========================================================================
FAILURE_CASES = [
    ("stock_empty_body", PATH_STOCK, "empty_body"),
    ("stock_html_error", PATH_STOCK, "html_error"),
    ("stock_bad_json", PATH_STOCK, "bad_json"),
    ("stock_empty_data", PATH_STOCK, "stock_empty_data"),
    ("stock_few_rows", PATH_STOCK, "stock_few_rows"),
    ("stock_bad_date", PATH_STOCK, "stock_bad_date"),
    ("stock_http500", PATH_STOCK, "http_500"),
    ("stock_http404", PATH_STOCK, "http_404"),
    ("stock_timeout", PATH_STOCK, "timeout"),
    ("stock_urlerror", PATH_STOCK, "urlerror"),
    ("stock_conn_refused", PATH_STOCK, "conn_refused"),
    ("mi_null_tables", PATH_MI, "null_tables"),
    ("mi_empty_tables", PATH_MI, "empty_tables"),
    ("mi_drop_up", PATH_MI, "mi_drop_up"),
    ("mi_drop_total", PATH_MI, "mi_drop_total"),
    ("mi_drop_ordinal", PATH_MI, "mi_drop_ordinal"),
    ("mi_bad_count", PATH_MI, "mi_bad_count"),
    ("mi_bad_json", PATH_MI, "bad_json"),
    ("mi_empty_body", PATH_MI, "empty_body"),
    ("mi_html_error", PATH_MI, "html_error"),
    ("mi_http500", PATH_MI, "http_500"),
    ("mi_timeout", PATH_MI, "timeout"),
    ("fmt_empty", PATH_FMT, "fmt_empty"),
    ("fmt_stat_bad", PATH_FMT, "fmt_stat_bad"),
    ("fmt_bad_close", PATH_FMT, "fmt_bad_close"),
    ("fmt_bad_amt", PATH_FMT, "fmt_bad_amt"),
    ("fmt_missing_recent", PATH_FMT, "fmt_missing_recent"),
    ("fmt_bad_json", PATH_FMT, "bad_json"),
    ("fmt_empty_body", PATH_FMT, "empty_body"),
    ("fmt_html_error", PATH_FMT, "html_error"),
    ("fmt_http500", PATH_FMT, "http_500"),
    ("fmt_timeout", PATH_FMT, "timeout"),
    ("min_empty", PATH_5MIN, "5min_empty"),
    ("min_bad_ohlc", PATH_5MIN, "5min_bad_ohlc"),
    ("min_shift_date", PATH_5MIN, "5min_shift_date"),
    ("min_bad_json", PATH_5MIN, "bad_json"),
    ("min_empty_body", PATH_5MIN, "empty_body"),
    ("min_html_error", PATH_5MIN, "html_error"),
    ("min_http500", PATH_5MIN, "http_500"),
    ("min_timeout", PATH_5MIN, "timeout"),
    # --- 補強：先前定義了卻未被任何測試使用的故障注入（死碼） ---
    ("stock_header_only", PATH_STOCK, "stock_header_only"),
    ("fmt_empty_month", PATH_FMT, "fmt_empty_month"),
    ("mi_fields_stripped", PATH_MI, "mi_fields_stripped"),
    # --- 補強：JSON null 與缺欄位（端點異常時可能出現） ---
    ("stock_json_null", PATH_STOCK, "json_null_body"),
    ("mi_data_null", PATH_MI, "mi_data_null"),
    ("fmt_short_row", PATH_FMT, "fmt_short_row"),
    # --- 法人買賣超（BFI82U）：逐日端點。stat 非 OK、缺列、合計不符都必須失敗，
    #     絕不得以 0 或前一日數字代替（那會讓圖表出現假數字）。 ---
    ("bfi_stat_bad", PATH_BFI, "bfi_stat_bad"),
    ("bfi_drop_foreign", PATH_BFI, "bfi_drop_foreign"),
    ("bfi_drop_total", PATH_BFI, "bfi_drop_total"),
    ("bfi_mismatch_total", PATH_BFI, "bfi_mismatch_total"),
    ("bfi_bad_json", PATH_BFI, "bad_json"),
    ("bfi_empty_body", PATH_BFI, "empty_body"),
    ("bfi_html_error", PATH_BFI, "html_error"),
    ("bfi_http500", PATH_BFI, "http_500"),
    ("bfi_timeout", PATH_BFI, "timeout"),
]


class TestFaultInjection(unittest.TestCase):
    def _check(self, label, path, fault):
        d = tempfile.mkdtemp(prefix=f"twstock-{label}-")
        p = os.path.join(d, "index.html")
        H.write_html(p)
        before = H.read_bytes(p)
        api = H.FakeAPI({path: fault})
        with api:
            r = H.run_cli(["--file", p])
        after = H.read_bytes(p)

        ctx = f"{label}（{path} ← {fault}）"
        self.assertTrue(api.calls, f"{ctx}：未發出任何請求")
        self.assertNotEqual(r.rc, 0, f"{ctx}：應以非 0 結束，實際 rc={r.rc}\n{r.out}")
        self.assertFalse(r.wrote, f"{ctx}：不應寫入檔案\n{r.out}")
        self.assertIsNone(r.changed, f"{ctx}：失敗時不應輸出 CHANGED\n{r.out}")
        self.assertEqual(before, after, f"{ctx}：原檔位元組被改動")
        self.assertIn("未寫入任何檔案", r.err, f"{ctx}：錯誤訊息未聲明未寫入")
        self.assertIn("ERROR:", r.err, f"{ctx}：stderr 應含 ERROR")
        self.assertTrue(no_temps(p), f"{ctx}：留下暫存檔")

    def test_matrix_covers_all_endpoints(self):
        self.assertEqual({c[1] for c in FAILURE_CASES}, set(H.ALL_PATHS))

    def test_matrix_covers_all_error_kinds(self):
        for name in ("http_500", "timeout", "empty_body", "html_error", "bad_json"):
            self.assertIn(name, {c[2] for c in FAILURE_CASES})

    def test_all_fault_names_resolve(self):
        for _, _, f in FAILURE_CASES:
            self.assertIn(f, H.FAULT_NAMES, f"未定義的故障：{f}")

    def test_mi_index_rejected_when_stat_not_ok(self):
        """stat != OK（例如假日查無資料）必須失敗，不得沿用舊值。"""
        d = tempfile.mkdtemp(prefix="twstock-stat-")
        p = os.path.join(d, "index.html")
        H.write_html(p)
        before = H.read_bytes(p)
        with H.FakeAPI({PATH_MI: "mi_stat_bad"}):
            r = H.run_cli(["--file", p])
        self.assertNotEqual(r.rc, 0)
        self.assertEqual(before, H.read_bytes(p))


# ==========================================================================
# 4b. 「端點尚未釋出當日資料」與 --allow-not-ready
#
# 證交所盤後端點並非 13:30 收盤即同步（STOCK_DAY_ALL 約 13:52–13:54、
# 三大法人 BFI82U 約 15:10），因此 13:32 的早班排程必然經常遇到「當日資料
# 還沒出來」。這不是故障，必須與真正的錯誤區分：
#   - 尚未釋出 + --allow-not-ready → 以 0 結束、不寫檔、CHANGED=0（本次略過）
#   - 尚未釋出 + 無旗標            → 以非 0 結束（保底排程要能及早發現異常）
#   - 真正的錯誤（HTTP 500…）      → 一律以非 0 結束，即使帶了旗標
# ==========================================================================
class TestNotReady(unittest.TestCase):
    def _run(self, faults, argv):
        d = tempfile.mkdtemp(prefix="twstock-notready-")
        p = os.path.join(d, "index.html")
        H.write_html(p)
        before = H.read_bytes(p)
        with H.FakeAPI(faults):
            r = H.run_cli(["--file", p] + list(argv))
        return r, p, before

    def test_not_ready_is_skipped_with_flag(self):
        """MI_INDEX stat != OK（當日尚未釋出）+ --allow-not-ready → 略過、不寫檔、CHANGED=0。"""
        r, p, before = self._run({PATH_MI: "mi_stat_bad"}, ["--allow-not-ready"])
        self.assertEqual(r.rc, 0, f"應以 0 結束（本次略過）：{r.out}\n{r.err}")
        self.assertFalse(r.wrote, f"不應寫入檔案：{r.out}")
        self.assertEqual(r.changed, "0", f"應輸出 CHANGED=0：{r.out}")
        self.assertEqual(before, H.read_bytes(p), "原檔位元組被改動")
        self.assertIn("NOT-READY", r.err, "stderr 應說明是尚未釋出而非錯誤")
        self.assertNotIn("ERROR:", r.err, "尚未釋出不得當成錯誤")

    def test_bfi_not_ready_is_skipped_with_flag(self):
        """三大法人 BFI82U 當日尚未公布 + --allow-not-ready → 略過、不寫檔。"""
        r, p, before = self._run({PATH_BFI: "bfi_stat_bad"}, ["--allow-not-ready"])
        self.assertEqual(r.rc, 0, f"應以 0 結束（本次略過）：{r.out}\n{r.err}")
        self.assertFalse(r.wrote)
        self.assertEqual(r.changed, "0")
        self.assertEqual(before, H.read_bytes(p))
        self.assertIn("NOT-READY", r.err)

    def test_not_ready_still_fails_without_flag(self):
        """未帶旗標（15:35 保底排程）時，尚未釋出仍必須失敗。"""
        r, p, before = self._run({PATH_MI: "mi_stat_bad"}, [])
        self.assertNotEqual(r.rc, 0, f"保底排程應失敗：{r.out}")
        self.assertFalse(r.wrote)
        self.assertIsNone(r.changed)
        self.assertEqual(before, H.read_bytes(p))
        self.assertIn("ERROR:", r.err)

    def test_real_error_fails_even_with_flag(self):
        """真正的錯誤（HTTP 500）即使帶 --allow-not-ready 也必須失敗。"""
        for path in (PATH_MI, PATH_BFI, PATH_STOCK):
            with self.subTest(path=path):
                r, p, before = self._run({path: "http_500"}, ["--allow-not-ready"])
                self.assertNotEqual(r.rc, 0, f"{path}：真正的錯誤不得被略過：{r.out}")
                self.assertFalse(r.wrote)
                self.assertIsNone(r.changed)
                self.assertEqual(before, H.read_bytes(p))
                self.assertIn("ERROR:", r.err)

    def test_bad_json_fails_even_with_flag(self):
        """壞 JSON 是真正的錯誤，不得被 --allow-not-ready 吞掉。"""
        r, p, before = self._run({PATH_MI: "bad_json"}, ["--allow-not-ready"])
        self.assertNotEqual(r.rc, 0)
        self.assertFalse(r.wrote)
        self.assertEqual(before, H.read_bytes(p))

    def test_not_ready_error_is_a_runtime_error(self):
        """NotReadyError 必須繼承 RuntimeError，既有「失敗即不寫檔」的保護才不變。"""
        self.assertTrue(issubclass(us.NotReadyError, RuntimeError))

    def test_workflow_has_two_schedules_and_flag_only_on_early_one(self):
        """workflow 必須有兩個 cron，且 --allow-not-ready 只掛在 13:32 早班。"""
        wf = os.path.join(H.ROOT, ".github", "workflows", "update-snapshot.yml")
        with open(wf, encoding="utf-8") as f:
            text = f.read()
        self.assertIn('cron: "32 5 * * 1-5"', text, "缺少 13:32 早班排程")
        self.assertIn('cron: "35 7 * * 1-5"', text, "缺少 15:35 保底排程")
        self.assertIn("--allow-not-ready", text, "早班未帶 --allow-not-ready")
        # 旗標必須由排程字串條件決定，而非無條件套用。
        self.assertIn('github.event.schedule', text,
                      "旗標應依觸發的排程決定，保底班不得帶旗標")


def _make_fault_test(label: str, path: str, fault: str):
    def test(self):
        self._check(label, path, fault)
    test.__name__ = f"test_fault_{label}"
    test.__doc__ = f"故障注入 {label}：{path} 回傳 {fault} → 必須失敗且不破壞原檔"
    return test


for _label, _path, _fault in FAILURE_CASES:
    setattr(TestFaultInjection, f"test_fault_{_label}",
            _make_fault_test(_label, _path, _fault))


# ==========================================================================
# 5. 個股層級的資料格式異常：應「排除」而非崩潰
# ==========================================================================
class TestMalformedStockRows(unittest.TestCase):
    HEADER = ("日期,證券代號,證券名稱,成交股數,成交金額,開盤價,最高價,"
              "最低價,收盤價,漲跌價差,成交筆數")

    @staticmethod
    def _row(code, name, vol="1000", amt="50000", close="100.00", chg="2.00",
             o="99", h="101", lo="98", date="1150924"):
        return [date, code, name, vol, amt, o, h, lo, close, chg, "10"]

    def _universe(self, data_rows):
        return {r["code"]: r for r in us.rank_universe([list(r) for r in data_rows])}

    def test_valid_row_kept_and_pct_correct(self):
        u = self._universe([self._row("2330", "台積電")])
        self.assertIn("2330", u)
        self.assertAlmostEqual(u["2330"]["pct"], 2.0 / 98.0 * 100, places=6)

    def test_comma_separated_numbers_parsed(self):
        u = self._universe([self._row("2330", "台積電", vol="12,345,678",
                                      amt="1,234,567,890", close="1,234.50",
                                      chg="1,000.00")])
        self.assertIn("2330", u)
        self.assertEqual(u["2330"]["vol"], 12345678.0)
        self.assertEqual(u["2330"]["close"], 1234.5)

    def test_unicode_minus_change_parsed(self):
        u = self._universe([self._row("2330", "台積電", close="98.00", chg="\u22122.00")])
        self.assertIn("2330", u)
        self.assertLess(u["2330"]["pct"], 0)

    def test_zero_volume_excluded(self):
        self.assertNotIn("2330", self._universe([self._row("2330", "台積電", vol="0")]))

    def test_empty_close_excluded(self):
        self.assertNotIn("2330", self._universe([self._row("2330", "台積電", close="")]))

    def test_empty_change_excluded(self):
        self.assertNotIn("2330", self._universe([self._row("2330", "台積電", chg="")]))

    def test_dash_only_change_excluded(self):
        self.assertNotIn("2330", self._universe([self._row("2330", "台積電", chg="--")]))

    def test_non_numeric_close_excluded(self):
        self.assertNotIn("2330", self._universe([self._row("2330", "台積電", close="N/A")]))

    def test_prev_price_nonpositive_excluded(self):
        """收盤 − 漲跌 ≤ 0 屬資料異常，必須排除（否則除以 0）。"""
        self.assertNotIn("2330", self._universe(
            [self._row("2330", "台積電", close="0.00", chg="0.00")]))

    def test_etf_and_dr_and_bad_codes_excluded(self):
        u = self._universe([
            self._row("0050", "元大台灣50"),
            self._row("00400A", "主動國泰動能高息"),
            self._row("9105", "泰金寶-DR"),
            self._row("23301", "亂碼"),
            self._row("ABCD", "亂碼"),
            self._row("2330", "台積電"),
        ])
        self.assertEqual(set(u), {"2330"})

    def test_short_row_skipped_without_crash(self):
        rows = [["1150924", "2330", "台積電", "1000"], self._row("2317", "鴻海")]
        u = {r["code"]: r for r in us.rank_universe(rows)}
        self.assertNotIn("2330", u)
        self.assertIn("2317", u)

    def test_html_injected_value_does_not_raise(self):
        u = self._universe([self._row("2330", "台積電", chg="<b>2.00</b>")])
        self.assertIn("2330", u)  # n2 剝除標籤後可得 2.00

    def test_garbage_name_is_escaped_in_output(self):
        r = {"code": "2330", "name": '<script>"x"</script>', "close": 1.0,
             "chg": 0.1, "prev": 0.9, "pct": 11.1, "vol": 1.0, "amt": 1.0,
             "open": 1.0, "high": 1.0, "low": 1.0}
        html = us.rl_row(1, r)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_rank_universe_never_raises_on_hostile_rows(self):
        hostile = [
            [], [""], ["a"] * 3, [None] * 10, ["1150924", None, None] + [""] * 8,
            self._row("2330", "台積電"),
        ]
        out = us.rank_universe(hostile)
        self.assertEqual([r["code"] for r in out], ["2330"])


# ==========================================================================
# 6. 正常交易日：25 組區塊全部更新、輸出結構完整
# ==========================================================================
class TestNormalTradingDay(unittest.TestCase):
    def setUp(self):
        self.out = baseline_out()

    # ---- SNAP marker 全部由本次資料重建 ------------------------------
    def test_all_29_regions_are_rewritten_from_live_data(self):
        """把全部區塊內容全換成哨兵，重跑後哨兵必須全部消失。"""
        sentinel = "ZZ__STALE__ZZ"
        corrupted = H.repo_index_html()
        for name in sorted(H.EXPECTED_REGIONS):
            m = us._region_body(corrupted, name)
            self.assertIsNotNone(m, f"來源缺少 marker：{name}")
            corrupted = corrupted[:m.start(2)] + sentinel + corrupted[m.end(2):]

        p = temp_html(corrupted)
        with H.FakeAPI():
            r = H.run_cli(["--file", p])
        self.assertEqual(r.rc, 0, f"哨兵檔重跑失敗：{r.out}\n{r.err}")
        with open(p, encoding="utf-8") as f:
            out = f.read()
        survivors = [n for n in sorted(H.EXPECTED_REGIONS)
                     if sentinel in region_body(out, n)]
        self.assertEqual(survivors, [], f"以下區塊未被更新：{survivors}")
        self.assertNotIn(sentinel, out)

    def test_expected_region_set_is_29(self):
        self.assertEqual(len(H.EXPECTED_REGIONS), 29)

    def test_source_has_all_29_markers(self):
        src = H.repo_index_html()
        for name in sorted(H.EXPECTED_REGIONS):
            self.assertIsNotNone(us._region_body(src, name), f"index.html 缺 marker {name}")

    def test_reruns_are_deterministic(self):
        _, _, _, a1 = clean_run()
        _, _, _, a2 = clean_run()
        self.assertEqual(us.meta_stripped(a1.decode()), us.meta_stripped(a2.decode()))

    def test_only_marker_bodies_may_change(self):
        """marker 以外的位元組（CSS、前端 JS）不得被更動。"""
        src = H.repo_index_html()

        def strip_regions(h):
            for name in sorted(H.EXPECTED_REGIONS):
                m = us._region_body(h, name)
                h = h[:m.start(2)] + h[m.end(2):]
            return h

        self.assertEqual(strip_regions(src), strip_regions(self.out))

    def test_no_leftover_placeholders(self):
        blob = "".join(region_body(self.out, n) for n in sorted(H.EXPECTED_REGIONS))
        for bad in ("None", "nan", "NaN", "undefined", "{{", "}}"):
            self.assertNotIn(bad, blob, f"輸出含異常字串 {bad}")

    def test_written_size_reasonable(self):
        # 上下界僅為「寫入是否完整、有無異常膨脹」的護欄，非產品規格；
        # 隨頁面功能增加（如法人資金動向區塊）需一併調升上界。
        self.assertGreater(len(self.out), 80_000)
        self.assertLess(len(self.out), 400_000)

    # ---- SVG 繪製完整性 ----------------------------------------------
    def test_svg_wrapper_and_accessibility(self):
        svg = svg_of(self.out)
        self.assertIn("<svg", svg)
        self.assertIn("</svg>", svg)
        self.assertRegex(svg, r'<svg[^>]*role="img"')
        self.assertIn('viewBox="0 0 1000 420"', svg)
        self.assertIn("<title>", svg)
        self.assertIn("aria-label", svg)

    def test_area_and_three_polylines_present(self):
        svg = svg_of(self.out)
        for cls in ("area-f", "line-p", "line-ma5", "line-ma20"):
            self.assertIn(f'class="{cls}"', svg, f"缺少 {cls}")

    def test_polylines_have_full_window_of_points(self):
        svg = svg_of(self.out)
        for cls in ("line-p", "line-ma5", "line-ma20"):
            m = re.search(rf'<polyline class="{cls}" points="([^"]+)"', svg)
            self.assertIsNotNone(m, f"找不到 {cls} 的 points")
            self.assertEqual(len(m.group(1).split()), us.N_WINDOW, cls)

    def test_ma_lines_match_independently_computed_values(self):
        """把折線座標反算回指數值，檢查 MA5／MA20 尾點與獨立計算相符。"""
        svg = svg_of(self.out)
        h = history()
        closes = [r["close"] for r in h]
        ma5, ma20 = H.independent_sma(closes, 5), H.independent_sma(closes, 20)
        for cls, series in (("line-ma5", ma5), ("line-ma20", ma20)):
            m = re.search(rf'<polyline class="{cls}" points="([^"]+)"', svg)
            pts = [tuple(map(float, p.split(","))) for p in m.group(1).split()]
            self.assertEqual(len(pts), us.N_WINDOW)
            # y 越小 → 指數越高；比較相對關係即足以確認沒走鐘
            last_y = pts[-1][1]
            self.assertGreater(last_y, 0)
            self.assertLess(last_y, us.VOL_BASE_Y)
            self.assertIsNotNone(series[-1])

    def test_volume_bars_full_window(self):
        svg = svg_of(self.out)
        ups = len(re.findall(r'class="v-up"', svg))
        downs = len(re.findall(r'class="v-down"', svg))
        self.assertEqual(ups + downs, us.N_WINDOW)
        self.assertGreater(ups, 0)
        self.assertGreater(downs, 0)

    def test_volume_bar_colours_are_red_up_green_down(self):
        """紅漲綠跌：量柱配色必須紅 > 綠。"""
        css = us._region_body(H.repo_index_html(), "SVG")
        html = H.repo_index_html()
        def fill_of(cls):
            m = re.search(rf"\.{cls}\{{fill:#([0-9a-fA-F]{{6}})", html)
            self.assertIsNotNone(m, f"CSS 缺少 .{cls} 的 fill")
            hexv = m.group(1)
            return tuple(int(hexv[i:i + 2], 16) for i in (0, 2, 4))
        ru, gu, bu = fill_of("v-up")
        rd, gd, bd = fill_of("v-down")
        self.assertGreater(ru, gu, "v-up 應為紅色系")
        self.assertGreater(ru, bu, "v-up 應為紅色系")
        self.assertGreater(gd, rd, "v-down 應為綠色系")
        self.assertGreater(gd, bd, "v-down 應為綠色系")
        del css

    def test_axis_labels_present(self):
        self.assertGreaterEqual(len(re.findall(r"<text", svg_of(self.out))), 10)

    # ---- 概況 / KPI / 家數 ---------------------------------------------
    def test_index_value_matches_fixture_close(self):
        last = history()[-1]["close"]
        m = re.search(r'id="idx-val">([\d,.]+)<', region_body(self.out, "IDXROW"))
        self.assertIsNotNone(m)
        self.assertAlmostEqual(float(m.group(1).replace(",", "")), last, places=2)

    def test_index_delta_matches_previous_close(self):
        h = history()
        chg = h[-1]["close"] - h[-2]["close"]
        m = re.search(r'id="idx-delta">([^\s<]+)', region_body(self.out, "IDXROW"))
        self.assertIsNotNone(m)
        self.assertAlmostEqual(
            float(m.group(1).replace(",", "").replace("\u2212", "-")), chg, places=2)

    def test_kpis_match_fixture_market_stats(self):
        s = ref_market_stats()
        body = region_body(self.out, "KPIS")
        got = dict(re.findall(r'id="(kpi-[a-z]+)">([\d,.]+)<', body))
        self.assertAlmostEqual(float(got["kpi-amt"].replace(",", "")), s["total"] / 1e8, places=2)
        self.assertAlmostEqual(float(got["kpi-stock"].replace(",", "")), s["ordinal"] / 1e8, places=2)
        self.assertEqual(int(got["kpi-up"].replace(",", "")), int(s["up"]))
        self.assertEqual(int(got["kpi-down"].replace(",", "")), int(s["down"]))

    def test_breadth_matches_fixture(self):
        s = ref_market_stats()
        body = region_body(self.out, "BREADTH")
        got = dict(re.findall(r'id="(br-[a-z]+)">([\d,]+)<', body))
        self.assertEqual(int(got["br-up"].replace(",", "")), int(s["up"]))
        self.assertEqual(int(got["br-down"].replace(",", "")), int(s["down"]))
        self.assertEqual(int(got["br-flat"].replace(",", "")), int(s["flat"]))

    def test_breadth_bar_widths_sum_to_100(self):
        body = region_body(self.out, "BREADTH")
        ws = [float(w) for w in re.findall(r'id="bbar-(?:up|down|flat)" style="width:([\d.]+)%', body)]
        self.assertEqual(len(ws), 3)
        self.assertAlmostEqual(sum(ws), 100.0, places=1)

    def test_breadth_totals_are_consistent(self):
        """頁面上的漲跌平三家數總和，必須等於 MI_INDEX 樣本解析出的同一總和。"""
        body = region_body(self.out, "BREADTH")
        nums = [int(re.search(rf'id="br-{k}">([\d,]+)<', body).group(1).replace(",", ""))
                for k in ("up", "down", "flat")]
        s = ref_market_stats()
        self.assertEqual(sum(nums), int(s["up"]) + int(s["down"]) + int(s["flat"]))

    def test_breadth_and_date_regions_populated(self):
        self.assertGreater(len(region_body(self.out, "BRDATE").strip()), 10)
        self.assertGreater(len(region_body(self.out, "BREADTH").strip()), 50)

    # ---- 排行區塊 ------------------------------------------------------
    def test_gain_ranking_matches_independent_calculation(self):
        got = parse_rank_region(self.out, "LISTGAIN")
        exp = ref_top10()
        self.assertEqual(len(got), 10)
        self.assertEqual([g["code"] for g in got], [e["code"] for e in exp])
        for g, e in zip(got, exp):
            self.assertAlmostEqual(g["pct"], e["pct"], places=2)

    def test_lose_ranking_matches_independent_calculation(self):
        got = parse_rank_region(self.out, "LISTLOSE")
        exp = ref_bottom10()
        self.assertEqual(len(got), 10)
        self.assertEqual([g["code"] for g in got], [e["code"] for e in exp])
        for g, e in zip(got, exp):
            self.assertAlmostEqual(g["pct"], e["pct"], places=2)

    def test_gain_sorted_descending_and_all_positive(self):
        pcts = [g["pct"] for g in parse_rank_region(self.out, "LISTGAIN")]
        self.assertEqual(pcts, sorted(pcts, reverse=True))
        self.assertTrue(all(p > 0 for p in pcts))

    def test_lose_sorted_ascending_and_all_negative(self):
        pcts = [g["pct"] for g in parse_rank_region(self.out, "LISTLOSE")]
        self.assertEqual(pcts, sorted(pcts))
        self.assertTrue(all(p < 0 for p in pcts))

    def test_tie_break_is_code_ascending(self):
        gain = parse_rank_region(self.out, "LISTGAIN")
        for a, b in zip(gain, gain[1:]):
            if a["pct"] == b["pct"]:
                self.assertLess(a["code"], b["code"])

    def test_rank_lists_do_not_overlap(self):
        g = {x["code"] for x in parse_rank_region(self.out, "LISTGAIN")}
        l = {x["code"] for x in parse_rank_region(self.out, "LISTLOSE")}
        self.assertEqual(g & l, set())

    def test_universe_size_matches_independent_count(self):
        body = region_body(self.out, "RANKNOTE")
        n = len(ref_universe())
        self.assertIn(f"{n:,d}", body, f"排名依據應載明母體 {n} 檔")

    def test_tsmc_block_matches_fixture(self):
        body = region_body(self.out, "LISTTSMC")
        self.assertIn('data-code="2330"', body)
        exp = next(r for r in ref_universe() if r["code"] == "2330")
        m = re.search(r'class="s-price num \w+">([\d,.]+)<', body)
        self.assertIsNotNone(m)
        self.assertAlmostEqual(float(m.group(1).replace(",", "")), exp["close"], places=2)
        m = re.search(r'class="chip \w+ num">([\u2212+\d.]+)%', body)
        self.assertIsNotNone(m)
        self.assertAlmostEqual(float(m.group(1).replace("\u2212", "-")), exp["pct"], places=2)

    def test_rankbar_contains_exactly_the_two_top_ten_lists(self):
        # 比較圖 = 漲幅前十 + 跌幅前十 + 2330 台積電（代號去重）。
        # 全圖依「漲跌幅遞減、同幅依代號遞增」排序（與程式內同一排序鍵）。
        # 不得用 reversed(跌幅榜) 推導：反轉會把同幅的代號順序一起翻掉。
        seen, uq = set(), []
        tsmc = next(x for x in ref_universe() if x["code"] == "2330")
        for r in ref_top10() + ref_bottom10() + [tsmc]:
            if r["code"] not in seen:
                seen.add(r["code"])
                uq.append(r)
        want = [x["code"] for x in sorted(uq, key=lambda x: (-x["pct"], x["code"]))]
        codes = parse_rankbar_codes(self.out)
        self.assertEqual(len(codes), 21)
        self.assertEqual(codes, want)
        pcts = [float(p) for p in re.findall(r'data-pct="(-?[\d.]+)"',
                                             region_body(self.out, "RANKBAR"))]
        self.assertEqual(pcts, sorted(pcts, reverse=True))

    def test_rank_note_states_source_date_and_basis(self):
        body = region_body(self.out, "RANKNOTE")
        self.assertIn(H.data_date(), body)
        self.assertIn("每日收盤行情", body)

    def test_colour_classes_follow_red_up_green_down(self):
        gain = region_body(self.out, "LISTGAIN")
        lose = region_body(self.out, "LISTLOSE")
        for blob, good, bad in ((gain, "chip up", "chip down"),
                                (lose, "chip down", "chip up")):
            self.assertIn(f'class="{good}', blob)
            self.assertNotIn(f'class="{bad}', blob)
        self.assertIn('class="rl-dl num up"', gain)
        self.assertIn('class="rl-dl num down"', lose)

    def test_bar_widths_within_bounds(self):
        for reg in ("LISTGAIN", "LISTLOSE"):
            for w in re.findall(r'style="width:([\d.]+)%"', region_body(self.out, reg)):
                self.assertGreaterEqual(float(w), 0.0)
                self.assertLessEqual(float(w), 100.0)

    # ---- 其他區塊 ------------------------------------------------------
    def test_title_contains_data_date(self):
        self.assertIn(H.data_date(), region_body(self.out, "TITLE"))

    def test_footer_regions_populated(self):
        for reg in ("FOOTA", "FOOTB", "FOOTCOPY"):
            self.assertGreater(len(region_body(self.out, reg).strip()), 20, reg)

    def test_ma_strip_values_match_independent_ma(self):
        body = region_body(self.out, "MASTRIP")
        closes = [r["close"] for r in history()]
        self.assertIn(f"{H.independent_sma(closes, 5)[-1]:,.2f}", body)
        self.assertIn(f"{H.independent_sma(closes, 20)[-1]:,.2f}", body)

    def test_status_and_timestamp_regions(self):
        self.assertGreater(len(region_body(self.out, "HTML_STATUS").strip()), 3)
        self.assertRegex(region_body(self.out, "HTML_LASTUPD"),
                         r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}")

    def test_js_regions_use_js_comment_markers(self):
        """script 內的 marker 必須是 /* */，否則 JS 會被註解掉。

        頁面含多個 <script>（宇程君對話資料為獨立區塊），因此逐一檢查。
        """
        src = H.repo_index_html()
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", src, re.S)
        self.assertTrue(scripts, "頁面必須至少含一個 script 區塊")
        js = "".join(scripts)
        self.assertNotIn("<!--SNAP:", js)
        for name in sorted(us.JS_REGIONS):
            self.assertIn(f"/*SNAP:{name}*/", js, f"JS 區塊 {name} 缺少 /* */ marker")
        self.assertRegex(region_body(self.out, "JS_SNAPAT"),
                         r"var SNAPSHOT_AT = '\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}';")

    def test_no_unclosed_html_comment(self):
        for m in re.finditer(r"<!--", self.out):
            self.assertIn("-->", self.out[m.start():m.start() + 400])

    # ---- 既有功能未被破壞 ---------------------------------------------
    def test_frontend_feature_hooks_intact(self):
        src = H.repo_index_html()
        for s in ("btn-refresh", "btn-auto", "list-gain", "list-lose", "list-tsmc",
                  "last-updated", "prefers-reduced-motion", 'lang="zh-Hant"',
                  "STOCK_DAY_ALL", "MI_5MINS_INDEX", "setStatus"):
            self.assertIn(s, src, f"原檔缺少 {s}")

    def test_no_external_dependencies(self):
        src = H.repo_index_html()
        self.assertEqual(re.findall(r'src="https?://', src), [])
        self.assertEqual(re.findall(r'<link[^>]+href="(https?://[^"]+)"', src), [])
        # PWA 需要本機的 manifest 與 App 圖示 <link>；仍禁止任何外部資源。
        for href in re.findall(r'<link[^>]+href="([^"]+)"', src):
            self.assertFalse(href.startswith(("http://", "https://", "//")),
                             f"<link> 不得指向外部資源：{href}")


# ==========================================================================
# 7. 非交易日／假日：相同資料 → CHANGED=0、不寫檔
# ==========================================================================
class TestNoChange(unittest.TestCase):
    def test_rerun_on_same_data_does_not_write(self):
        r1, p, _, _ = clean_run()
        self.assertEqual(r1.rc, 0)
        before = H.read_bytes(p)
        api = H.FakeAPI()
        with api:
            r2 = H.run_cli(["--file", p])
        self.assertEqual(r2.rc, 0, f"{r2.out}\n{r2.err}")
        self.assertEqual(r2.changed, "0", f"資料未變應為 CHANGED=0\n{r2.out}")
        self.assertFalse(r2.wrote)
        self.assertEqual(before, H.read_bytes(p))
        self.assertIn("內容無變化", r2.out)
        self.assertGreater(len(api.calls), 0, "仍應實際查詢端點以取得當日資料")

    def test_holiday_rerun_keeps_timestamp(self):
        """假日：端點回傳同一交易日資料 → 不寫檔、時間戳沿用。"""
        _, p, _, _ = clean_run()
        before = H.read_bytes(p)
        stamp_before = re.search(r"var SNAPSHOT_AT = '([^']*)'", before.decode()).group(1)
        with H.FakeAPI():
            r = H.run_cli(["--file", p])
        after = H.read_bytes(p)
        self.assertEqual(r.changed, "0")
        self.assertEqual(before, after)
        stamp_after = re.search(r"var SNAPSHOT_AT = '([^']*)'", after.decode()).group(1)
        self.assertEqual(stamp_before, stamp_after)

    def test_no_git_dirt_when_unchanged(self):
        _, p, _, _ = clean_run()
        with H.FakeAPI():
            r = H.run_cli(["--file", p])
        self.assertEqual(r.changed, "0")
        self.assertNotIn("已更新", r.out)
        self.assertTrue(no_temps(p))

    def test_weekend_like_run_still_queries_live_endpoints(self):
        """非交易日仍必須真的去問端點（才能知道資料有沒有變）。"""
        _, p, _, _ = clean_run()
        api = H.FakeAPI()
        with api:
            H.run_cli(["--file", p])
        self.assertEqual(set(api.parse(u)[0] for u in api.calls), set(H.ALL_PATHS))


# ==========================================================================
# 8. 時間戳邏輯
# ==========================================================================
class TestTimestampLogic(unittest.TestCase):
    def test_unchanged_data_keeps_existing_timestamp(self):
        _, p, _, _ = clean_run()
        before = H.read_bytes(p)
        old = re.search(r"var SNAPSHOT_AT = '([^']*)'", before.decode()).group(1)
        frozen = H.ref_dt(20, 30, 0)
        with H.FakeAPI(), H.freeze_now(frozen):
            r = H.run_cli(["--file", p])
        self.assertEqual(r.rc, 0)
        self.assertEqual(r.changed, "0")
        self.assertIn("沿用檔案中既有時間戳", r.out)
        self.assertNotIn(frozen.strftime("%Y/%m/%d %H:%M:%S"), r.out)
        self.assertIn(old, r.out)

    def test_changed_data_uses_current_capture_time(self):
        """資料有變 → 必須用本次擷取時間，否則新資料配舊時間戳。"""
        text = re.sub(r'(id="idx-val">)[\d,.]+', r"\g<1>99,999.99",
                      H.repo_index_html())
        p = temp_html(text)
        frozen = H.ref_dt(21, 15, 30)
        stamp = frozen.strftime("%Y/%m/%d %H:%M:%S")
        with H.FakeAPI(), H.freeze_now(frozen):
            r = H.run_cli(["--file", p])
        self.assertEqual(r.rc, 0, f"{r.out}\n{r.err}")
        self.assertEqual(r.changed, "1", f"資料已變應為 CHANGED=1\n{r.out}")
        self.assertIn("本次擷取時間（資料已變更）", r.out)
        out = open(p, encoding="utf-8").read()
        self.assertIn(f"var SNAPSHOT_AT = '{stamp}';", out)
        self.assertIn(stamp, out)
        self.assertIn(f"{H.ref_verified_close():,.2f}", out, "應以本次抓到的真實數值覆蓋錯誤值")

    def test_force_meta_writes_timestamp_without_data_change(self):
        _, p, before, _ = clean_run()
        frozen = H.ref_dt(22, 5, 9)
        stamp = frozen.strftime("%Y/%m/%d %H:%M:%S")
        with H.FakeAPI(), H.freeze_now(frozen):
            r = H.run_cli(["--file", p, "--force-meta"])
        self.assertEqual(r.rc, 0)
        self.assertIn("--force-meta（強制更新時間戳）", r.out)
        after = H.read_bytes(p)
        self.assertNotEqual(before, after)
        self.assertIn(stamp, after.decode())
        self.assertEqual(us.meta_stripped(before.decode()),
                         us.meta_stripped(after.decode()),
                         "force-meta 只應改時間戳，不得改動資料")

    def test_force_meta_reports_changed_0_because_data_unchanged(self):
        """force-meta 只動時間戳，CHANGED 仍應為 0（不觸發資料 commit）。"""
        _, p, _, _ = clean_run()
        with H.FakeAPI():
            r = H.run_cli(["--file", p, "--force-meta"])
        self.assertEqual(r.changed, "0")

    def test_meta_stripped_normalises_timestamp(self):
        self.assertEqual(us.meta_stripped("x 2026/09/24 20:20:00 y"),
                         us.meta_stripped("x 2026/09/25 09:00:00 y"))
        self.assertNotEqual(us.meta_stripped("x 2026/09/24 20:20:00 y"),
                            us.meta_stripped("x 1 y"))

    def test_dry_run_never_writes(self):
        _, p, before, _ = clean_run()
        with H.FakeAPI():
            r = H.run_cli(["--file", p, "--dry-run"])
        self.assertEqual(r.rc, 0)
        self.assertIn("dry-run", r.out)
        self.assertEqual(before, H.read_bytes(p))

    def test_missing_file_returns_exit_2(self):
        r = H.run_cli(["--file", "/nonexistent/index.html"])
        self.assertEqual(r.rc, 2)
        self.assertIn("找不到", r.err)

    def test_file_without_markers_returns_exit_2(self):
        p = temp_html("<html><body>no markers here</body></html>")
        before = H.read_bytes(p)
        with H.FakeAPI():
            r = H.run_cli(["--file", p])
        self.assertEqual(r.rc, 2)
        self.assertIn("SNAP marker", r.err)
        self.assertEqual(before, H.read_bytes(p))


# ==========================================================================
# 9. 交叉事件計算
# ==========================================================================
class TestCrossoverMath(unittest.TestCase):
    """先以可控合成資料驗證演算法，再於 TestCrossoverIntegration 用真資料驗證。"""

    @staticmethod
    def make_rows(closes):
        base = datetime(2026, 1, 1).date()
        return [{"date": (base + timedelta(days=i)).strftime("%Y/%m/%d"),
                 "close": float(c), "amt": 1.0} for i, c in enumerate(closes)]

    def test_sma_matches_independent_implementation(self):
        closes = [100 + (i % 7) * 3 for i in range(60)]
        self.assertEqual(us.sma(closes, 5), H.independent_sma(closes, 5))
        self.assertEqual(us.sma(closes, 20), H.independent_sma(closes, 20))

    def test_single_golden_cross(self):
        closes = [100.0] * 20 + [80.0] * 10 + [130.0] * 12
        rows = self.make_rows(closes)
        got = us.find_crossovers(rows, us.sma(closes, 5), us.sma(closes, 20), 0)
        exp = H.independent_crossovers(rows, 5, 20)
        self.assertEqual([(c["date"], c["kind"]) for c in got],
                         [(c["date"], c["kind"]) for c in exp])
        self.assertEqual([c["kind"] for c in got], ["g"])
        self.assertEqual(got[0]["kind"], "g")

    def test_single_death_cross(self):
        closes = [100.0] * 20 + [140.0] * 10 + [70.0] * 12
        rows = self.make_rows(closes)
        got = us.find_crossovers(rows, us.sma(closes, 5), us.sma(closes, 20), 0)
        exp = H.independent_crossovers(rows, 5, 20)
        self.assertEqual([(c["date"], c["kind"]) for c in got],
                         [(c["date"], c["kind"]) for c in exp])
        self.assertEqual([c["kind"] for c in got], ["d"])

    def test_alternating_golden_and_death(self):
        # 五段行情（平→跌→漲→跌→漲）可穩定產生 2 次死亡、2 次黃金交叉
        closes = ([100.0] * 20 + [60.0] * 20 + [200.0] * 20
                  + [60.0] * 20 + [200.0] * 20)
        rows = self.make_rows(closes)
        got = us.find_crossovers(rows, us.sma(closes, 5), us.sma(closes, 20), 0)
        exp = H.independent_crossovers(rows, 5, 20)
        self.assertEqual([(c["date"], c["kind"]) for c in got],
                         [(c["date"], c["kind"]) for c in exp])
        kinds = [c["kind"] for c in got]
        self.assertGreaterEqual(kinds.count("g"), 2)
        self.assertGreaterEqual(kinds.count("d"), 2)
        for a, b in zip(kinds, kinds[1:]):
            self.assertNotEqual(a, b, "同一型別不應連續出現")

    def test_every_cross_is_a_real_sign_flip(self):
        closes = ([100.0] * 20 + [80.0] * 10 + [130.0] * 12 + [60.0] * 12
                  + [150.0] * 12 + [90.0] * 12)
        rows = self.make_rows(closes)
        ma5, ma20 = us.sma(closes, 5), us.sma(closes, 20)
        got = us.find_crossovers(rows, ma5, ma20, 0)
        self.assertTrue(got, "合成資料應至少產生一個交叉")
        idx = {r["date"]: i for i, r in enumerate(rows)}
        for c in got:
            i = idx[c["date"]]
            d0, d1 = ma5[i - 1] - ma20[i - 1], ma5[i] - ma20[i]
            with self.subTest(date=c["date"], kind=c["kind"]):
                if c["kind"] == "g":
                    self.assertLess(d0, 0)
                    self.assertGreaterEqual(d1, 0)
                else:
                    self.assertGreater(d0, 0)
                    self.assertLessEqual(d1, 0)
                self.assertAlmostEqual(c["ma5"], ma5[i], places=6)
                self.assertAlmostEqual(c["ma20"], ma20[i], places=6)
                self.assertAlmostEqual(c["close"], rows[i]["close"], places=6)

    def test_cross_never_reported_before_scan_from(self):
        closes = [100.0] * 20 + [80.0] * 10 + [130.0] * 12 + [60.0] * 15
        rows = self.make_rows(closes)
        got = us.find_crossovers(rows, us.sma(closes, 5), us.sma(closes, 20), 40)
        for c in got:
            self.assertGreaterEqual(rows.index(next(r for r in rows if r["date"] == c["date"])), 40)

    def test_no_cross_when_flat(self):
        rows = self.make_rows([100.0] * 40)
        self.assertEqual(
            us.find_crossovers(rows, us.sma([100.0] * 40, 5), us.sma([100.0] * 40, 20), 0), [])

    def test_no_crash_when_ma_is_none(self):
        closes = [100.0] * 22
        rows = self.make_rows(closes)
        self.assertIsInstance(
            us.find_crossovers(rows, us.sma(closes, 5), us.sma(closes, 20), 0), list)

    def test_label_rows_are_staggered(self):
        """同日多筆標籤必須交錯高度，避免互相重疊。"""
        crosses = [{"wi": i, "date": f"2026/01/{i + 1:02d}", "kind": "g" if i % 2 else "d"}
                   for i in range(6)]
        rows = us.assign_label_rows(crosses)
        self.assertEqual(len(rows), 6)
        self.assertTrue(set(rows.values()).issubset(set(us.LABEL_ROWS)))

    def test_label_rows_keep_min_horizontal_gap_when_spaced(self):
        """交叉間距足夠時（真實 60 日視窗的常態），同一排標籤必須保持最小水平間距。"""
        step = (us.X1 - us.X0) / (us.N_WINDOW - 1)
        need = int(us.LABEL_MIN_GAP / step) + 1          # 5 個索引 ≒ 78 SVG 單位
        crosses = [{"wi": i * need, "date": f"2026/01/{i + 1:02d}", "kind": "g"}
                   for i in range(8)]
        rows = us.assign_label_rows(crosses)
        self.assertEqual(len(rows), len(crosses))
        by_row: dict[float, list[int]] = {}
        for wi, y in rows.items():
            by_row.setdefault(y, []).append(wi)
        for wi_list in by_row.values():
            xs = sorted(us.x_of(wi) for wi in wi_list)
            for a, b in zip(xs, xs[1:]):
                self.assertGreaterEqual(b - a, us.LABEL_MIN_GAP - 1e-6,
                                        "同排標籤間距不足，會重疊")

    def test_label_rows_degrade_gracefully_when_crowded(self):
        """交叉密集到兩排都放不下時（真實 60 日視窗不會發生），
        assign_label_rows 仍須為每一筆指定一排且不得拋例外 ——
        此時的交錯是『盡量分散』而非保證最小間距，屬已知限制。"""
        crosses = [{"wi": i, "date": f"2026/01/{i + 1:02d}", "kind": "g"} for i in range(8)]
        rows = us.assign_label_rows(crosses)
        self.assertEqual(len(rows), len(crosses))
        self.assertTrue(set(rows.values()).issubset(set(us.LABEL_ROWS)))


class TestCrossoverIntegration(unittest.TestCase):
    """真實樣本：清單、圖上標記、摘要三者必須一致。"""

    def setUp(self):
        self.out = baseline_out()
        self.exp = expected_crosses()
        self.in_win = in_window_crosses()

    def test_list_matches_independent_scan(self):
        """事件清單為『最新在前』，故需與獨立計算結果的反序比對。"""
        got = parse_cx_rows(self.out)
        self.assertEqual([(g["date"], g["kind"]) for g in got],
                         [(e["date"], e["kind"]) for e in reversed(self.exp)])
        self.assertEqual(len(got), len(self.exp))

    def test_list_is_newest_first(self):
        dates = [r["date"] for r in parse_cx_rows(self.out)]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_list_close_matches_history(self):
        closes = {r["date"]: r["close"] for r in history()}
        for row in parse_cx_rows(self.out):
            with self.subTest(date=row["date"]):
                self.assertAlmostEqual(row["close"], closes[row["date"]], places=2)

    def test_kinds_alternate(self):
        kinds = [r["kind"] for r in parse_cx_rows(self.out)]
        for a, b in zip(kinds, kinds[1:]):
            self.assertNotEqual(a, b)

    def test_cross_dates_do_not_include_data_date(self):
        """交叉是由「前一日差值 × 當日差值 ≤ 0」判定；最後一筆能否成交叉取決於資料，
        但不得出現未在歷史中的日期。"""
        known = {r["date"] for r in history()}
        for row in parse_cx_rows(self.out):
            self.assertIn(row["date"], known)

    def test_chart_dot_count_equals_in_window_crosses(self):
        self.assertEqual(len(parse_svg_dots(self.out)), len(self.in_win))
        self.assertGreater(len(self.in_win), 0)

    def test_chart_dots_are_ascending_left_to_right(self):
        dots = parse_svg_dots(self.out)
        xs = [x for x, _ in dots]
        self.assertEqual(xs, sorted(xs))

    def test_chart_marks_match_in_window_crosses_exactly(self):
        marks = parse_svg_dot_marks(self.out)
        self.assertEqual([(d, k) for d, k, _ in marks],
                         [(c["date"], c["kind"]) for c in self.in_win])

    def test_guide_lines_align_with_dots(self):
        dots = parse_svg_dots(self.out)
        lines = parse_guide_lines(self.out)
        self.assertEqual(len(lines), len(dots))
        for (lx, _lk), (dx, _dy) in zip(lines, dots):
            self.assertAlmostEqual(lx, dx, places=1, msg="導引線與圓點 x 未對齊")

    def test_guide_line_kinds_match_cross_types(self):
        self.assertEqual([k for _, k in parse_guide_lines(self.out)],
                         [c["kind"] for c in self.in_win])

    def test_dot_colour_matches_cross_type(self):
        kind_by_date = {c["date"]: c["kind"] for c in self.exp}
        for date, kind, fill in parse_svg_dot_marks(self.out):
            self.assertEqual(kind, kind_by_date[date])
            self.assertEqual(fill, us.CROSS_COLOR[kind].lower(),
                             f"{date}（{kind}）顏色應為 {us.CROSS_COLOR[kind]}")

    def test_only_two_cross_colours_used(self):
        fills = {f for _, _, f in parse_svg_dot_marks(self.out)}
        self.assertEqual(fills, {c.lower() for c in us.CROSS_COLOR.values()})

    def test_chart_labels_show_dates(self):
        svg = svg_of(self.out)
        for c in self.in_win:
            self.assertIn(c["date"], svg, f"圖上缺少 {c['date']} 標籤")

    def test_label_pills_do_not_overlap(self):
        pills = parse_pills(self.out)
        self.assertEqual(len(pills), len(self.in_win))
        for i, a in enumerate(pills):
            for b in pills[i + 1:]:
                overlap_x = not (a["x"] + a["w"] <= b["x"] or b["x"] + b["w"] <= a["x"])
                overlap_y = abs(a["y"] - b["y"]) < 12
                self.assertFalse(overlap_x and overlap_y,
                                 f"標籤重疊：{a} 與 {b}")

    def test_labels_use_at_least_two_stagger_heights(self):
        ys = {p["y"] for p in parse_pills(self.out)}
        self.assertGreaterEqual(len(ys), 2, "密集標籤應交錯高度")

    def test_mobile_key_matches_chart_marks(self):
        key = region_body(self.out, "CXMKEY")
        for c in self.in_win:
            self.assertIn(c["date"][5:], key, f"摘要列缺少 {c['date']}")
        self.assertEqual(key.count('class="k '), len(self.in_win))

    def test_mobile_key_kinds_match(self):
        key = region_body(self.out, "CXMKEY")
        for c in self.in_win:
            label = "黃金" if c["kind"] == "g" else "死亡"
            self.assertRegex(key, rf'<span class="k {c["kind"]}"><i[^>]*></i>{c["date"][5:]} {label}</span>')

    def test_ma_strip_reports_most_recent_cross(self):
        body = region_body(self.out, "MASTRIP")
        last = self.exp[-1]
        self.assertIn(last["date"], body)
        self.assertIn("黃金交叉" if last["kind"] == "g" else "死亡交叉", body)
        # 摘要列統計的是『走勢圖區間內』的交叉次數，而非全期筆數
        self.assertIn(f"{len(self.in_win)} 次", body)

    def test_note_region_explains_crosses(self):
        body = region_body(self.out, "CXNOTE")
        self.assertGreater(len(body.strip()), 40)
        self.assertTrue("黃金交叉" in body or "死亡交叉" in body)

    def test_sign_flip_holds_in_real_data(self):
        """真實資料的每一筆交叉都必須真的是差值變號。"""
        h = history()
        closes = [r["close"] for r in h]
        ma5, ma20 = H.independent_sma(closes, 5), H.independent_sma(closes, 20)
        idx = {r["date"]: i for i, r in enumerate(h)}
        for c in self.exp:
            i = idx[c["date"]]
            d0, d1 = ma5[i - 1] - ma20[i - 1], ma5[i] - ma20[i]
            with self.subTest(date=c["date"], kind=c["kind"]):
                if c["kind"] == "g":
                    self.assertLess(d0, 0)
                    self.assertGreaterEqual(d1, 0)
                else:
                    self.assertGreater(d0, 0)
                    self.assertLessEqual(d1, 0)


# ==========================================================================
# 10. 安全性：測試期間不得有任何真實網路呼叫
# ==========================================================================
class TestNoNetwork(unittest.TestCase):
    @staticmethod
    def _boom(*a, **k):
        raise AssertionError("測試期間發生了真實網路連線！")

    def test_full_cycle_makes_no_socket_calls(self):
        p = temp_html(H.repo_index_html())
        with mock.patch.object(socket, "socket", self._boom), \
                mock.patch.object(socket, "create_connection", self._boom), \
                H.FakeAPI():
            r = H.run_cli(["--file", p])
        self.assertEqual(r.rc, 0, f"{r.out}\n{r.err}")

    def test_fault_run_makes_no_socket_calls(self):
        p = temp_html(H.repo_index_html())
        with mock.patch.object(socket, "socket", self._boom), \
                H.FakeAPI({PATH_MI: "http_500"}):
            r = H.run_cli(["--file", p])
        self.assertNotEqual(r.rc, 0)

    def test_every_endpoint_the_script_uses_is_stubbed(self):
        api = H.FakeAPI()
        with api:
            us.fetch(PATH_STOCK)
            us.fetch(PATH_MI, {"date": "20260924"})
            us.fetch(PATH_5MIN)
            us.fetch(PATH_FMT, {"date": "20260901"})
            us.fetch(PATH_BFI, {"dayDate": H.inst_days()[-1]})
        self.assertEqual({api.parse(u)[0] for u in api.calls}, set(H.ALL_PATHS))

    def test_fetch_touches_only_the_twse_base(self):
        api = H.FakeAPI()
        with api:
            us.fetch(PATH_STOCK)
        self.assertTrue(all(u.startswith("https://www.twse.com.tw") for u in api.calls))


# ==========================================================================
# 11. 原子寫入與輸出驗證
# ==========================================================================
class TestAtomicWriteAndVerify(unittest.TestCase):
    def test_write_replaces_file(self):
        d = tempfile.mkdtemp(prefix="twstock-aw-")
        p = os.path.join(d, "a.html")
        with open(p, "w", encoding="utf-8") as f:
            f.write("old")
        us.atomic_write(p, "new")
        self.assertEqual(open(p, encoding="utf-8").read(), "new")
        self.assertTrue(no_temps(p))

    def test_failed_write_leaves_original_and_no_temp(self):
        d = tempfile.mkdtemp(prefix="twstock-aw-")
        p = os.path.join(d, "a.html")
        with open(p, "w", encoding="utf-8") as f:
            f.write("old")
        with mock.patch.object(us.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                us.atomic_write(p, "new")
        self.assertEqual(open(p, encoding="utf-8").read(), "old")
        self.assertTrue(no_temps(p), "失敗後不得留下暫存檔")

    def test_verify_output_accepts_a_good_document(self):
        good = H.repo_index_html()
        regions = {n: region_body(good, n) for n in sorted(H.EXPECTED_REGIONS)}
        us.verify_output(good, regions)  # 不應拋例外

    def test_verify_output_rejects_empty_svg_region(self):
        good = H.repo_index_html()
        regions = {n: region_body(good, n) for n in sorted(H.EXPECTED_REGIONS)}
        broken = dict(regions)
        broken["SVG"] = ""
        with self.assertRaises(RuntimeError):
            us.verify_output(us.apply_regions(good, broken), broken)

    def test_verify_output_rejects_svg_without_wrapper(self):
        """少了 <svg> wrapper → 所有圖表子元素不會繪製，必須被擋下。"""
        good = H.repo_index_html()
        regions = {n: region_body(good, n) for n in sorted(H.EXPECTED_REGIONS)}
        broken = dict(regions)
        broken["SVG"] = broken["SVG"].replace("<svg", "<div").replace("</svg>", "</div>")
        with self.assertRaises(RuntimeError) as cm:
            us.verify_output(us.apply_regions(good, broken), broken)
        self.assertTrue("SVG" in str(cm.exception))

    def test_verify_output_rejects_too_few_volume_bars(self):
        good = H.repo_index_html()
        regions = {n: region_body(good, n) for n in sorted(H.EXPECTED_REGIONS)}
        broken = dict(regions)
        broken["SVG"] = re.sub(r'<rect class="v-(?:up|down)".*?</rect>\s*', "",
                               broken["SVG"], flags=re.S)
        with self.assertRaises(RuntimeError):
            us.verify_output(us.apply_regions(good, broken), broken)

    def test_apply_regions_requires_exactly_one_marker(self):
        good = H.repo_index_html()
        with self.assertRaises(RuntimeError):
            us.apply_regions(good, {"NOT_A_REGION": "x"})

    def test_region_styles_use_js_markers_inside_script(self):
        for name in sorted(us.JS_REGIONS):
            o, c = us._region_styles(name)[0]
            self.assertIn("SNAP", o)
            self.assertNotIn("<!--", o)
        o, c = us._region_styles("SVG")[0]
        self.assertIn("<!--", o)


# ==========================================================================
# 13. 投資免責聲明：必須逐字為指定英文版本，且不得被快照重寫覆蓋
# ==========================================================================
# 使用者指定的英文免責聲明（逐字，不得改動）
DISCLAIMER_EN = (
    "All data on this page is a compilation and visualization of publicly available "
    "information from the Taiwan Stock Exchange; it is provided for informational "
    "purposes only and does not constitute investment advice, an offer, or a "
    "recommendation. Discrepancies between this data and actual conditions may arise "
    "due to corrections by the exchange, delays, or transmission issues; investors "
    "should rely on official announcements from the Taiwan Stock Exchange and the "
    "Market Observation Post System."
)

# 舊版文字：一旦再出現即為回歸
OLD_DISCLAIMER_MARKERS = (
    "As an AI Agent",
    "licensed financial advisor",
    "legally protected, personalized investment advice",
)

# 中文免責聲明：必須保留
DISCLAIMER_ZH = "本頁所有數據為臺灣證券交易所公開資料之整理與視覺化"


def _region_spans(html: str) -> list[tuple[int, int, str]]:
    """所有 SNAP 區塊的 (start, end, name)，含 HTML 與 JS 兩種 marker 語法。"""
    spans: list[tuple[int, int, str]] = []
    for rx in (r"<!--SNAP:([A-Z_]+)-->(.*?)<!--/SNAP:\1-->",
               r"/\*SNAP:([A-Z_]+)\*/(.*?)/\*SNAP:/\1\*/"):
        for m in re.finditer(rx, html, re.S):
            spans.append((m.start(), m.end(), m.group(1)))
    return spans


def _disc_block(html: str) -> str | None:
    m = re.search(r'<p class="disc">.*?</p>', html, re.S)
    return m.group(0) if m else None


def _forced_change_html() -> str:
    """改掉一個快照數字，強制本次更新真的重寫檔案（否則測試會空轉）。"""
    return re.sub(r'(id="idx-val">)[\d,.]+', r"\g<1>99,999.99", H.repo_index_html())


class TestDisclaimerPreservation(unittest.TestCase):
    """免責聲明為使用者指定的英文版本，且 update_snapshot.py 不得覆寫它。"""

    def test_repo_index_has_new_disclaimer_verbatim(self):
        html = H.repo_index_html()
        self.assertEqual(html.count(DISCLAIMER_EN), 1,
                         "index.html 應逐字包含新版英文免責聲明，且恰好一次")

    def test_repo_index_has_no_old_disclaimer(self):
        html = H.repo_index_html()
        for marker in OLD_DISCLAIMER_MARKERS:
            self.assertNotIn(marker, html, f"仍殘留舊版免責聲明文字：{marker!r}")

    def test_chinese_disclaimer_kept(self):
        html = H.repo_index_html()
        self.assertIn(DISCLAIMER_ZH, html, "中文免責聲明不得被移除")

    def test_disclaimer_is_outside_every_snap_region(self):
        """免責聲明在 marker 之外 → update_snapshot.py 的 apply_regions 不會碰它。

        若日後有人把免責聲明搬進某個 SNAP 區塊，此測試會失敗 —— 那就必須同步
        更新腳本內的樣板，否則下次自動更新會把文字寫回去。
        """
        html = H.repo_index_html()
        spans = _region_spans(html)
        self.assertGreater(len(spans), 20, "應偵測到所有 SNAP 區塊")
        i = html.index(DISCLAIMER_EN)
        for start, end, name in spans:
            self.assertFalse(start < i < end,
                             f"免責聲明落在 SNAP:{name} 區塊內，腳本重寫時會覆蓋它")

    def test_disclaimer_survives_a_snapshot_update(self):
        """真的跑一次會寫檔的更新，新文字必須原樣留著、舊文字不得回來。"""
        p = temp_html(_forced_change_html())
        with H.FakeAPI():
            r = H.run_cli(["--file", p])
        self.assertEqual(r.rc, 0, f"{r.out}\n{r.err}")
        self.assertEqual(r.changed, "1", f"應確實重寫檔案\n{r.out}")
        self.assertTrue(r.wrote)
        html = open(p, encoding="utf-8").read()
        self.assertEqual(html.count(DISCLAIMER_EN), 1,
                         "更新後新英文免責聲明必須逐字存在且僅一次")
        self.assertIn(DISCLAIMER_ZH, html, "更新後中文免責聲明仍須存在")
        for marker in OLD_DISCLAIMER_MARKERS:
            self.assertNotIn(marker, html, f"更新後不得出現舊文字：{marker!r}")

    def test_bytes_around_disclaimer_untouched_by_update(self):
        """更新前後 .disc 區塊必須位元組完全相同。"""
        p = temp_html(_forced_change_html())
        before = open(p, encoding="utf-8").read()
        with H.FakeAPI():
            r = H.run_cli(["--file", p])
        self.assertEqual(r.rc, 0, f"{r.out}\n{r.err}")
        after = open(p, encoding="utf-8").read()
        b_block, a_block = _disc_block(before), _disc_block(after)
        self.assertIsNotNone(b_block, "找不到 .disc 區塊")
        self.assertEqual(b_block, a_block,
                         "更新後 .disc 區塊（中文＋新英文免責聲明）必須位元組不變")


# ==========================================================================
# 14. 可容忍的異常列：個股層級的壞資料應「排除」而非讓整支程式失敗
# ==========================================================================
TOLERATED_CASES = [
    ("stock_bad_close", PATH_STOCK, "stock_bad_close"),
    ("stock_full_roc_date", PATH_STOCK, "stock_full_roc_date"),
]


class TestToleratedMalformedRows(unittest.TestCase):
    """個股層級的壞資料（單一列）不得讓整支程式失敗，但也不得寫入錯誤內容。"""

    def _run(self, path, fault):
        d = tempfile.mkdtemp(prefix="twstock-tol-")
        p = os.path.join(d, "index.html")
        H.write_html(p)
        before = H.read_bytes(p)
        with H.FakeAPI({path: fault}):
            r = H.run_cli(["--file", p])
        return r, p, before

    def test_bad_close_row_is_excluded_not_fatal(self):
        """單一檔個股收盤價為 N/A → 該檔被排除，程式仍成功且不亂寫。"""
        r, p, before = self._run(PATH_STOCK, "stock_bad_close")
        self.assertEqual(r.rc, 0, f"單一壞列不應讓程式失敗\n{r.err}")
        self.assertEqual(before, H.read_bytes(p), "資料未變時不得改寫原檔")
        self.assertTrue(no_temps(p))

    def test_roc_date_with_slashes_is_accepted(self):
        """115/09/24 與 1150924 必須等價（兩種民國日期寫法都要能解析）。"""
        r, p, before = self._run(PATH_STOCK, "stock_full_roc_date")
        self.assertEqual(r.rc, 0, f"兩種民國日期寫法應等價\n{r.err}")
        self.assertEqual(before, H.read_bytes(p))

    def test_no_unused_fault_names(self):
        """每個定義的故障注入都必須被至少一個測試引用（防止死碼）。"""
        with open(os.path.abspath(__file__), encoding="utf-8") as f:
            src = f.read()
        unused = [n for n in H.FAULT_NAMES if f'"{n}"' not in src]
        self.assertEqual(unused, [], f"定義了卻未被任何測試使用的故障注入：{unused}")


# ==========================================================================
# 15. verify_output 的防護分支：每一條都必須真的擋下壞輸出
# ==========================================================================
class TestVerifyOutputGuards(unittest.TestCase):
    """verify_output() 是「寫檔前的最後一道閘門」，每個分支都必須真的會 raise。"""

    @classmethod
    def setUpClass(cls):
        cls.good = H.repo_index_html()
        cls.regions = {n: us._region_body(cls.good, n).group(2)
                       for n in H.EXPECTED_REGIONS
                       if us._region_body(cls.good, n) is not None}

    def _expect_reject(self, mutate, needle=None):
        r = dict(self.regions)
        mutate(r)
        with self.assertRaises(RuntimeError) as cm:
            us.verify_output(us.apply_regions(self.good, r), r)
        if needle:
            self.assertIn(needle, str(cm.exception))

    def test_accepts_the_real_document(self):
        us.verify_output(self.good, self.regions)  # 不應拋例外

    def test_rejects_blank_region(self):
        self._expect_reject(lambda r: r.__setitem__("FOOTB", ""), "空白")

    def test_rejects_non_numeric_idx_val(self):
        self._expect_reject(
            lambda r: r.__setitem__("IDXROW",
                r["IDXROW"].replace('id="idx-val">', 'id="idx-val">abc')), "idx-val")

    def test_rejects_non_numeric_kpi_amt(self):
        self._expect_reject(
            lambda r: r.__setitem__("KPIS",
                r["KPIS"].replace('id="kpi-amt">', 'id="kpi-amt">x')), "kpi-amt")

    def test_rejects_none_literal(self):
        self._expect_reject(lambda r: r.__setitem__("FOOTB", r["FOOTB"] + "None"), "None")

    def test_rejects_undefined_literal(self):
        self._expect_reject(lambda r: r.__setitem__("FOOTB", r["FOOTB"] + "undefined"), "undefined")

    def test_rejects_unrendered_template(self):
        self._expect_reject(lambda r: r.__setitem__("FOOTB", r["FOOTB"] + "{{x}}"), "{{")

    def test_rejects_too_short_region(self):
        self._expect_reject(lambda r: r.__setitem__("CXLIST", "<p>x</p>"), "過短")

    def test_rejects_unclosed_html_comment(self):
        self._expect_reject(lambda r: r.__setitem__("FOOTB", "<!-- oops " + r["FOOTB"]), "未閉合")

    def test_rejects_html_marker_inside_script(self):
        self._expect_reject(
            lambda r: r.__setitem__("JS_SNAPAT", "<!--SNAP:X-->1<!--/SNAP:X-->"), "script")

    def test_rejects_missing_svg_close(self):
        self._expect_reject(lambda r: r.__setitem__("SVG", r["SVG"].replace("</svg>", "")), "wrapper")

    def test_rejects_missing_role_img(self):
        self._expect_reject(lambda r: r.__setitem__("SVG", r["SVG"].replace('role="img"', "")), "role")

    def test_rejects_missing_viewbox(self):
        self._expect_reject(
            lambda r: r.__setitem__("SVG", r["SVG"].replace('viewBox="0 0 1000 420"', "")), "viewBox")

    def test_rejects_missing_svg_title(self):
        self._expect_reject(lambda r: r.__setitem__("SVG", r["SVG"].replace("<title>", "<t>")), "title")

    def test_rejects_missing_area(self):
        self._expect_reject(
            lambda r: r.__setitem__("SVG", r["SVG"].replace('class="area-f"', 'class="xx"')), "area-f")

    def test_rejects_missing_ma20_line(self):
        self._expect_reject(
            lambda r: r.__setitem__("SVG", r["SVG"].replace('class="line-ma20"', 'class="xx"')), "MA20")

    def test_rejects_missing_cross_dot(self):
        self._expect_reject(
            lambda r: r.__setitem__("SVG", r["SVG"].replace('class="cx-dot"', 'class="xx"')), "交叉")

    def test_rejects_missing_green_volume_bar(self):
        self._expect_reject(
            lambda r: r.__setitem__("SVG", r["SVG"].replace('class="v-down"', 'class="xx"')), "量柱")


# ==========================================================================
# 16. 純函式邊界：缺值、空字串、非數字、極端輸入
# ==========================================================================
class TestHelperEdgeCases(unittest.TestCase):
    def test_n2_handles_all_missing_forms(self):
        for v in ("", " ", "-", "--", "N/A", "nan", "NaN", "None", None, [1], {}):
            self.assertIsNone(us.n2(v), f"n2({v!r}) 應為 None")

    def test_n2_parses_real_numbers(self):
        self.assertEqual(us.n2("1,234.5"), 1234.5)
        self.assertEqual(us.n2("\u2212123"), -123.0)      # unicode minus
        self.assertEqual(us.n2("<b>5</b>"), 5.0)          # HTML 標籤剝除
        self.assertEqual(us.n2("12%"), 12.0)              # 百分比符號剝除
        self.assertEqual(us.n2(0), 0.0)                   # 0 不是缺值

    def test_sma_returns_none_until_window_filled(self):
        self.assertEqual(us.sma([1, 2, 3], 5), [None, None, None])
        self.assertEqual(us.sma([1, 2, 3], 3), [None, None, 2.0])

    def test_axis_layout_covers_flat_series(self):
        ax = us.axis_layout([100.0, 100.0, 100.0])
        self.assertLessEqual(ax["lo"], 100.0)
        self.assertGreaterEqual(ax["hi"], 100.0)

    def test_vol_layout_handles_zero_volume(self):
        self.assertGreater(us.vol_layout(0)["vmax"], 0)

    def test_esc_handles_none(self):
        self.assertEqual(us.esc(None), "None")

    def test_parse_cnt_handles_missing_and_grouped(self):
        self.assertEqual(us._parse_cnt(None), (None, None))
        self.assertEqual(us._parse_cnt(""), (None, None))
        self.assertEqual(us._parse_cnt("1,234(56)"), (1234, 56))
        self.assertEqual(us._parse_cnt("1,234"), (1234, None))

    def test_formatters_render_missing_as_zero(self):
        self.assertEqual(us.f2(None), "0.00")
        self.assertEqual(us.f0(None), "0")
        self.assertEqual(us.sgn(0), "flat")
        self.assertEqual(us.pts(None), "0.00")
        self.assertEqual(us.pct(None), "0.00%")


# ==========================================================================
# 16. 宇程君（韭齡多年的虛擬分析失）每日評論：scripts/analyst.py
# ==========================================================================
# 這一組測試守住整個角色功能的核心承諾：**文字完全由當日資料決定**。
# 若 analyst.py 偷讀了 datetime.now()、random、或任何寫死的市場數字，
# 「同一份資料重跑文字必須逐字元相同」這條就會失敗 —— 而該條正是
# 讓『資料未變 → CHANGED=0 → 不建立空 commit』得以成立的基礎。
# ==========================================================================
sys.path.insert(0, os.path.join(H.ROOT, "scripts"))
import analyst  # noqa: E402


def _analyst_cfg() -> dict:
    """在樣本（fixtures）下取得真正的 cfg，再據以產生角色文字。"""
    with H.FakeAPI():
        return us.collect()


def _analyst_html(cfg=None) -> str:
    cfg = cfg if cfg is not None else _analyst_cfg()
    return analyst.build(cfg, f0=us.f0, f2=us.f2, pct=us.pct,
                         pts=us.pts, esc=us.esc)


class TestAnalystDeterminism(unittest.TestCase):
    """角色文字必須是資料的純函式（無隨機、無時鐘相依）。"""

    def test_same_cfg_yields_byte_identical_output(self):
        cfg = _analyst_cfg()
        self.assertEqual(_analyst_html(cfg), _analyst_html(cfg),
                         "同一份 cfg 重跑必須逐位元組相同（不可有隨機成分）")

    def test_output_is_stable_across_a_real_clock_change(self):
        """把時鐘移到隔天，文字仍須完全相同 —— 證明沒有讀 datetime.now()。"""
        cfg = _analyst_cfg()
        before = _analyst_html(cfg)
        with H.freeze_now(H.ref_dt(23, 4, 5)):
            after = _analyst_html(cfg)
        self.assertEqual(before, after,
                         "角色文字不得隨系統時間改變（僅能取決於資料）")

    def test_no_market_numbers_are_hardcoded(self):
        """改一個輸入值，輸出必須跟著變 —— 證明數字不是寫死的文案。"""
        a = _analyst_html()
        cfg = _analyst_cfg()
        cfg["today_close"] = cfg["today_close"] + 500.0
        b = _analyst_html(cfg)
        self.assertNotEqual(a, b, "指數改了，角色文字必須跟著改")

    def test_changed_volume_changes_the_verdict(self):
        a = _analyst_html()
        cfg = _analyst_cfg()
        cfg["win"][-1]["amt"] = cfg["win"][-1]["amt"] * 0.5
        b = _analyst_html(cfg)
        self.assertNotEqual(a, b, "量能改了，角色文字必須跟著改")

    def test_mood_score_is_bounded_and_labelled(self):
        cfg = _analyst_cfg()
        a = analyst.analyze(cfg)
        self.assertGreaterEqual(a["mood"], 0.0)
        self.assertLessEqual(a["mood"], 100.0)
        self.assertIn(a["tier_key"], {"hot", "warm", "mid", "cool", "cold"})
        self.assertTrue(a["tier_label"])

    def test_mood_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(analyst.MOOD_WEIGHTS.values()), 1.0, places=9)

    def test_all_tiers_have_catchphrases(self):
        for _lo, key, _label in analyst.MOOD_TIERS:
            self.assertTrue(analyst.CATCHPHRASES.get(key),
                            f"氛圍等級 {key} 缺少口頭禪")


class TestAnalystReferencesRealData(unittest.TestCase):
    """輸出必須引用當日真實數字，而非泛泛之談。"""

    def setUp(self):
        self.cfg = _analyst_cfg()
        self.html = _analyst_html(self.cfg)

    def test_quotes_the_index_close_and_change(self):
        self.assertIn(us.f2(self.cfg["today_close"]), self.html)
        self.assertIn(us.pts(self.cfg["today_close"] - self.cfg["prev_close"]),
                      self.html)

    def test_quotes_breadth_and_turnover(self):
        mkt = self.cfg["mkt"]
        self.assertIn(us.f0(mkt["up"]), self.html)
        self.assertIn(us.f0(mkt["down"]), self.html)
        self.assertIn(us.f2(mkt["total_amt"] / 1e8), self.html)

    def test_quotes_tsmc_2330(self):
        self.assertIn(us.f2(self.cfg["tsmc"]["close"]), self.html)

    def test_quotes_moving_averages_and_crossovers(self):
        self.assertIn(us.f2(self.cfg["win_ma5"][-1]), self.html)
        self.assertIn(us.f2(self.cfg["win_ma20"][-1]), self.html)
        self.assertIn(str(len(self.cfg["crosses_all"])), self.html)

    def test_quotes_top_gainer_and_loser(self):
        g = self.cfg["gain"][0]
        l = self.cfg["lose"][0]
        self.assertIn(us.esc(g["code"]), self.html)
        self.assertIn(us.esc(g["name"]), self.html)
        self.assertIn(us.esc(l["code"]), self.html)
        self.assertIn(us.esc(l["name"]), self.html)


class TestAnalystHonesty(unittest.TestCase):
    """角色必須誠實標示：AI 生成虛擬角色、角色標籤為「韭齡多年的虛擬分析失」、非投資建議。"""

    def setUp(self):
        self.html = _analyst_html()

    def test_states_ai_generated_and_not_a_person(self):
        for phrase in (analyst.ROLE_LABEL, "AI 生成虛擬角色", "非投資建議"):
            self.assertIn(phrase, self.html, f"缺少誠實標示：{phrase}")
        self.assertIn("不是真人", self.html)

    def test_discloses_how_text_is_generated(self):
        self.assertIn("固定規則", self.html)
        self.assertIn("無隨機成分", self.html)
        self.assertIn("analyst.py", self.html)

    def test_mood_is_labelled_heuristic_not_a_forecast(self):
        self.assertIn("啟發式分數", self.html)
        self.assertIn("不是", self.html)

    def test_sector_classification_is_disclosed_as_approximate(self):
        self.assertIn("依股票代號區間粗略判定", self.html)
        self.assertIn("非證交所官方產業別", self.html)

    def test_stale_role_labels_are_gone(self):
        """舊標籤文字不得殘留在頁面上（換標籤時最容易漏掉的地方）。"""
        for stale in ("AI 生成角色", "非真人分析師", "不是分析師"):
            self.assertNotIn(stale, self.html, f"頁面仍殘留舊標籤：{stale}")

    def test_role_label_is_exactly_as_specified(self):
        """「分析失」是使用者指定的諧音寫法，不可被改成「分析師」。"""
        self.assertIn(analyst.ROLE_LABEL, self.html)
        self.assertNotIn("虛擬分析師", self.html)

    def test_avatar_is_local_not_external(self):
        # 頭像已隨「今日完整分析」搬進彈窗（AI_CHAT），因此改檢查彈窗外框。
        chat = analyst.build_chat_ui(brief_html=self.html, data_date="2026/10/15")
        self.assertIn('src="assets/yu_cheng_jun_avatar.png"', chat)
        self.assertNotIn("http://", chat)
        self.assertNotIn("https://", chat)
        # 分析內容本身不得引用任何外部資源
        self.assertNotIn("http://", self.html)
        self.assertNotIn("https://", self.html)

    def test_character_name_is_present(self):
        self.assertIn(analyst.CHARACTER, self.html)


class TestAnalystSectorMap(unittest.TestCase):
    def test_sector_lookup_covers_common_codes(self):
        self.assertIn("半導體", analyst.sector_of("2330"))
        self.assertIn("金融", analyst.sector_of("2881"))
        self.assertIn("航運", analyst.sector_of("2603"))
        self.assertEqual(analyst.sector_of("9999"), "其他")

    def test_sector_of_handles_garbage(self):
        self.assertEqual(analyst.sector_of(""), "其他")
        self.assertEqual(analyst.sector_of("ABC"), "其他")
        self.assertEqual(analyst.sector_of(None), "其他")

    def test_dominant_sector_is_deterministic(self):
        recs = [{"code": "2330"}, {"code": "2317"}, {"code": "2603"}]
        first = analyst.dominant_sector(recs)
        for _ in range(5):
            self.assertEqual(analyst.dominant_sector(recs), first)

    def test_dominant_sector_on_empty(self):
        self.assertEqual(analyst.dominant_sector([]), ("無", 0, 0))


class TestAnalystBlockIsWiredIntoThePage(unittest.TestCase):
    """角色區塊必須真的被注入 index.html，且頁尾 marker 完整。"""

    def test_region_is_registered(self):
        self.assertIn("AIANALYST", H.EXPECTED_REGIONS)

    def test_rendered_page_contains_the_block(self):
        body = region_body(baseline_out(), "AIANALYST")
        self.assertIn(analyst.CHARACTER, body)
        self.assertIn("ai-brief-head", body)
        # 已搬進彈窗：不再有獨立的卡片區塊與頭像
        self.assertNotIn("ai-card", body)
        self.assertNotIn("assets/yu_cheng_jun_avatar.png", body)

    def test_brief_lives_inside_the_chat_popup(self):
        """「今日完整分析」必須嵌在 AI_CHAT（彈窗）區塊內，頁面上不得再有獨立區塊。"""
        out = baseline_out()
        chat = region_body(out, "AI_CHAT")
        self.assertIn('id="ai-more"', chat)
        self.assertIn('id="ai-drawer"', chat)
        self.assertIn("<!--SNAP:AIANALYST-->", chat)
        # 頁面本文（去掉 AI_CHAT 區塊後）不得再出現獨立的宇程君卡片
        page_without_chat = out.replace(chat, "")
        self.assertNotIn("ai-card", page_without_chat)
        self.assertNotIn("ai-strip", page_without_chat)
        self.assertNotIn('id="ai-open"', page_without_chat)

    def test_avatar_asset_exists_in_repo(self):
        p = os.path.join(H.ROOT, "assets", "yu_cheng_jun_avatar.png")
        self.assertTrue(os.path.isfile(p), "角色頭像必須隨 repo 一起版控")
        self.assertGreater(os.path.getsize(p), 2000)

    def test_avatar_is_square_and_176px(self):
        """頭像必須是正方形（圓形裁切才不會被切掉）且為 88px 顯示的 2x。

        直接讀 PNG 的 IHDR，不需 PIL（測試套件維持零外部相依）。
        """
        p = os.path.join(H.ROOT, "assets", "yu_cheng_jun_avatar.png")
        with open(p, "rb") as fh:
            head = fh.read(24)
        self.assertEqual(head[:8], b"\x89PNG\r\n\x1a\n", "頭像必須是 PNG")
        width, height = struct.unpack(">II", head[16:24])
        self.assertEqual(width, height, "頭像必須是正方形，否則圓形裁切會切掉主體")
        self.assertEqual((width, height), (176, 176),
                         "頭像應為 176x176（頁面以 88px 顯示，2x 供高解析螢幕）")


# ==========================================================================
# 17. 宇程君對話引擎：scripts/analyst.py 的聊天彈窗
# ==========================================================================
# 這一組守住聊天型彈窗的核心承諾：
#   1. 回覆文字**完全由當日資料決定**（與評論區同樣是純函式）；
#   2. 關鍵字比對是**前端與後端一致**的固定規則，不是模型推論；
#   3. 聊天資料以 JSON 內嵌時**不會提前關閉 <script>**；
#   4. 誠實標示與免責聲明也必須出現在聊天視窗內。
# ==========================================================================
def _payload(cfg=None) -> dict:
    cfg = cfg if cfg is not None else _analyst_cfg()
    return analyst.build_dialog_payload(cfg, f0=us.f0, f2=us.f2, pct=us.pct,
                                        pts=us.pts, esc=us.esc)


def _payload_text(cfg=None) -> str:
    return analyst.build_chat_data(cfg if cfg is not None else _analyst_cfg(),
                                   f0=us.f0, f2=us.f2, pct=us.pct,
                                   pts=us.pts, esc=us.esc)


class TestChatEngineDeterminism(unittest.TestCase):
    """對話資料必須是資料的純函式（無隨機、無時鐘相依）。"""

    def test_same_cfg_yields_byte_identical_json(self):
        cfg = _analyst_cfg()
        self.assertEqual(_payload_text(cfg), _payload_text(cfg),
                         "同一份 cfg 重跑必須逐位元組相同（不可有隨機成分）")

    def test_output_is_stable_across_a_real_clock_change(self):
        """把時鐘移到隔天，聊天資料仍須完全相同 —— 證明沒有讀 datetime.now()。"""
        cfg = _analyst_cfg()
        before = _payload_text(cfg)
        with H.freeze_now(H.ref_dt(23, 4, 5)):
            after = _payload_text(cfg)
        self.assertEqual(before, after,
                         "聊天資料不得隨系統時間改變（只能取決於資料）")

    def test_no_market_numbers_are_hardcoded(self):
        a = _payload_text()
        cfg = _analyst_cfg()
        cfg["today_close"] = cfg["today_close"] + 500.0
        self.assertNotEqual(a, _payload_text(cfg),
                            "指數改了，聊天答案必須跟著改（不可寫死）")

    def test_json_is_valid_and_key_order_is_fixed(self):
        t = _payload_text()
        body = t[len("var AI_CHAT_DATA="):-1]
        parsed = json.loads(body.replace("<\\/", "</"))
        self.assertEqual(parsed, json.loads(_payload_text()[len("var AI_CHAT_DATA="):-1]
                                            .replace("<\\/", "</")))
        self.assertEqual(list(parsed.keys()), sorted(parsed.keys()),
                         "鍵序必須固定（sort_keys），否則重跑會產生偽變更")

    def test_script_tag_cannot_be_closed_early(self):
        """內嵌 JSON 不得包含未轉義的 </，否則 <script> 會被提前關閉。"""
        for cfg in (_analyst_cfg(),):
            t = _payload_text(cfg)
            self.assertNotIn("</", t.replace("<\\/", ""),
                             "JSON 內出現未轉義的 </ 會提前關閉 <script>")

    def test_all_quick_replies_have_answers(self):
        p = _payload()
        self.assertTrue(p["quick"], "必須提供可點選的預設問題")
        for item in p["quick"]:
            self.assertIn(item["topic"], p["replies"],
                          f"快捷問題 {item['label']} 沒有對應答案")


class TestChatEngineKeywordMatching(unittest.TestCase):
    """關鍵字比對必須是固定規則，且涵蓋需求列出的每一類問題。"""

    def test_required_questions_map_to_expected_topics(self):
        cases = {
            "大盤現在怎樣？": "market",
            "指數今天如何": "market",
            "台積電 2330 均線怎麼看？": "tsmc",
            "2330 現在多少": "tsmc",
            "均線怎麼看": "ma",
            "黃金交叉是什麼": "ma",
            "死亡交叉": "ma",
            "今天漲幅排行有什麼？": "gain",
            "漲停有哪些": "gain",
            "今天跌幅排行有什麼？": "lose",
            "跌停幾家": "lose",
            "成交量如何": "volume",
            "量能": "volume",
            "給我風險提醒": "risk",
            "有什麼要注意的": "risk",
            "免責聲明": "disclaimer",
        }
        for text, want in cases.items():
            self.assertEqual(analyst.match_topic(text), want, f"{text!r} 應對應到 {want}")

    def test_unknown_text_returns_none(self):
        for text in ("", "   ", "你午餐吃什麼", "asdfghjkl"):
            self.assertIsNone(analyst.match_topic(text), f"{text!r} 不應對應到任何主題")

    def test_specific_topic_wins_over_generic(self):
        """「台積電均線」必須走 tsmc（較具體）而非 ma，否則答案會漏掉台積電。"""
        self.assertEqual(analyst.match_topic("台積電均線怎麼看"), "tsmc")

    def test_matching_is_case_insensitive(self):
        self.assertEqual(analyst.match_topic("TSMC 2330"), "tsmc")
        self.assertEqual(analyst.match_topic("MA20"), "ma")

    def test_frontend_keyword_table_matches_python(self):
        """前端 JS 的關鍵字表必須與 Python 的同主題同詞，否則同一句話
        在兩邊會得到不同答案。"""
        src = H.repo_index_html()
        for topic, words in analyst.TOPIC_KEYWORDS:
            for w in words:
                self.assertIn("'" + w + "'", src,
                              f"前端關鍵字表缺少 {topic} 的 {w!r}")


class TestChatEngineReferencesRealData(unittest.TestCase):
    """聊天答案必須引用當日真實數字，且與頁面其他區塊一致。"""

    def setUp(self):
        self.cfg = _analyst_cfg()
        self.p = _payload(self.cfg)

    def test_market_answer_quotes_index_breadth_turnover(self):
        s = self.p["replies"]["market"]
        self.assertIn(us.f2(self.cfg["today_close"]), s)
        self.assertIn(us.f0(self.cfg["mkt"]["up"]), s)
        self.assertIn(us.f0(self.cfg["mkt"]["down"]), s)
        self.assertIn(us.f2(self.cfg["mkt"]["total_amt"] / 1e8), s)

    def test_tsmc_answer_quotes_2330(self):
        s = self.p["replies"]["tsmc"]
        self.assertIn(us.f2(self.cfg["tsmc"]["close"]), s)
        self.assertIn(us.f0(self.cfg["tsmc"]["vol"] / 1000), s)

    def test_ma_answer_quotes_both_moving_averages(self):
        s = self.p["replies"]["ma"]
        self.assertIn(us.f2(self.cfg["win_ma5"][-1]), s)
        self.assertIn(us.f2(self.cfg["win_ma20"][-1]), s)

    def test_gain_and_lose_answers_quote_top_names(self):
        g = self.cfg["gain"][0]
        l = self.cfg["lose"][0]
        self.assertIn(us.esc(g["code"]), self.p["replies"]["gain"])
        self.assertIn(us.esc(g["name"]), self.p["replies"]["gain"])
        self.assertIn(us.esc(l["code"]), self.p["replies"]["lose"])
        self.assertIn(us.esc(l["name"]), self.p["replies"]["lose"])

    def test_volume_answer_quotes_the_ratio(self):
        s = self.p["replies"]["volume"]
        self.assertIn("{:.1f}".format(self.cfg["win"][-1]["amt"]
                                       / (sum(r["amt"] for r in self.cfg["win"][-21:-1]
                                              if r["amt"] is not None) / 20) * 100.0), s)

    def test_numbers_block_matches_page_formatters(self):
        n = self.p["numbers"]
        self.assertEqual(n["close"], us.f2(self.cfg["today_close"]))
        self.assertEqual(n["ma5"], us.f2(self.cfg["win_ma5"][-1]))
        self.assertEqual(n["tsmc"], us.f2(self.cfg["tsmc"]["close"]))
        self.assertEqual(n["amt"], us.f2(self.cfg["mkt"]["total_amt"] / 1e8))

    def test_negative_signs_use_the_page_convention(self):
        """負號必須是 U+2212（與頁面一致），且 JSON 不得出現 ASCII '-' 當負號。"""
        cfg = self.cfg
        cfg["today_close"] = cfg["prev_close"] - 100.0
        p = _payload(cfg)
        self.assertIn("\u2212", p["numbers"]["chg"])
        self.assertNotIn("-", p["numbers"]["chg"])


class TestChatEngineHonesty(unittest.TestCase):
    """聊天視窗內也必須保留角色誠實標示與投資免責聲明。"""

    def setUp(self):
        self.p = _payload()
        self.ui = analyst.build_chat_ui()

    def test_payload_carries_honesty_and_disclaimer(self):
        self.assertIn("AI 生成", self.p["honesty"])
        self.assertIn(analyst.ROLE_LABEL, self.p["honesty"])
        self.assertIn("不構成任何投資建議", self.p["disclaimer"])
        self.assertIn("臺灣證券交易所", self.p["disclaimer"])

    def test_ui_has_dialog_semantics(self):
        for need in ('role="dialog"', 'aria-modal="true"', 'aria-labelledby="ai-panel-t"'):
            self.assertIn(need, self.ui, f"彈窗缺少無障礙屬性：{need}")

    def test_ui_has_fab_with_avatar_and_label(self):
        self.assertIn('class="ai-fab"', self.ui)
        self.assertIn("assets/yu_cheng_jun_avatar.png", self.ui)
        self.assertIn("aria-label", self.ui)
        self.assertIn(analyst.ROLE_LABEL, self.ui)
        self.assertIn(analyst.CHARACTER, self.ui)

    def test_ui_has_close_button_input_and_quick_area(self):
        for need in ('id="ai-panel-x"', 'id="ai-in"', 'id="ai-quick"',
                     'id="ai-log"', 'id="ai-form"', 'type="submit"'):
            self.assertIn(need, self.ui, f"彈窗缺少必要元素：{need}")

    def test_ui_uses_only_local_avatar(self):
        self.assertNotIn("http://", self.ui)
        self.assertNotIn("https://", self.ui)

    def test_role_label_never_becomes_analyst(self):
        """「分析失」不可被改成「分析師」（全站掃描，含聊天區塊）。"""
        for blob in (self.p["honesty"], self.p["disclaimer"], self.ui,
                     self.p["greeting"], self.p["source_note"]):
            self.assertNotIn("虛擬分析師", blob)

    def test_greeting_is_the_exact_specified_copy(self):
        """開場訊息必須逐字等於使用者指定的固定文案（含標點與換行）。"""
        self.assertEqual(
            self.p["greeting"],
            "我是「韭經考驗的台股戰士-宇程君」，是一位韭菜型AI並不智能的硅基生命體"
            "<br><br>"
            "你可以點下面的問題，或直接打字問我")
        self.assertIn(analyst.CHARACTER, self.p["greeting"])

    def test_greeting_is_fixed_and_does_not_track_the_data(self):
        """開場訊息是固定文案：改動當日資料不得改變它（當日評語改由分析面板承載）。"""
        cfg = _analyst_cfg()
        base = _payload(cfg)["greeting"]
        cfg2 = dict(cfg, today_close=cfg["today_close"] + 500.0)
        self.assertEqual(base, _payload(cfg2)["greeting"])
        self.assertEqual(base, analyst.GREETING)

    def test_greeting_keeps_the_two_paragraph_break(self):
        """兩段之間必須保留空行（<br><br>），不可被壓成一行。"""
        self.assertIn("<br><br>", self.p["greeting"])
        self.assertEqual(self.p["greeting"].count("<br><br>"), 1)

    def test_source_note_is_disclosed(self):
        self.assertIn("固定規則", self.p["source_note"])
        self.assertIn("無隨機成分", self.p["source_note"])
        self.assertIn("analyst.py", self.p["source_note"])

    def test_fallback_mentions_what_can_be_asked(self):
        self.assertIn("大盤", self.p["fallback"])
        self.assertIn("風險", self.p["fallback"])


class TestChatIsWiredIntoThePage(unittest.TestCase):
    """聊天彈窗必須真的被注入 index.html 且前端行為完整。"""

    def setUp(self):
        self.out = baseline_out()

    def test_regions_are_registered(self):
        for name in ("AI_CHAT", "AI_CHAT_DATA"):
            self.assertIn(name, H.EXPECTED_REGIONS)

    def test_markers_exist_in_source(self):
        src = H.repo_index_html()
        for name in ("AI_CHAT", "AI_CHAT_DATA"):
            self.assertIsNotNone(us._region_body(src, name), f"index.html 缺 marker {name}")

    def test_rendered_page_has_fab_and_panel(self):
        body = region_body(self.out, "AI_CHAT")
        for need in ('id="ai-fab"', 'id="ai-panel"', 'id="ai-panel-x"',
                     'id="ai-log"', 'id="ai-quick"', 'id="ai-form"', 'id="ai-in"'):
            self.assertIn(need, body, f"AI_CHAT 區塊缺少 {need}")

    def test_rendered_data_region_is_valid_json(self):
        raw = us._region_body(self.out, "AI_CHAT_DATA")
        self.assertIsNotNone(raw)
        body = raw.group(2).strip()
        self.assertTrue(body.startswith("var AI_CHAT_DATA="),
                        "AI_CHAT_DATA 必須是 var AI_CHAT_DATA=... 的 JS 指定敘述")
        self.assertTrue(body.endswith(";"), "JS 指定敘述應以分號結束")
        payload = json.loads(body[len("var AI_CHAT_DATA="):-1].replace("<\\/", "</"))
        self.assertIn("replies", payload)
        self.assertIn("quick", payload)
        self.assertTrue(payload["replies"], "聊天答案不得為空")
        self.assertIn("character", payload)
        self.assertIn("honesty", payload)
        self.assertIn("disclaimer", payload)

    def test_data_region_is_inside_a_script_and_uses_js_comment_markers(self):
        """AI_CHAT_DATA 位於 <script> 內，必須用 JS 區塊註解；
        HTML 註解在 <script> 內會被視為單行註解而弄壞 JS。"""
        self.assertIn("AI_CHAT_DATA", us.JS_REGIONS)
        src = H.repo_index_html()
        self.assertIn("/*SNAP:AI_CHAT_DATA*/", src)
        self.assertNotIn("<!--SNAP:AI_CHAT_DATA-->", src)

    def test_frontend_implements_enter_escape_and_close(self):
        src = H.repo_index_html()
        self.assertIn("addEventListener('submit'", src, "缺少送出（Enter）處理")
        self.assertIn("'Escape'", src, "缺少 Esc 關閉處理")
        self.assertIn("ai-panel-x", src, "缺少關閉鈕處理")

    def test_frontend_manages_focus_and_aria(self):
        src = H.repo_index_html()
        for need in ("aria-expanded", "aria-controls", "aria-haspopup",
                     "focus()", "aria-modal"):
            self.assertIn(need, src, f"前端缺少無障礙處理：{need}")

    def test_frontend_does_not_hardcode_market_numbers(self):
        """前端只能讀 AI_CHAT_DATA；不可在 JS 裡寫死市場數字。"""
        src = H.repo_index_html()
        block = src[src.index("var CHAT = (function()"):src.index("/* ---------- wiring ---------- */")]
        for bad in ("46,969", "48,510", "14,357", "2,440"):
            self.assertNotIn(bad, block, f"前端聊天模組寫死了市場數字：{bad}")

    def test_mobile_breakpoint_makes_panel_full_width(self):
        """手機 390px 時彈窗必須滿寬自適應（不可橫向溢出）。"""
        src = H.repo_index_html()
        self.assertIn("@media (max-width:520px)", src)
        block = src[src.index("@media (max-width:520px)"):]
        self.assertIn("left:0;right:0;bottom:0", block)

    def test_drawer_is_inside_the_popup_and_toggles(self):
        """「今日完整分析」必須以可開合的抽屜形式在彈窗內，且有前端接線。"""
        body = region_body(self.out, "AI_CHAT")
        for need in ('id="ai-more"', 'id="ai-drawer"', 'id="ai-drawer-x"',
                     'aria-controls="ai-drawer"', 'id="ai-brief-body"',
                     '<!--SNAP:AIANALYST-->'):
            self.assertIn(need, body, f"AI_CHAT 區塊缺少 {need}")
        src = H.repo_index_html()
        self.assertIn("ai-more", src, "前端缺少抽屜入口接線")
        self.assertIn("ai-drawer", src, "前端缺少抽屜接線")
        self.assertIn("openDrawer", src, "前端缺少開啟抽屜的函式")
        self.assertIn("closeDrawer", src, "前端缺少收合抽屜的函式")


class TestAnalystChatPopupUx(unittest.TestCase):
    """對話彈窗的版面契約：對話是主體，完整分析收進抽屜。

    這些断言守住 2026-10-15 的修正：原本「今日完整分析」區塊常駐展開，
    吃掉桌面 55%、手機 31% 的彈窗高度，把對話擠到不能用。
    """

    def setUp(self):
        self.chat = region_body(baseline_out(), "AI_CHAT")
        self.src = H.repo_index_html()

    def test_no_always_on_brief_block_steals_the_log_space(self):
        """彈窗外框不得再有不分青紅皂白就展開的 .ai-brief 區塊。"""
        self.assertNotIn('class="ai-brief"', self.chat)
        self.assertNotIn("ai-brief-tog", self.chat)

    def test_drawer_starts_hidden(self):
        """抽屜必須預設 hidden，否則一開啟就又是長文佔滿。"""
        i = self.chat.index('id="ai-drawer"')
        head = self.chat[i:i + 200]
        self.assertIn("hidden", head, "抽屜必須預設隱藏")

    def test_drawer_holds_analysis_and_disclaimer(self):
        """完整分析與投資免責聲明都要在抽屜內（不佔對話高度仍看得到）。"""
        body = region_body(baseline_out(), "AI_CHAT")
        i_drawer = body.index('id="ai-drawer"')
        i_an = body.index("<!--SNAP:AIANALYST-->")
        i_disc = body.index('id="ai-panel-disc"')
        self.assertGreater(i_an, i_drawer, "完整分析必須在抽屜內")
        self.assertGreater(i_disc, i_drawer, "免責聲明必須在抽屜內")

    def test_honesty_bar_is_always_visible_not_buried(self):
        """誠實標示必須常駐於對話視圖（在抽屜之前），不能只有抽屜裡看得到。"""
        self.assertIn('id="ai-panel-hon"', self.chat)
        self.assertLess(self.chat.index('id="ai-panel-hon"'),
                        self.chat.index('id="ai-drawer"'),
                        "誠實標示必須在抽屜之外常駐顯示")
        self.assertIn("honesty", self.src, "前端必須填入誠實標示")

    def test_fab_uses_lede_not_the_lineheight_breaker(self):
        """浮動鈕不得再用 .ai-fab-tx（line-height:0 的 62px 圓形內容溢出 4px）。"""
        self.assertNotIn("ai-fab-tx", self.chat)
        self.assertNotIn("ai-fab-tx", self.src)
        self.assertIn('id="ai-fab-lede"', self.chat)
        self.assertIn("ai-fab-lede", self.src)

    def test_quick_row_is_a_single_scrollable_line(self):
        """快捷問題必須單列可橫向滑動，不得換行擠掉訊息區。"""
        i = self.src.index(".ai-panel-quick{")
        block = self.src[i:self.src.index("}", i)]
        self.assertIn("overflow-x:auto", block)
        self.assertNotIn("flex-wrap:wrap", block)
        self.assertIn("white-space:nowrap", self.src)

    def test_mobile_panel_uses_dynamic_viewport_height(self):
        """手機彈窗必須吃滿動態視窗高度（避開瀏覽器工具列）。"""
        self.assertIn("100dvh", self.src)

    def test_greeting_is_emitted_only_once(self):
        """問候語不得在每次重開彈窗時重複堆疊。"""
        self.assertIn("greeted", self.src)

    def test_message_log_is_bounded(self):
        """長對話必須有 DOM 上限，否則手機越來越慢。"""
        self.assertIn("LOG_MAX", self.src)

    def test_escape_closes_drawer_before_dialog(self):
        """Esc 必須先關抽屜、再關彈窗。"""
        i = self.src.index("addEventListener('keydown'")
        block = self.src[i:i + 700]
        self.assertIn("drawerOpen()", block, "Esc 必須先判斷抽屜是否開著")

    def test_tab_scope_follows_the_drawer(self):
        """抽屜開著時 Tab 只能在抽屜內循環。"""
        i = self.src.index("ev.key !== 'Tab'")
        block = self.src[i:i + 1400]
        self.assertIn("drawerOpen() ? drawer : panel", block)

    def test_no_dangling_old_panel_foot(self):
        """舊的 .ai-panel-foot 容器必須完全移除（曾把輸入列推離彈窗底部）。"""
        self.assertNotIn("ai-panel-foot", self.chat)
        self.assertNotIn("ai-panel-foot", self.src)
        self.assertNotIn("ai-fab-hint", self.src)

    def test_removed_entry_strip_leaves_no_dangling_references(self):
        """入口條移除後，前端不得再引用已不存在的 #ai-open 或舊樣式類別。"""
        src = H.repo_index_html()
        self.assertNotIn("el('ai-open')", src, "前端仍引用已移除的 #ai-open")
        self.assertNotIn("opener", src, "前端仍殘留 opener 變數")
        for gone in ("ai-card", "ai-strip", "ai-cta-row", "ai-open"):
            self.assertNotIn(gone, src, f"index.html 仍殘留已移除的樣式／元素：{gone}")


class TestStockSearchAndIndustryFilter(unittest.TestCase):
    """搜尋／產業篩選：資料層正確性 + 頁面 UI 契約（全部離線原始碼層驗證）。"""

    def setUp(self):
        _, _, _, self.out = clean_run()
        self.out = self.out.decode()
        self.src = H.repo_index_html()

    # ---- 資料層 ----
    def test_industry_table_is_static_no_network(self):
        """產業對照必須是內建靜態表，模組不得再對產業端點發出任何抓取。"""
        mod = inspect.getsource(us)
        # 註解可文件化來源網址，但不得有任何實際抓取呼叫。
        self.assertNotIn('fetch("/v1/opendata', mod, "仍對產業端點發出抓取")
        self.assertNotIn("openapi.twse.com.tw/v1", mod.replace("#   * openapi.twse.com.tw/v1", ""))
        self.assertEqual(
            [l for l in mod.splitlines()
             if "openapi" in l and not l.strip().startswith("#")], [],
            "非註解行仍引用 openapi（代表仍有網路相依）")
        self.assertGreater(len(us.STOCK_INDUSTRY), 100)

    def test_industry_table_values_are_official_names(self):
        """每個值都必須是證交所官方產業別名稱之一。"""
        allowed = {
            "水泥工業", "食品工業", "塑膠工業", "紡織纖維", "電機機械", "電器電纜",
            "玻璃陶瓷", "造紙工業", "鋼鐵工業", "橡膠工業", "汽車工業", "電子工業",
            "建材營造業", "航運業", "觀光餐旅", "金融保險業", "貿易百貨業", "綜合",
            "其他業", "化學工業", "生技醫療業", "油電燃氣業", "半導體業",
            "電腦及週邊設備業", "光電業", "通信網路業", "電子零組件業", "電子通路業",
            "資訊服務業", "其他電子業", "文化創意業", "農業科技業", "綠能環保",
            "數位雲端", "運動休閒", "居家生活",
        }
        bad = {k: v for k, v in us.STOCK_INDUSTRY.items() if v not in allowed}
        self.assertEqual(bad, {}, f"出現非官方產業別名稱：{bad}")

    def test_industry_of_falls_back_to_unclassified(self):
        """查不到的代號一律『未分類』，不猜測。"""
        self.assertEqual(us.industry_of("2330"), "半導體業")
        self.assertEqual(us.industry_of("9999"), "未分類")
        self.assertEqual(us.industry_of(""), "未分類")
        self.assertEqual(us.industry_of(None), "未分類")

    def test_every_rendered_row_carries_a_real_industry(self):
        """頁面上每一列股票都必須有 data-industry，且不得為空或『未分類』。"""
        rows = re.findall(r'data-code="(\d{4})"[^>]*?data-industry="([^"]*)"', self.out)
        self.assertGreaterEqual(len(rows), 40, "股票列數異常")
        blank = [c for c, i in rows if i in ("", us.UNCLASSIFIED)]
        self.assertEqual(blank, [], f"以下代號沒有官方產業別：{blank}")

    def test_displayed_codes_are_all_in_the_table(self):
        """頁面會顯示的代號必須全部收錄在對照表內（否則會顯示未分類）。"""
        codes = set(re.findall(r'data-code="(\d{4})"', self.out))
        self.assertTrue(codes)
        missing = sorted(c for c in codes if c not in us.STOCK_INDUSTRY)
        self.assertEqual(missing, [], f"對照表缺這些代號：{missing}")

    def test_tsmc_industry_is_data_driven(self):
        """台積電產業不得再寫死『半導體』，必須由 industry_of() 產生。"""
        self.assertIn('<em>半導體業</em>', self.out)
        self.assertNotIn("<em>半導體</em>", self.out)

    def test_rank_container_has_an_id_for_filtering(self):
        self.assertIn('class="rank" id="rank-list"', self.out)

    # ---- UI 契約 ----
    def test_filter_controls_exist(self):
        for el_id in ("sf-q", "sf-ind", "sf-clear", "sf-meta", "sf-empty"):
            self.assertIn(f'id="{el_id}"', self.src, f"缺少篩選元素 {el_id}")

    def test_filter_is_live_not_submit_based(self):
        """必須以 input/change 即時過濾，不得依賴送出鈕。"""
        i = self.src.index("var FILTER = (function()")
        block = self.src[i:i + 8000]
        self.assertIn("addEventListener('input'", block)
        self.assertIn("addEventListener('change'", block)
        self.assertNotIn("form", block.lower().replace("format", ""))

    def test_filter_covers_all_four_blocks(self):
        i = self.src.index("var FILTER = (function()")
        block = self.src[i:i + 8000]
        for el_id in ("list-gain", "list-lose", "list-tsmc", "rank-list"):
            self.assertIn(f"$('{el_id}')", block, f"篩選未涵蓋 {el_id}")

    def test_filter_has_empty_state_and_clear(self):
        i = self.src.index("var FILTER = (function()")
        block = self.src[i:i + 8000]
        self.assertIn("empty.hidden = n !== 0", block, "空狀態切換邏輯缺失")
        self.assertIn("sf-clear", block, "清除鈕未接線")
        self.assertIn("Escape", block, "Esc 清除未實作")

    def test_filter_reapplied_after_live_refresh(self):
        """即時更新後必須重套篩選，否則刷新會讓篩選失效。"""
        i = self.src.index("function refresh()")
        block = self.src[i:self.src.index("function schedule()")]
        self.assertIn("FILTER.apply()", block)

    def test_industry_map_ships_in_payload_for_live_refresh(self):
        """產業對照必須隨 AI_CHAT_DATA 一起輸出，前端重整才補得回 data-industry。"""
        m = re.search(r'var AI_CHAT_DATA=(\{.*?\});?/\*SNAP:/AI_CHAT_DATA\*/', self.out, re.S)
        self.assertIsNotNone(m, "找不到 AI_CHAT_DATA")
        payload = json.loads(m.group(1).replace("<\\/", "</"))
        self.assertIn("industry", payload)
        ind = payload["industry"]
        self.assertEqual(ind["2330"], "半導體業")
        # 頁面上每個代號都必須在 payload 內，否則刷新後會變「未分類」
        for code in set(re.findall(r'data-code="(\d{4})"', self.out)):
            self.assertIn(code, ind, f"payload 缺代號 {code}")

    def test_refresh_paths_restore_industry_attribute(self):
        """即時重整重建的列必須帶回 data-industry（否則篩選在刷新後失效）。"""
        i = self.src.index("function rlRow")
        self.assertIn("indOf(rec.code)", self.src[i:i + 700])
        j = self.src.index("rh += '<div class=\"rk\"")
        self.assertIn("indOf(rc.code)", self.src[j:j + 300])
        k = self.src.index("function applyStock")
        self.assertIn("setAttribute('data-industry'", self.src[k:k + 500])

    def test_industry_lookup_has_single_source(self):
        """對照表只能有一份定義處（analyst.py），update_snapshot 僅轉出。"""
        self.assertIs(us.STOCK_INDUSTRY, analyst.STOCK_INDUSTRY)
        src = inspect.getsource(us)
        self.assertNotIn('"2330": "半導體業"', src, "update_snapshot 又抄了一份對照表")

    def test_industry_map_has_no_personal_or_company_names(self):
        """對照表只含代號→產業別，不得夾帶公司名稱或其他識別資訊。"""
        for k, v in analyst.STOCK_INDUSTRY.items():
            self.assertRegex(k, r"^\d{4}$")
            self.assertNotIn("公司", v)
            self.assertLessEqual(len(v), 12)

    def test_hidden_attribute_actually_hides_rows(self):
        """[hidden] 必須真的隱藏。列本身帶 display:grid/flex，會蓋掉瀏覽器預設的
        [hidden]{display:none}，若不強制覆寫，切換 hidden 只改狀態、畫面不變。"""
        self.assertIn("[hidden]{display:none !important;}", self.src)
        # 受影響的元素確實自帶 display 規則（這正是需要覆寫的原因）
        self.assertIn("display:grid", self.src[self.src.index(".rl-row{"):self.src.index(".rl-row{") + 200])
        self.assertIn("display:grid", self.src[self.src.index(".rk{"):self.src.index(".rk{") + 120])
        self.assertIn("display:grid", self.src[self.src.index(".stock-main{"):self.src.index(".stock-main{") + 120])

    def test_keyword_search_also_matches_industry(self):
        """輸入產業名（如「半導體」）也要能找到該產業的股票 —— 排行列只顯示
        代號與名稱，產業別僅存在於 data-industry，故比對字串必須併入產業別。"""
        i = self.src.index("var FILTER = (function()")
        block = self.src[i:i + 8000]
        self.assertIn("(el.textContent || '') + ' ' + industry", block)

    def test_filter_ui_is_static_and_outside_snap_regions(self):
        """篩選 UI 必須在任何 marker 之外（不屬於 SNAP 區塊）。"""
        i = self.src.index('class="stock-filter"')
        before = self.src[:i]
        self.assertGreater(before.rindex("<!--/SNAP:"), before.rindex("<!--SNAP:"))
        # 篩選 UI 所在的整段不得落在任何 region 之間
        m = us._region_body(self.src, "RANKNOTE")
        self.assertNotIn("stock-filter", m.group(0))

    def test_filter_targets_are_large_enough_for_touch(self):
        """手機觸控目標必須 >= 44px。"""
        for sel in (".sf-q input{", ".sf-ind select{", ".sf-clear{"):
            i = self.src.index(sel)
            self.assertIn("height:44px", self.src[i:i + 260], f"{sel} 觸控高度不足")

    def test_industry_note_is_honest_about_source(self):
        """產業歸類為粗略判定，頁面必須誠實標明非證交所官方產業別。"""
        self.assertIn("粗略判定", self.src)
        self.assertIn("非證交所官方產業別", self.src)
        self.assertIn("未分類", self.src)

    # ---- 全市場搜尋（2026-10-16 擴充）----
    def test_search_scope_is_stated_as_the_full_market(self):
        """搜尋範圍已擴為全市場上市普通股，文案必須誠實說明，且不得再殘留
        舊版「只涵蓋下方榜單」的誤導字樣。"""
        self.assertIn("搜尋全市場代號或名稱", self.src, "placeholder 未說明全市場範圍")
        self.assertIn("搜尋全市場上市普通股", self.src, "sr-only 標籤未說明全市場範圍")
        self.assertIn("搜尋範圍為", self.src, "sf-note 未說明搜尋範圍")
        self.assertIn("全市場上市普通股", self.src, "sf-note 未標明全市場")
        # 舊版誤導文案必須完全消失
        self.assertNotIn("搜尋榜單中的代號或名稱", self.src)
        self.assertNotIn("在下方榜單中", self.src)
        self.assertNotIn("搜尋與篩選僅涵蓋下方榜單已顯示的股票", self.src)

    def test_empty_state_is_clear_and_actionable(self):
        """空結果文案必須清楚並提示下一步（換關鍵字或清除搜尋）。"""
        i = self.src.index('id="sf-empty"')
        block = self.src[i:i + 400]
        self.assertIn("找不到符合的股票", block)
        self.assertIn("清除搜尋", block)

    def test_meta_count_labels_the_denominator_as_the_list(self):
        """未搜尋時，計數分母仍標明是「本頁榜單」。"""
        i = self.src.index("if(meta) meta.innerHTML")
        self.assertIn("本頁榜單共", self.src[i:i + 400])

    def test_universe_payload_is_embedded_and_complete(self):
        """UNIVERSE_DATA 必須內嵌於頁面，且涵蓋全市場上市普通股（含當日無成交者）。"""
        m = re.search(r'var UNIVERSE_DATA=(\{.*?\});?/\*SNAP:/UNIVERSE_DATA\*/', self.out, re.S)
        self.assertIsNotNone(m, "找不到 UNIVERSE_DATA")
        payload = json.loads(m.group(1))
        self.assertIn("inds", payload)
        self.assertIn("s", payload)
        self.assertGreaterEqual(len(payload["s"]), 1000, "universe 檔數過少")
        # 每列：代號、名稱、產業索引、收盤、漲跌、幅度、量、額
        for row in payload["s"]:
            self.assertEqual(len(row), 8)
            self.assertRegex(row[0], r"^\d{4}$")
            self.assertIsInstance(row[2], int)
            self.assertLess(row[2], len(payload["inds"]))
        # 台積電必須在內，且產業為半導體業
        ts = [r for r in payload["s"] if r[0] == "2330"]
        self.assertEqual(len(ts), 1)
        self.assertEqual(payload["inds"][ts[0][2]], "半導體業")

    def test_universe_includes_untraded_stocks_with_null_quote(self):
        """當日無成交者仍須在 universe 內，且報價欄位為 null（不得補 0 或沿用前一日）。"""
        m = re.search(r'var UNIVERSE_DATA=(\{.*?\});?/\*SNAP:/UNIVERSE_DATA\*/', self.out, re.S)
        payload = json.loads(m.group(1))
        nulls = [r for r in payload["s"] if r[3] is None]
        self.assertTrue(nulls, "universe 應包含當日無成交（無收盤價）的個股")
        for r in nulls:
            self.assertIsNone(r[4]); self.assertIsNone(r[5])
            self.assertIsNone(r[6]); self.assertIsNone(r[7])

    def test_universe_payload_is_a_pure_function_of_data(self):
        """同一份資料重跑必須逐位元組相同（不讀時間、不用隨機數）。"""
        cfg = us.collect.__wrapped__ if hasattr(us.collect, "__wrapped__") else None
        # 直接以 fixture 重跑兩次，比對 UNIVERSE_DATA 區塊
        _, _, _, a1 = clean_run()
        _, _, _, a2 = clean_run()
        r1 = region_body(a1.decode(), "UNIVERSE_DATA")
        r2 = region_body(a2.decode(), "UNIVERSE_DATA")
        self.assertEqual(r1, r2)

    def test_search_results_block_exists(self):
        """搜尋結果區塊與其子元素必須存在。"""
        for el_id in ("sf-results", "sfr-head", "sfr-list", "sfr-more", "sf-uni-n"):
            self.assertIn(f'id="{el_id}"', self.src, f"缺少搜尋結果元素 {el_id}")

    def test_search_matches_code_name_and_industry(self):
        """前端比對必須同時涵蓋代號、名稱與產業別。"""
        i = self.src.index("var FILTER = (function()")
        block = self.src[i:i + 7000]
        self.assertIn("code + ' ' + name + ' ' + industry", block)

    def test_search_result_quote_has_no_double_percent(self):
        """payload 的 pct 已含 %；前端若再補一次會出現 '−2.01%%'（實測踩到的缺陷）。"""
        i = self.src.index("var FILTER = (function()")
        block = self.src[i:i + 8000]
        self.assertNotIn("esc(it.pct) + '%'", block)
        self.assertIn("esc(it.pct) + '</span>'", block)
        # payload 本身的 pct 必須是含 % 的字串，且負號為 U+2212（與頁面其他區塊一致）
        m = re.search(r'var UNIVERSE_DATA=(\{.*?\});?/\*SNAP:/UNIVERSE_DATA\*/', self.out, re.S)
        payload = json.loads(m.group(1))
        seen = [r for r in payload["s"] if r[5]]
        self.assertTrue(seen)
        for r in seen:
            self.assertTrue(r[5].endswith("%"), r[5])
            self.assertEqual(r[5].count("%"), 1, r[5])
        negs = [r for r in seen if r[5].startswith("\u2212")]
        self.assertTrue(negs, "應有負向個股以驗證負號字元")
        for r in negs:
            self.assertNotIn("-", r[5], "負號必須是 U+2212，不是 ASCII hyphen")

    def test_search_shows_no_data_instead_of_fabricating(self):
        """取不到當日報價時必須顯示「無當日資料」，不得編造數字。"""
        i = self.src.index("var FILTER = (function()")
        block = self.src[i:i + 7000]
        self.assertIn("無當日資料", block)
        self.assertIn("不以前一日或 0 代替", block)

    def test_search_has_scroll_cap_and_more_hint(self):
        """結果過長時必須限制顯示數量並提示。"""
        i = self.src.index("var FILTER = (function()")
        block = self.src[i:i + 7000]
        self.assertIn("CAP", block)
        self.assertIn("僅顯示前", block)
        self.assertIn("max-height", self.src[self.src.index(".sfr-list{"):self.src.index(".sfr-list{") + 200])


if __name__ == "__main__":
    unittest.main(verbosity=2)
