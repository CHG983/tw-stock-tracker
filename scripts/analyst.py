#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyst.py — 「韭經考驗的台股戰士-宇程君」每日市場評論產生器。

角色定位（務必誠實）
--------------------
宇程君是 **AI 生成的虛擬角色**，頁面標示其角色為「韭齡多年的虛擬分析失」；不是真人。他的每一句話都由本檔案
依 `update_snapshot.py` 自臺灣證券交易所（TWSE）公開資料抓到的 **當日真實數字**，
以固定規則（門檻判斷 + 模板）產生：

  * 沒有隨機成分（no random、no time、no 網路之外的輸入）→ 同一份資料重跑，
    產出的文字逐字元相同，因此可重現、可單元測試。
  * 沒有寫死的市場數字 → 資料不同，評語就不同；每日快照更新時文字會跟著變。
  * 所有數字一律經由呼叫端注入的格式化函式輸出，確保與頁面其他數字使用
    相同的千分位與正負號慣例（負號為 U+2212）。

本模組刻意 **不 import update_snapshot**（避免循環匯入），改用依賴注入傳入
`f0 / f2 / pct / pts / esc`。

頁面用語
--------
「市場氛圍分數」是本頁自訂的啟發式指標（0–100），由四項可驗證的分項加權而成，
權重與分項值都直接顯示在頁面上，使用者可自行複核。它不是市場共識、不是機率、
也不是預測值。
"""
from __future__ import annotations

from collections import Counter

CHARACTER = "韭經考驗的台股戰士-宇程君"

# 角色標籤：頁面、README 與測試共用這一個字串，確保全站一致。
# 【務必照原樣使用】「分析失」為使用者指定的諧音寫法，不可自行改成「分析師」。
ROLE_LABEL = "韭齡多年的虛擬分析失"

# --------------------------------------------------------------------------
# 族群歸類：**依股票代號區間**做的粗略歸類（非 TWSE 官方產業別）
# --------------------------------------------------------------------------
# 說明：TWSE 官方產業別需另抓 t187ap03_L 端點，本頁不引入該依賴。此處僅用
# 代號區間做大分類，且頁面上明確標示為「依代號區間粗略判定」。同一個代號
# 只會命中第一個符合的區間，因此規則順序即優先序。
SECTOR_BY_CODE: tuple[tuple[int, int, str], ...] = (
    (1000, 1999, "傳產（水泥／食品／塑膠／紡織／電機／化學／鋼鐵等）"),
    (2000, 2499, "電子（半導體、電腦及週邊）"),
    (2500, 2599, "營建"),
    (2600, 2699, "航運"),
    (2700, 2799, "觀光餐旅"),
    (2800, 2899, "金融保險"),
    (2900, 2999, "貿易百貨"),
    (3000, 3999, "電子（光電、零組件、網通）"),
    (4000, 4199, "生技醫療"),
    (4900, 6999, "電子（其他）"),
    (8000, 8999, "電子通路"),
)

# 氛圍分數的四個分項與權重（總和 = 1.0）
MOOD_WEIGHTS = {"index": 0.35, "breadth": 0.30, "volume": 0.20, "ma": 0.15}

# 氛圍等級：(分數下限, CSS 類別, 顯示名稱)，由高至低
MOOD_TIERS: tuple[tuple[float, str, str], ...] = (
    (80.0, "hot", "熱到燙手"),
    (65.0, "warm", "偏多樂觀"),
    (45.0, "mid", "多空拉鋸"),
    (30.0, "cool", "偏空保守"),
    (0.0, "cold", "冷到結霜"),
)

# 口頭禪：每個氛圍等級三句，以分數取模決定用哪一句（仍為純決定性）
CATCHPHRASES: dict[str, tuple[str, ...]] = {
    "hot": (
        "熱到燙手！我這種被套牢出身的，看到整片紅反而先摸摸口袋還在不在。",
        "大家都在喊上車，我先把安全帶繫好——雲霄飛車我搭太多次了。",
        "這種行情最容易讓人忘記風險。帳面好看歸好看，停損還是要掛。",
    ),
    "warm": (
        "紅通通的一片，看久了眼睛都會笑——但笑完還是要看量。",
        "這根紅棒讓我暫時忘了衣櫃裡那幾件套牢的股票。",
        "盤面轉強是好事，但我提醒自己：追高是最貴的一種浪漫。",
    ),
    "mid": (
        "不上不下的盤最磨人，像等一鍋水滾，你只能站著看，還不能走開。",
        "多空在那邊拔河，我這種散戶只能抱緊便當看戲。",
        "沒方向的時候，最好的策略就是不要把子彈一次打完。",
    ),
    "cool": (
        "綠油油一片，我一度以為在看草原。幸好我帶了椅子，坐著等它長回來。",
        "這種盤就像下雨天叫不到車，越急越沒有，慢慢等吧。",
        "跌成這樣，我把停損單當成保險費——繳了會心痛，但不繳更痛。",
    ),
    "cold": (
        "今天這種盤，連我家那隻狗都不看螢幕了。泡杯茶，比盯盤有用。",
        "帳面瘦到我都快認不出來。這種時候唯一該做的，是先活著，明天再說。",
        "被套牢那麼多次，我最大的收穫是學會：不要在恐慌裡做決定。",
    ),
}

# 風險提醒的固定收尾（宇程君式的自我調侃，但訊息是認真的）
RISK_CLOSER = (
    "我這輩子被套的次數比看對的次數多得多，所以我的每一句話都請當成"
    "「參考」，不要當成「指令」。"
)


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def _band(v: float, lo: float, hi: float) -> float:
    """把 v 映射到 [lo, hi] 的相對位置並夾在 0..1，用於把不同量尺的分項
    統一成 0..1 的分數。"""
    if hi == lo:
        return 0.0
    return _clamp01((v - lo) / (hi - lo))


def sector_of(code: str) -> str:
    """依股票代號區間粗略判定族群（非官方產業別）。"""
    try:
        n = int(str(code))
    except (TypeError, ValueError):
        return "其他"
    for lo, hi, name in SECTOR_BY_CODE:
        if lo <= n <= hi:
            return name
    return "其他"


def dominant_sector(records: list[dict]) -> tuple[str, int, int]:
    """回傳 (最常見族群, 該族群檔數, 樣本總數)。同數量時以族群名稱排序決定，
    確保結果決定性。"""
    counts = Counter(sector_of(r.get("code", "")) for r in records)
    if not counts:
        return "無", 0, 0
    name, n = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    return name, n, sum(counts.values())


# --------------------------------------------------------------------------
# 核心計算：把當日資料換算成宇程君的觀察值
# --------------------------------------------------------------------------
def analyze(cfg: dict) -> dict:
    """純函式：輸入 update_snapshot.collect() 的 cfg，輸出所有可顯示的觀察值。

    不含任何隨機或時間相依成分。同一份 cfg 永遠得到相同的結果。
    """
    close = cfg["today_close"]
    prev = cfg["prev_close"]
    mkt = cfg["mkt"]
    win = cfg["win"]
    ma5 = cfg["win_ma5"][-1]
    ma20 = cfg["win_ma20"][-1]

    chg = close - prev
    pchg = (chg / prev * 100.0) if prev else 0.0

    up, dn, fl = mkt["up"], mkt["down"], mkt["flat"]
    up_stop, dn_stop = (mkt["up_stop"] or 0), (mkt["down_stop"] or 0)
    mkt_up_stop, mkt_dn_stop = (mkt["mkt_up_stop"] or 0), (mkt["mkt_down_stop"] or 0)
    amt_yi = mkt["total_amt"] / 1e8

    # --- 量能：只比較 FMTQIK 自家序列（同一來源、同一基準、同一單位）---
    last_amt = win[-1]["amt"]
    prev20 = [r["amt"] for r in win[-21:-1] if r["amt"] is not None]
    amt_base = (sum(prev20) / len(prev20)) if prev20 else last_amt
    amt_ratio = (last_amt / amt_base) if amt_base else 1.0

    # --- 四個氛圍分項（各 0..1）---
    i_score = _band(pchg, -2.0, 2.0)
    breadth_ratio = (up / (up + dn)) if (up + dn) else 0.5
    b_score = _band(breadth_ratio, 0.30, 0.70)
    a_score = _band(amt_ratio, 0.75, 1.25)
    ma_score = ((0.5 if close > ma5 else 0.0)
                + (0.25 if close > ma20 else 0.0)
                + (0.25 if ma5 > ma20 else 0.0))
    mood = 100.0 * (MOOD_WEIGHTS["index"] * i_score
                    + MOOD_WEIGHTS["breadth"] * b_score
                    + MOOD_WEIGHTS["volume"] * a_score
                    + MOOD_WEIGHTS["ma"] * ma_score)

    tier_key, tier_label = "cold", MOOD_TIERS[-1][2]
    for lo, key, label in MOOD_TIERS:
        if mood >= lo:
            tier_key, tier_label = key, label
            break

    pool = CATCHPHRASES[tier_key]
    catch = pool[int(mood) % len(pool)]

    # --- 均線排列與最近一次交叉 ---
    if close > ma5 > ma20:
        ma_order = "多頭排列（收盤 &gt; MA5 &gt; MA20）"
    elif close < ma5 < ma20:
        ma_order = "空頭排列（收盤 &lt; MA5 &lt; MA20）"
    else:
        ma_order = "均線糾結（多空未明）"

    c_all = cfg["crosses_all"]
    c_win = cfg["crosses_in_win"]
    if c_all:
        r0 = c_all[-1]
        cross_txt = ("{} 的{}（當日收 {:,.2f} 點）".format(
            r0["date"], "黃金交叉" if r0["kind"] == "g" else "死亡交叉", r0["close"]))
    else:
        cross_txt = "無（歷史區間內未出現交叉）"

    # --- 台積電 2330 ---
    tsmc = cfg["tsmc"]
    ts_diff = tsmc["pct"] - pchg

    # --- 族群 ---
    g_name, g_n, g_tot = dominant_sector(cfg["gain"])
    l_name, l_n, l_tot = dominant_sector(cfg["lose"])

    # --- 量價配合 ---
    if pchg > 0.05 and amt_ratio >= 1.05:
        vp = "量價齊揚，多方力道有量能支撐"
    elif pchg > 0.05 and amt_ratio <= 0.95:
        vp = "價漲量縮，追價意願偏保守"
    elif pchg < -0.05 and amt_ratio >= 1.05:
        vp = "價跌量增，賣壓相對沉重"
    elif pchg < -0.05 and amt_ratio <= 0.95:
        vp = "價跌量縮，賣壓暫歇但買盤也未接手"
    else:
        vp = "量價變化不大，盤面偏向觀望"

    # --- 風險提醒（依當日數據挑選，非固定文案）---
    warns: list[str] = []
    if mood >= 75.0:
        warns.append("市場氛圍偏熱，短線追高的回檔風險升高")
    if mood <= 35.0:
        warns.append("市場氛圍偏冷，弱勢股容易持續破底，不宜任意攤平")
    if up_stop >= 5:
        warns.append("今日有 {} 檔漲停，個股波動極端，留意隔日反轉".format(up_stop))
    if dn_stop >= 5:
        warns.append("今日有 {} 檔跌停，賣壓相對集中".format(dn_stop))
    warns.append("MA5 {}".format("仍低於 MA20，中期均線結構偏空"
                                 if ma5 < ma20 else
                                 "高於 MA20，中期均線結構偏多（均線皆為落後指標）"))
    if amt_ratio < 0.90:
        warns.append("成交金額低於近 20 個交易日均量，量能不足易使行情無力")
    elif amt_ratio > 1.25:
        warns.append("成交金額明顯放大，短線換手激烈")
    if abs(pchg) >= 1.5:
        warns.append("單日振幅偏大，部位控制比選股更重要")
    warns.append("不論盤怎麼走，先想好最多能虧多少，再想能賺多少")

    return {
        "close": close, "prev": prev, "chg": chg, "pchg": pchg,
        "up": up, "dn": dn, "fl": fl, "up_stop": up_stop, "dn_stop": dn_stop,
        "mkt_up_stop": mkt_up_stop, "mkt_dn_stop": mkt_dn_stop,
        "amt_yi": amt_yi, "last_amt": last_amt, "amt_base": amt_base,
        "amt_ratio": amt_ratio,
        "ma5": ma5, "ma20": ma20, "ma_order": ma_order,
        "breadth_ratio": breadth_ratio,
        "i_score": i_score, "b_score": b_score, "a_score": a_score,
        "ma_score": ma_score, "mood": mood,
        "tier_key": tier_key, "tier_label": tier_label, "catchphrase": catch,
        "cross_txt": cross_txt, "n_cross_all": len(c_all), "n_cross_win": len(c_win),
        "tsmc": tsmc, "ts_diff": ts_diff,
        "gain_top": cfg["gain"][0] if cfg["gain"] else None,
        "lose_top": cfg["lose"][0] if cfg["lose"] else None,
        "g_sector": g_name, "g_n": g_n, "l_sector": l_name, "l_n": l_n,
        "vp": vp, "warns": warns,
    }


# --------------------------------------------------------------------------
# 版面
# --------------------------------------------------------------------------
def render_html(a: dict, *, ad: str, f0, f2, pct, pts, esc) -> str:
    """把 analyze() 的結果輸出成 SNAP:AIANALYST 區塊內容。

    格式化函式由呼叫端注入，確保與頁面其他數字使用完全相同的慣例。
    `ad` 為資料日期（AD 格式，取自 STOCK_DAY_ALL）。
    """
    pct_of_up = (a["up"] / (a["up"] + a["dn"] + a["fl"]) * 100.0
                 if (a["up"] + a["dn"] + a["fl"]) else 0.0)
    diff_txt = ""  # 已改由上面的 rel 片語表達
    diff_val = ""
    ratio_txt = ("放大" if a["amt_ratio"] >= 1.05 else
                 ("縮小" if a["amt_ratio"] <= 0.95 else "持平"))
    vp_class = ("up" if a["amt_ratio"] >= 1.05 and a["pchg"] > 0.05 else
                ("down" if a["amt_ratio"] >= 1.05 and a["pchg"] < -0.05 else "flat"))

    quote = (
        "今天加權指數收 {c} 點，{ch} 點（{pc}）；上市股票 {u} 家上漲、{d} 家下跌，"
        "集中市場成交金額 {amt} 億元。{catch}".format(
            c=f2(a["close"]), ch=pts(a["chg"]), pc=pct(a["pchg"]),
            u=f0(a["up"]), d=f0(a["dn"]), amt=f2(a["amt_yi"]),
            catch=a["catchphrase"]))

    g, l = a["gain_top"], a["lose_top"]
    movers = ""
    if g is not None:
        movers += ("漲幅榜首 <b>{code} {name}</b>（{pc}，收 {px} 元）".format(
            code=esc(g["code"]), name=esc(g["name"]),
            pc=pct(g["pct"]), px=f2(g["close"])))
        movers += "，漲幅前十名中以 <b>{}</b> 共 <b>{}</b> 檔最多".format(
            a["g_sector"], a["g_n"])
    if l is not None:
        movers += ("；跌幅榜首 <b>{code} {name}</b>（{pc}，收 {px} 元）".format(
            code=esc(l["code"]), name=esc(l["name"]),
            pc=pct(l["pct"]), px=f2(l["close"])))
        movers += "，跌幅前十名中以 <b>{}</b> 共 <b>{}</b> 檔最多".format(
            a["l_sector"], a["l_n"])
    movers += "。"

    t = a["tsmc"]
    ts_txt = ("收 <b>{px}</b> 元（{ch} 元／{pc}），成交量 <b>{vol}</b> 張、"
              "成交金額 <b>{amt}</b> 億元，{rel}").format(
        px=f2(t["close"]), ch=pts(t["chg"]), pc=pct(t["pct"]),
        vol=f0(t["vol"] / 1000), amt=f2((t["amt"] or 0) / 1e8),
        rel=("表現較大盤<b>強</b> {:.2f} 個百分點".format(abs(a["ts_diff"]))
             if a["ts_diff"] > 0.005 else
             ("表現較大盤<b>弱</b> {:.2f} 個百分點".format(abs(a["ts_diff"]))
              if a["ts_diff"] < -0.005 else "表現<b>與大盤同步</b>")))

    points = [
        ("大盤力道",
         "加權指數收 <b>{c}</b> 點（{ch} 點／<b>{pc}</b>）；上市股票 <b>{u}</b> 家上漲"
         "（含漲停 {us} 家）、<b>{d}</b> 家下跌（含跌停 {ds} 家）、<b>{fl}</b> 家持平，"
         "上漲家數占 <b>{share}%</b>。全市場（含 ETF、權證等）漲停 {mus} 家、跌停 {mds} 家，"
         "集中市場成交金額 <b>{amt}</b> 億元。".format(
             c=f2(a["close"]), ch=pts(a["chg"]), pc=pct(a["pchg"]),
             u=f0(a["up"]), us=f0(a["up_stop"]), d=f0(a["dn"]), ds=f0(a["dn_stop"]),
             fl=f0(a["fl"]), share="{:.1f}".format(pct_of_up),
             mus=f0(a["mkt_up_stop"]), mds=f0(a["mkt_dn_stop"]),
             amt=f2(a["amt_yi"]))),

        ("均線與交叉",
         "收盤 <b>{c}</b> 點位於 MA5（{m5}）<b>{pos5}</b>、MA20（{m20}）<b>{pos20}</b>，"
         "排列為 <b>{order}</b>；近期歷史區間內最近一次均線交叉為 <b>{cross}</b>"
         "（全期共 {na} 筆，走勢圖 60 日區間內 {nw} 筆）。".format(
             c=f2(a["close"]), m5=f2(a["ma5"]),
             pos5="之上" if a["close"] > a["ma5"] else ("之下" if a["close"] < a["ma5"] else "約當"),
             m20=f2(a["ma20"]),
             pos20="之上" if a["close"] > a["ma20"] else ("之下" if a["close"] < a["ma20"] else "約當"),
             order=a["ma_order"], cross=a["cross_txt"],
             na=a["n_cross_all"], nw=a["n_cross_win"])),

        ("台積電 2330", ts_txt + "。"),

        ("族群與個股", movers +
         "族群歸類係<b>依股票代號區間粗略判定</b>，非證交所官方產業別。"),

        ("成交量能",
         "最近交易日成交金額為前 20 個交易日均量的 <b>{r}%</b>（量能{rt}）；"
         "指數 {dir} 配合量能變化，屬 <b class=\"{k}\">{vp}</b>。"
         "（量能比較採走勢圖同一 FMTQIK 序列，避免跨端點單位差異。）".format(
             r="{:.1f}".format(a["amt_ratio"] * 100.0), rt=ratio_txt,
             dir=("上漲" if a["pchg"] > 0.05 else ("下跌" if a["pchg"] < -0.05 else "大致收平")),
             k=vp_class, vp=a["vp"])),

        ("氛圍計分（可複核）",
         "指數漲跌 <b>{i}</b>／100（權重 35%）＋漲跌家數比 <b>{b}</b>／100（30%）"
         "＋量能變化 <b>{v}</b>／100（20%）＋均線排列 <b>{m}</b>／100（15%）"
         "＝ 合計 <b>{mood}</b> 分，對應等級「<b>{lab}</b>」。"
         "此為本頁自訂的啟發式分數，<b>不是</b>市場共識、機率或預測值。".format(
             i="{:.0f}".format(a["i_score"] * 100), b="{:.0f}".format(a["b_score"] * 100),
             v="{:.0f}".format(a["a_score"] * 100), m="{:.0f}".format(a["ma_score"] * 100),
             mood="{:.1f}".format(a["mood"]), lab=a["tier_label"])),
    ]

    li_html = "\n".join(
        '      <li><b class="lb">{}</b> {}</li>'.format(lb, body) for lb, body in points)

    warns = "；".join(a["warns"][:4]) + "。" + RISK_CLOSER

    return (
        '<section class="card ai-card" aria-labelledby="ai-t">\n'
        '    <div class="ai-head">\n'
        '      <img class="ai-av" src="assets/yu_cheng_jun_avatar.png" width="88" height="88"\n'
        '           alt="「韭經考驗的台股戰士-宇程君」角色頭像（AI 生成虛擬角色）" decoding="async">\n'
        '      <div class="ai-id">\n'
        '        <h2 id="ai-t">韭經考驗的台股戰士-宇程君</h2>\n'
        '        <p class="ai-role">{lbl}・依當日證交所公開資料自動產生的市場評論</p>\n'
        '        <p class="ai-badges">\n'
        '          <span class="ai-badge">{lbl}</span>\n'
        '          <span class="ai-badge">AI 生成虛擬角色</span>\n'
        '          <span class="ai-badge is-warn">非投資建議</span>\n'
        '        </p>\n'
        '      </div>\n'
        '      <div class="ai-mood is-{tk}">\n'
        '        <span class="ai-mood-lb">市場氛圍</span>\n'
        '        <b class="ai-mood-v">{tl}</b>\n'
        '        <span class="ai-mood-meter" role="img" aria-label="市場氛圍分數 {mv} 分，滿分 100 分">'
        '<i style="width:{mw}%"></i></span>\n'
        '        <span class="ai-mood-n num">{mv} / 100</span>\n'
        '      </div>\n'
        '    </div>\n'
        '\n'
        '    <blockquote class="ai-quote">\n'
        '      「{quote}」\n'
        '      <cite>— 宇程君（{lbl}）</cite>\n'
        '    </blockquote>\n'
        '\n'
        '    <ul class="ai-points">\n'
        '{li}\n'
        '    </ul>\n'
        '\n'
        '    <p class="ai-risk"><b>宇程君碎念：</b>{warns}</p>\n'
        '\n'
        '    <p class="ai-src"><b>這個角色與這段文字是怎麼來的：</b>'
        '「{who}」是 <b>AI 生成的虛擬角色</b>，也就是本站標示的<b>{lbl}</b>；不是真人。'
        '本區塊文字由 <code>scripts/analyst.py</code> 於每日自動更新時，'
        '依當日自證交所實際抓取的數字，以<b>固定規則</b>產生'
        '（無隨機成分、無寫死的市場數字；同一份資料重跑會得到完全相同的文字）。'
        '氛圍分數的四個分項與權重皆列於上方供複核。'
        '資料日期 <b>{ad}</b>。</p>\n'
        '  </section>'.format(
            tk=a["tier_key"], tl=a["tier_label"],
            mv="{:.1f}".format(a["mood"]), mw="{:.1f}".format(a["mood"]),
            quote=quote, li=li_html, warns=warns, who=CHARACTER,
            lbl=ROLE_LABEL, ad=esc(ad))
    )


def build(cfg: dict, *, f0, f2, pct, pts, esc) -> str:
    """對外主入口：回傳 SNAP:AIANALYST 的區塊內容。"""
    return render_html(analyze(cfg), ad=cfg["date"],
                       f0=f0, f2=f2, pct=pct, pts=pts, esc=esc)
