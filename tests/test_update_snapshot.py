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
import io
import json
import os
import re
import socket
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import _harness as H  # noqa: E402
import update_snapshot as us  # noqa: E402

PATH_STOCK, PATH_MI, PATH_FMT, PATH_5MIN = (
    H.PATH_STOCK, H.PATH_MI, H.PATH_FMT, H.PATH_5MIN)
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
    return out + [os.path.join("fmtqik", f"{m}.json") for m in H.meta()["months"]]


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
    def test_meta_lists_fourteen_months(self):
        m = H.meta()
        self.assertEqual(len(m["months"]), 14)
        self.assertRegex(m["data_date"], r"^\d{4}/\d{2}/\d{2}$")

    def test_every_fixture_file_present(self):
        for rel in fixture_files():
            self.assertTrue(os.path.exists(H.fixture_path(rel)), f"缺少樣本 {rel}")

    def test_stock_day_all_shape(self):
        rows = list(csv.reader(io.StringIO(H.read_fixture("stock_day_all.csv"))))
        self.assertGreaterEqual(len(rows), 500)
        self.assertEqual(rows[1][0], "1150924")
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
        self.assertEqual(h[-1]["date"], H.data_date())
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
        self.assertIn("1150924", txt)

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

    def test_index_history_requests_fourteen_months(self):
        """月份推算必須與樣本一致，否則樣本無法重播。"""
        with H.freeze_now(datetime(2026, 9, 24, 21, 0, 0, tzinfo=TZ)):
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
# 6. 正常交易日：24 組區塊全部更新、輸出結構完整
# ==========================================================================
class TestNormalTradingDay(unittest.TestCase):
    def setUp(self):
        self.out = baseline_out()

    # ---- 24 組 marker 全部由本次資料重建 ------------------------------
    def test_all_24_regions_are_rewritten_from_live_data(self):
        """把 24 個區塊內容全換成哨兵，重跑後哨兵必須全部消失。"""
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

    def test_expected_region_set_is_24(self):
        self.assertEqual(len(H.EXPECTED_REGIONS), 24)

    def test_source_has_all_24_markers(self):
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
        self.assertGreater(len(self.out), 80_000)
        self.assertLess(len(self.out), 250_000)

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
        s = ref_market_stats()
        self.assertEqual(s["up"] + s["down"] + s["flat"], 1072)

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
        # 比較圖 = 漲幅前十 + 2330 台積電 + 跌幅前十（共 21 筆）。
        # 全圖依漲跌幅「遞減」排序，故跌幅段為跌幅榜（由低到高）的反序。
        codes = parse_rankbar_codes(self.out)
        want = ([x["code"] for x in ref_top10()] + ["2330"]
                + [x["code"] for x in reversed(ref_bottom10())])
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
        """script 內的 marker 必須是 /* */，否則 JS 會被註解掉。"""
        src = H.repo_index_html()
        script = re.search(r"<script>(.*?)</script>", src, re.S).group(1)
        self.assertNotIn("<!--SNAP:", script)
        for name in sorted(us.JS_REGIONS):
            self.assertIn(f"/*SNAP:{name}*/", script)
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
        self.assertLessEqual(src.count("<link"), 1)


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
        frozen = datetime(2026, 9, 24, 20, 30, 0, tzinfo=TZ)
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
        frozen = datetime(2026, 9, 24, 21, 15, 30, tzinfo=TZ)
        stamp = frozen.strftime("%Y/%m/%d %H:%M:%S")
        with H.FakeAPI(), H.freeze_now(frozen):
            r = H.run_cli(["--file", p])
        self.assertEqual(r.rc, 0, f"{r.out}\n{r.err}")
        self.assertEqual(r.changed, "1", f"資料已變應為 CHANGED=1\n{r.out}")
        self.assertIn("本次擷取時間（資料已變更）", r.out)
        out = open(p, encoding="utf-8").read()
        self.assertIn(f"var SNAPSHOT_AT = '{stamp}';", out)
        self.assertIn(stamp, out)
        self.assertIn("48,024.60", out, "應以本次抓到的真實數值覆蓋錯誤值")

    def test_force_meta_writes_timestamp_without_data_change(self):
        _, p, before, _ = clean_run()
        frozen = datetime(2026, 9, 24, 22, 5, 9, tzinfo=TZ)
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
        self.assertEqual(len(got), 14)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
