"""產生 tests/fixtures/baseline_index.html。

用途
----
測試需要一份「已被 fixture 資料完整渲染過」的 index.html 當作基準，
才能驗證「資料沒變就不得寫檔」這類性質。

若直接拿 repo 根目錄的 index.html 當基準，測試就會和「線上頁面目前
的資料日期」綁死：
  · 排程把 index.html 更新到比 fixture 新的一天後，
  · 測試仍然期待「跑一次不會改動檔案」，
  · 於是測試失敗 → CI 紅燈 → update job 被 needs 擋住 → 排程從此不再更新。
這正是 2026/10/16 之後每日快照停擺的原因。

改成凍結基準後，基準只隨 fixture 變動，與線上頁面的資料日期無關。

重建時機
--------
當 tests/fixtures/ 的樣本被重新擷取（capture_fixtures.py）之後執行本檔，
並把產出的 baseline_index.html 一併提交。
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import _harness as H  # noqa: E402


BASELINE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", "baseline_index.html")


def main() -> int:
    d = tempfile.mkdtemp(prefix="twstock-baseline-")
    path = os.path.join(d, "index.html")
    # 以目前 repo 的 index.html 當「骨架」：只取結構（marker / CSS / 前端 JS），
    # 內容稍後會被 fixture 資料完整覆寫。
    H.write_html(path, H.repo_index_html())

    # 18:42:11 刻意避開測試中用到的 20:30、21:15:30、22:05:09、23:04:05，
    # 否則 --force-meta 測試會寫入相同時間戳而「看起來沒變」而失敗。
    with H.FakeAPI(), H.freeze_now(H.ref_dt(18, 42, 11)):
        r = H.run_cli(["--file", path])
    if r.rc != 0:
        print(r.out)
        print(r.err, file=sys.stderr)
        return 1

    # 冪等性檢查：同一份資料再跑一次必須 CHANGED=0 且不改動檔案。
    before = H.read_bytes(path)
    with H.FakeAPI(), H.freeze_now(H.ref_dt(18, 42, 11)):
        r2 = H.run_cli(["--file", path])
    if r2.rc != 0 or before != H.read_bytes(path):
        print("基準不是冪等的：再跑一次仍改動了檔案", file=sys.stderr)
        print(r2.out)
        print(r2.err, file=sys.stderr)
        return 1

    shutil.copyfile(path, BASELINE)
    with open(BASELINE, "rb") as f:
        n = len(f.read())
    print(f"wrote {BASELINE} ({n} bytes, idempotent)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
