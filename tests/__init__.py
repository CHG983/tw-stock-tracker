# tests 套件標記，讓 `python -m unittest discover -s tests -t .` 可以正常匯入。
# 測試本身仍以 sys.path 直接匯入 _harness 與 update_snapshot，不依賴套件相對匯入。
