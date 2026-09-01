# -*- coding: utf-8 -*-
"""
株主優待銘柄 PER割安スクリーナー(ローカル単発実行版)

優待のある銘柄について過去のPER推移を復元し、
「一度高く買われたPERが是正されて、過去の安値水準まで戻ってきた」銘柄を抽出する。

過去PERの作り方:
    yfinanceが返すPERは現時点の1点のみで履歴がないため、
    年次の純利益と発行済株式数から分割調整済みEPSを期ごとに算出し、
    決算発表の想定日から適用する階段関数として日足終値に割り当てる。

判定の考え方:
    平均とz-scoreだけで見ると、EPSが数倍に伸びた銘柄では
    「EPSが小さかった頃の高PER期」が平均を押し上げ、
    実際にはレンジ上限にいる銘柄まで割安と誤判定してしまう。
    そのため以下のロバストな指標を主軸に置く。
      ・下位%     : 過去分布の中での順位(外れ値に強い)
      ・最小比    : 現在PER ÷ 過去最小PER。過去の安値水準まで来ているか
      ・直近1年比 : 現在PERが直近1年の中央値以下か。今まさに下がってきている最中か
      ・ピーク比  : 現在PER ÷ 過去最大PER。一時的な高評価からの是正度合い
    平均PERとz-scoreは参考値として表示のみ行う。

使い方:
    python3 yutai_value_screener.py                      # 既定条件で実行
    python3 yutai_value_screener.py --max-min-ratio 1.2  # 条件を厳しく
    python3 yutai_value_screener.py --refresh-yutai      # 優待一覧を取り直す
    python3 yutai_value_screener.py --codes 2216,3494    # 指定銘柄だけ確認
"""

import argparse
import os
import re
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests
import yfinance as yf

warnings.filterwarnings("ignore")

# ================= 設定 =================
MAX_PCT = 30.0                 # 現在PERの過去分布内の順位(下位%)の上限
MAX_MIN_RATIO = 1.3            # 現在PER ÷ 過去最小PER の上限。過去の安値水準までの近さ
MAX_PEAK_RATIO = 0.65          # 現在PER ÷ 過去最大PER の上限。一時高評価からの是正度合い
REQUIRE_RECENT_LOW = True      # 現在PERが直近1年の中央値以下であることを要求する
MIN_EPS_GROWTH = 1.0           # 直近EPS ÷ 最古EPS の下限。1.0で「減益していない」
MIN_YEARS = 3.0                # PER時系列に必要な最低年数
REPORT_LAG_DAYS = 75           # 期末から決算発表までの想定日数
PRICE_PERIOD = "6y"            # 株価履歴の取得期間
WORKERS = 4                    # yfinance並列取得数。上げすぎるとYahooのレート制限に掛かる
RETRY_MAX = 4                  # レート制限時のリトライ回数
RETRY_WAIT = [5, 15, 40, 90]   # リトライ間隔(秒)
YUTAI_CACHE = "yutai_master.csv"
YUTAI_CACHE_DAYS = 7           # 優待一覧キャッシュの有効日数
OUT_DIR = "output"
# ========================================

JST = timezone(timedelta(hours=9))
MINKABU_URL = "https://minkabu.jp/yutai/search?page={}"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
HEADERS = {"User-Agent": UA}


# ---------------- 優待銘柄一覧 ----------------

def fetch_yutai_list(max_pages=120, delay=1.0):
    """みんかぶの優待検索から優待銘柄の一覧を取得する"""
    print("みんかぶから優待銘柄一覧を取得中...")
    rows, total = [], None
    for page in range(1, max_pages + 1):
        try:
            resp = requests.get(MINKABU_URL.format(page), headers=HEADERS, timeout=25)
            resp.raise_for_status()
        except Exception as e:
            print(f"  page {page} 取得失敗: {e}")
            break
        html = resp.text

        if total is None:
            m = re.search(r"全(\d[\d,]*)件", html)
            if m:
                total = int(m.group(1).replace(",", ""))
                print(f"  検索結果 全{total}件")

        blocks = html.split('<li class="yutai_rank_style">')[1:]
        if not blocks:
            break

        for b in blocks:
            m = re.search(r'/stock/([0-9A-Z]{4,5})/yutai"[^>]*class="fwb">\s*(.+?)\(\w+\)\s*</a>', b, re.S)
            if not m:
                continue
            item = re.search(r'class="yutai_item[^"]*"[^>]*>(.*?)</div>', b, re.S)
            cats = re.findall(r'class="yutai_category">(.*?)</span>', b)
            minv = re.search(r'最低投資金額</div><span class="fsm"><span class="fsn fwb">([\d.]+)</span>', b)
            yld = re.search(r'株主優待利回り</span><span class="fsm"><span class="[^"]*">([\d.\-]+)</span>', b)
            mon = re.search(r'権利確定月</span><span class="fsm fwb">(.*?)</span>', b)
            rows.append({
                "code": m.group(1),
                "name": re.sub(r"\s+", "", m.group(2)),
                "優待内容": re.sub(r"\s+", " ", item.group(1)).strip() if item else "",
                "カテゴリ": "/".join(cats),
                "最低投資額(万円)": float(minv.group(1)) if minv else None,
                "優待利回り(%)": float(yld.group(1)) if yld and yld.group(1) not in ("---", "-") else None,
                "権利確定月": mon.group(1).strip() if mon else "",
            })

        if page % 10 == 0:
            print(f"  page {page} まで完了 (累計 {len(rows)}件)")
        if total and len(rows) >= total:
            break
        time.sleep(delay)

    df = pd.DataFrame(rows).drop_duplicates(subset="code").reset_index(drop=True)
    print(f"優待銘柄 {len(df)}件を取得")
    return df


def load_yutai_list(refresh=False):
    """キャッシュがあれば使い、無ければ取得して保存する"""
    if not refresh and os.path.exists(YUTAI_CACHE):
        age = (time.time() - os.path.getmtime(YUTAI_CACHE)) / 86400
        if age <= YUTAI_CACHE_DAYS:
            df = pd.read_csv(YUTAI_CACHE, dtype={"code": str})
            print(f"優待一覧をキャッシュから読み込み ({len(df)}件 / {age:.1f}日前)")
            return df
        print(f"優待一覧のキャッシュが{age:.0f}日前のため取り直します")

    df = fetch_yutai_list()
    df.to_csv(YUTAI_CACHE, index=False)
    return df


# ---------------- PER履歴の構築 ----------------

def per_history(code):
    """日次PER時系列・年次EPS・直近12ヶ月配当を返す。失敗時は (None, 理由)"""
    t = yf.Ticker(f"{code}.T")

    hist = t.history(period=PRICE_PERIOD, auto_adjust=False)
    if hist is None or hist.empty:
        return None, "株価取得失敗"
    hist.index = hist.index.tz_localize(None)

    annual = t.income_stmt
    if annual is None or annual.empty or "Net Income" not in annual.index:
        return None, "年次財務なし"
    net_income = annual.loc["Net Income"].dropna().sort_index()
    if len(net_income) < 3:
        return None, f"年次データ{len(net_income)}期のみ"

    shares = t.get_shares_full(start=hist.index.min().strftime("%Y-%m-%d"))
    if shares is None or len(shares) == 0:
        return None, "株式数履歴なし"
    shares = shares.copy()
    shares.index = pd.to_datetime(shares.index).tz_localize(None)
    shares = shares[~shares.index.duplicated(keep="last")].sort_index().dropna()

    # 株価は分割調整済みなので、株式数もその日以降の分割倍率を掛けて現在基準に揃える
    splits = hist[hist["Stock Splits"] != 0]["Stock Splits"]

    def split_factor(d):
        f = 1.0
        for split_date, ratio in splits.items():
            if split_date > d:
                f *= ratio
        return f

    eps_rows = []
    for fy_end, net in net_income.items():
        fy_end = pd.Timestamp(fy_end)
        prior = shares[shares.index <= fy_end]
        raw_shares = prior.iloc[-1] if len(prior) else shares.iloc[0]
        shares_adj = raw_shares * split_factor(fy_end)
        if not shares_adj or shares_adj <= 0:
            continue
        eps_rows.append({
            "fy_end": fy_end,
            "effective": fy_end + pd.Timedelta(days=REPORT_LAG_DAYS),
            "net_income": net,
            "eps": net / shares_adj,
        })
    if len(eps_rows) < 3:
        return None, "EPS算出不可"
    eps_df = pd.DataFrame(eps_rows).sort_values("effective")

    close = hist["Close"]
    eps_series = pd.Series(np.nan, index=close.index)
    for _, r in eps_df.iterrows():
        eps_series[close.index >= r["effective"]] = r["eps"]

    per = (close / eps_series).replace([np.inf, -np.inf], np.nan)
    out = pd.DataFrame({"close": close, "eps": eps_series, "per": per}).dropna()
    if out.empty:
        return None, "PER算出不可"

    # 配当は株価履歴に含まれるため追加のリクエストなしで取れる(分割調整済み)
    div = hist["Dividends"][hist["Dividends"] > 0]
    recent = hist.index.max() - pd.Timedelta(days=365)
    div_ttm = float(div[div.index >= recent].sum()) if len(div) else 0.0

    # BPS。EPSと同じ分割調整済み株式数で割ることで単位を揃える
    bps_rows = []
    try:
        bs = t.balance_sheet
    except Exception:
        bs = None
    if bs is not None and not bs.empty:
        equity_row = next((r for r in ("Stockholders Equity", "Common Stock Equity",
                                       "Total Equity Gross Minority Interest") if r in bs.index), None)
        if equity_row:
            for fy_end, equity in bs.loc[equity_row].dropna().sort_index().items():
                fy_end = pd.Timestamp(fy_end)
                prior = shares[shares.index <= fy_end]
                raw_shares = prior.iloc[-1] if len(prior) else shares.iloc[0]
                shares_adj = raw_shares * split_factor(fy_end)
                if shares_adj and shares_adj > 0:
                    bps_rows.append({"fy_end": fy_end, "bps": equity / shares_adj})
    bps_df = pd.DataFrame(bps_rows)

    return (out, eps_df, div_ttm, bps_df), None


def _is_rate_limit(e):
    return "RateLimit" in type(e).__name__ or "Too Many Requests" in str(e)


def analyze(row):
    """1銘柄を分析して指標の辞書を返す。レート制限は待って再試行する"""
    code, name = str(row["code"]), row["name"]
    res = err = None
    for attempt in range(RETRY_MAX + 1):
        try:
            res, err = per_history(code)
            break
        except Exception as e:
            if _is_rate_limit(e) and attempt < RETRY_MAX:
                time.sleep(RETRY_WAIT[min(attempt, len(RETRY_WAIT) - 1)])
                continue
            return {"code": code, "name": name, "err": f"例外:{type(e).__name__}"}
    if err:
        return {"code": code, "name": name, "err": err}

    out, eps_df, div_ttm, bps_df = res
    per = out["per"]
    eps_list = eps_df["eps"].tolist()
    cur = per.iloc[-1]
    price = out["close"].iloc[-1]
    std = per.std()

    # 直近1年のレンジ。「今まさに下がってきている最中か」を見るために使う
    recent = per[per.index >= per.index.max() - pd.Timedelta(days=365)]
    if len(recent) < 20:
        recent = per

    bps_list = bps_df["bps"].tolist() if len(bps_df) else []
    bps_years = [d.strftime("%Y") for d in bps_df["fy_end"]] if len(bps_df) else []
    bps_now = bps_list[-1] if bps_list else np.nan
    bps_growth = bps_list[-1] / bps_list[0] if len(bps_list) >= 2 and bps_list[0] > 0 else np.nan
    bps_rising = bool(len(bps_list) >= 2 and all(b < a for b, a in zip(bps_list, bps_list[1:])))

    return {
        "code": code, "name": name, "err": None,
        "株価": price,
        "PER": cur,
        "最小PER": per.min(),
        "最小比": cur / per.min() if per.min() > 0 else np.nan,
        "最大PER": per.max(),
        "最大PER時期": per.idxmax().strftime("%Y-%m"),
        "ピーク比": cur / per.max() if per.max() > 0 else np.nan,
        "平均PER": per.mean(),
        "σ": std,
        "z": (cur - per.mean()) / std if std else np.nan,
        "下位%": (per < cur).mean() * 100,
        "1年中央値": recent.median(),
        "1年最小": recent.min(),
        "1年最大": recent.max(),
        "1年中央値以下": bool(cur <= recent.median()),
        "配当利回り(%)": div_ttm / price * 100 if price else np.nan,
        "BPS": bps_now,
        "BPS推移": bps_list,
        "BPS年": bps_years,
        "BPS成長": bps_growth,
        "BPS毎期増加": bps_rising,
        "PBR": price / bps_now if bps_now and bps_now > 0 else np.nan,
        "EPS推移": eps_list,
        "EPS成長": eps_list[-1] / eps_list[0] if eps_list[0] > 0 else np.nan,
        "EPS全期黒字": all(e > 0 for e in eps_list),
        "年数": (out.index.max() - out.index.min()).days / 365.25,
        "spark": per.resample("ME").last().dropna().tolist(),
    }


# ---------------- 出力 ----------------

def sparkline_svg(values, current, width=100, height=26):
    """PER推移のインラインSVG。現在値の水準に破線を引いて位置関係を分かるようにする"""
    vals = [v for v in values if v is not None and np.isfinite(v)]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    rng = hi - lo or 1.0
    step = width / (len(vals) - 1)

    def y(v):
        return height - (v - lo) / rng * (height - 5) - 2.5

    pts = " ".join(f"{i * step:.1f},{y(v):.1f}" for i, v in enumerate(vals))
    ylo = y(lo)
    return (
        f'<svg width="{width + 3}" height="{height}" viewBox="0 0 {width + 3} {height}">'
        f'<line x1="0" y1="{ylo:.1f}" x2="{width}" y2="{ylo:.1f}" '
        f'stroke="#c9c4bc" stroke-width="1" stroke-dasharray="2,2"/>'
        f'<polyline points="{pts}" fill="none" stroke="#6b7fd7" stroke-width="1.5"/>'
        f'<circle cx="{width:.1f}" cy="{y(vals[-1]):.1f}" r="2.5" fill="#e0533d"/></svg>'
    )


def bar_chart_svg(values, labels, width=104, height=34):
    """年ごとのBPSを示すインラインSVGの棒グラフ。積み上がっていれば右肩上がりに見える"""
    vals = [v for v in values if v is not None and np.isfinite(v)]
    if len(vals) < 2:
        return ""
    hi = max(vals)
    lo = min(0, min(vals))
    rng = (hi - lo) or 1.0
    n = len(vals)
    gap = 3
    bw = (width - gap * (n - 1)) / n
    top = 2
    plot_h = height - top - 10  # 下部に年ラベルの帯を残す
    base = top + plot_h

    bars, marks = [], []
    for i, v in enumerate(vals):
        h = max(1.0, (v - lo) / rng * plot_h)
        x = i * (bw + gap)
        y = base - h
        # 直近期だけ色を濃くして現在地を分かるようにする
        fill = "#3f6b57" if i == n - 1 else "#a8c4b5"
        bars.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{h:.1f}" fill="{fill}" rx="1"/>')
        if labels and i < len(labels):
            marks.append(f'<text x="{x + bw / 2:.1f}" y="{height - 1:.0f}" font-size="6.5" '
                         f'fill="#8a857d" text-anchor="middle">{labels[i][2:]}</text>')
    return (f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
            + "".join(bars) + "".join(marks) + "</svg>")


def build_html(df, params, data_date):
    """スクリーニング結果をHTMLにまとめる"""
    rows = []
    for _, r in df.iterrows():
        eps_txt = " → ".join(f"{e:,.1f}" for e in r["EPS推移"])
        y_div = r["配当利回り(%)"]
        y_yut = r["優待利回り(%)"]
        total = (0 if pd.isna(y_div) else y_div) + (0 if pd.isna(y_yut) else y_yut)
        bps_list = r["BPS推移"] if isinstance(r["BPS推移"], list) else []
        bps_svg = bar_chart_svg(bps_list, r["BPS年"] if isinstance(r["BPS年"], list) else [])
        bps_txt = f"{bps_list[-1]:,.0f}円" if bps_list else "—"
        arrow = " ↑毎期増" if r["BPS毎期増加"] else ""
        bps_growth_txt = "—" if pd.isna(r["BPS成長"]) else f"{r['BPS成長']:.2f}x<div class=\"sub\">{arrow.strip()}</div>"
        pbr_txt = "—" if pd.isna(r["PBR"]) else f"{r['PBR']:.2f}"
        rows.append(f"""
    <tr>
      <td class="code">{r['code']}</td>
      <td class="name"><a href="https://minkabu.jp/stock/{r['code']}/yutai" target="_blank" rel="noopener">{r['name']}</a></td>
      <td class="num">{r['株価']:,.0f}</td>
      <td class="num strong">{r['PER']:.1f}</td>
      <td class="num">{r['最小PER']:.1f}</td>
      <td class="num good">{r['最小比']:.2f}</td>
      <td class="num">{r['下位%']:.0f}%<div class="sub">平均{r['平均PER']:.1f} z{r['z']:+.2f}</div></td>
      <td class="num">{r['ピーク比']:.2f}<div class="sub">最大{r['最大PER']:.1f} ({r['最大PER時期']})</div></td>
      <td class="num">{r['1年最小']:.1f}〜{r['1年最大']:.1f}<div class="sub">中央{r['1年中央値']:.1f}</div></td>
      <td class="spark">{sparkline_svg(r['spark'], r['PER'])}</td>
      <td class="num">{r['EPS成長']:.2f}x<div class="sub">{eps_txt}</div></td>
      <td class="bps">{bps_svg}<div class="sub">{bps_txt}</div></td>
      <td class="num">{bps_growth_txt}</td>
      <td class="num">{pbr_txt}</td>
      <td class="num">{'—' if pd.isna(y_div) else f'{y_div:.2f}%'}</td>
      <td class="num">{'—' if pd.isna(y_yut) else f'{y_yut:.2f}%'}</td>
      <td class="num strong">{total:.2f}%</td>
      <td class="yutai">{r['優待内容']}<div class="sub">{r['カテゴリ']}</div></td>
      <td class="num">{r['最低投資額(万円)']:,.1f}万</td>
      <td class="num sub">{r['権利確定月']}</td>
    </tr>""")

    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>優待銘柄 PER割安スクリーナー {data_date}</title>
<style>
  body {{ font-family: -apple-system, "Hiragino Sans", sans-serif; margin: 24px; background:#fbfbfa; color:#22201d; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .meta {{ color:#6b6660; font-size: 13px; margin-bottom: 16px; }}
  .cond {{ background:#fff; border:1px solid #e5e2dd; border-radius:8px; padding:10px 14px;
           font-size:13px; color:#4a4640; margin-bottom:18px; max-width:900px; line-height:1.7; }}
  .cond b {{ color:#22201d; }}
  .wrap {{ overflow-x:auto; }}
  table {{ border-collapse: collapse; font-size: 13px; background:#fff; min-width: 1700px; }}
  th, td {{ border-bottom:1px solid #ece9e4; padding:7px 9px; text-align:left; vertical-align:top; }}
  th {{ background:#f3f1ed; position:sticky; top:0; cursor:pointer; white-space:nowrap; font-weight:600; }}
  th:hover {{ background:#e9e6e0; }}
  tbody tr:hover {{ background:#fdf9f2; }}
  .num {{ text-align:right; white-space:nowrap; }}
  .strong {{ font-weight:700; }}
  .good {{ font-weight:600; color:#1f7a5a; }}
  .code {{ color:#8a857d; font-variant-numeric: tabular-nums; }}
  .name a {{ color:#22201d; text-decoration:none; font-weight:600; }}
  .name a:hover {{ text-decoration:underline; }}
  .sub {{ color:#8a857d; font-size:11px; font-weight:400; }}
  .yutai {{ max-width: 240px; }}
  .spark {{ padding:4px 6px; }}
  .bps {{ padding:4px 6px; white-space:nowrap; }}
</style></head><body>
<h1>優待銘柄 PER割安スクリーナー</h1>
<div class="meta">{data_date} 時点 / 該当 {len(df)} 銘柄</div>
<div class="cond">
  <b>抽出条件</b>&nbsp;
  最小比 <b>≦ {params['max_min_ratio']}</b>&nbsp;・&nbsp;
  下位 <b>{params['max_pct']:.0f}%以内</b>&nbsp;・&nbsp;
  ピーク比 <b>≦ {params['max_peak']}</b>&nbsp;・&nbsp;
  {'現在PERが<b>直近1年の中央値以下</b>・&nbsp;' if params['require_recent'] else ''}
  EPS成長 <b>≧ {params['min_growth']}倍</b>&nbsp;・&nbsp;全期黒字&nbsp;・&nbsp;履歴 <b>{params['min_years']}年以上</b>
  <div class="sub">
    最小比 = 現在PER ÷ 過去最小PER。1.0に近いほど過去の安値水準にいる。<br>
    ピーク比 = 現在PER ÷ 過去最大PER。小さいほど一時的な高評価から是正されている。<br>
    平均PERとz-scoreは参考値。EPSが大きく伸びた銘柄では過去の高PER期に引っ張られるため判定には使っていない。<br>
    BPS推移 = 年次の純資産 ÷ 分割調整済み株式数。棒が右肩上がりなら純資産が積み上がっている(濃い棒が直近期)。<br>
    PERは実績EPSベース(会社予想は未使用)。配当利回りは直近12ヶ月の実績配当ベース。
  </div>
</div>
<div class="wrap">
<table id="t">
<thead><tr>
  <th>コード</th><th>銘柄名</th><th>株価</th><th>PER</th><th>最小PER</th><th>最小比</th>
  <th>下位%</th><th>ピーク比</th><th>直近1年レンジ</th><th>PER推移</th><th>EPS成長</th>
  <th>BPS推移</th><th>BPS成長</th><th>PBR</th>
  <th>配当利回り</th><th>優待利回り</th><th>合計利回り</th>
  <th>優待内容</th><th>最低投資額</th><th>権利月</th>
</tr></thead>
<tbody>{''.join(rows)}
</tbody></table>
</div>
<script>
// 見出しクリックで並び替え
document.querySelectorAll('#t th').forEach((th, i) => {{
  let asc = false;
  th.addEventListener('click', () => {{
    const tb = document.querySelector('#t tbody');
    const rows = [...tb.rows];
    const num = s => {{ const m = s.replace(/,/g, '').match(/-?\\d+(\\.\\d+)?/); return m ? parseFloat(m[0]) : NaN; }};
    rows.sort((a, b) => {{
      const x = a.cells[i].innerText, y = b.cells[i].innerText;
      const nx = num(x), ny = num(y);
      const v = (!isNaN(nx) && !isNaN(ny)) ? nx - ny : x.localeCompare(y, 'ja');
      return asc ? v : -v;
    }});
    asc = !asc;
    rows.forEach(r => tb.appendChild(r));
  }});
}});
</script>
</body></html>"""


# ---------------- メイン ----------------

def main():
    ap = argparse.ArgumentParser(description="優待銘柄のPER割安スクリーナー")
    ap.add_argument("--max-min-ratio", type=float, default=MAX_MIN_RATIO,
                    help=f"現在PER÷過去最小PER の上限 (既定 {MAX_MIN_RATIO})")
    ap.add_argument("--max-pct", type=float, default=MAX_PCT,
                    help=f"過去分布内の順位(下位%%)の上限 (既定 {MAX_PCT})")
    ap.add_argument("--max-peak", type=float, default=MAX_PEAK_RATIO,
                    help=f"ピーク比の上限 (既定 {MAX_PEAK_RATIO})")
    ap.add_argument("--no-recent-check", action="store_true",
                    help="直近1年の中央値以下という条件を外す")
    ap.add_argument("--min-growth", type=float, default=MIN_EPS_GROWTH,
                    help=f"EPS成長の下限 (既定 {MIN_EPS_GROWTH})")
    ap.add_argument("--min-years", type=float, default=MIN_YEARS,
                    help=f"必要な履歴年数 (既定 {MIN_YEARS})")
    ap.add_argument("--workers", type=int, default=WORKERS, help=f"並列数 (既定 {WORKERS})")
    ap.add_argument("--refresh-yutai", action="store_true", help="優待一覧を取り直す")
    ap.add_argument("--codes", type=str, default=None, help="銘柄コードをカンマ区切りで指定して確認する")
    ap.add_argument("--out-dir", type=str, default=OUT_DIR, help=f"出力先 (既定 {OUT_DIR})")
    args = ap.parse_args()
    require_recent = REQUIRE_RECENT_LOW and not args.no_recent_check

    yutai = load_yutai_list(refresh=args.refresh_yutai)
    yutai["code"] = yutai["code"].astype(str).str.strip()

    if args.codes:
        want = [c.strip() for c in args.codes.split(",") if c.strip()]
        known = yutai[yutai["code"].isin(want)]
        missing = [c for c in want if c not in set(known["code"])]
        extra = pd.DataFrame([{"code": c, "name": c, "優待内容": "(優待一覧に無し)",
                               "カテゴリ": "", "最低投資額(万円)": np.nan,
                               "優待利回り(%)": np.nan, "権利確定月": ""} for c in missing])
        yutai = pd.concat([known, extra], ignore_index=True)
        print(f"指定された {len(yutai)} 銘柄を確認します")

    print(f"\nPER履歴を構築中... ({len(yutai)}銘柄 / {args.workers}並列)")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        results = list(ex.map(analyze, [r for _, r in yutai.iterrows()]))
    print(f"完了 {time.time() - t0:.0f}秒")

    res = pd.DataFrame(results)
    failed = res[res["err"].notna()]
    ok = res[res["err"].isna()].drop(columns=["err"])
    print(f"分析成功 {len(ok)} / 失敗 {len(failed)}")
    if len(failed):
        print("  失敗理由:", failed["err"].value_counts().to_dict())

    merged = ok.merge(
        yutai[["code", "優待内容", "カテゴリ", "最低投資額(万円)", "優待利回り(%)", "権利確定月"]],
        on="code", how="left",
    )

    cond = (
        (merged["PER"] > 0)
        & merged["EPS全期黒字"]
        & (merged["EPS成長"] >= args.min_growth)
        & (merged["最小比"] <= args.max_min_ratio)
        & (merged["下位%"] <= args.max_pct)
        & (merged["ピーク比"] <= args.max_peak)
        & (merged["年数"] >= args.min_years)
    )
    if require_recent:
        cond &= merged["1年中央値以下"]

    hit = merged[cond].sort_values("最小比").reset_index(drop=True)
    print(f"\n条件合致: {len(hit)} 銘柄")

    os.makedirs(args.out_dir, exist_ok=True)
    stamp = datetime.now(JST).strftime("%Y%m%d")
    data_date = datetime.now(JST).strftime("%Y-%m-%d")

    csv_cols = ["code", "name", "株価", "PER", "最小PER", "最小比", "下位%", "ピーク比",
                "最大PER", "最大PER時期", "平均PER", "z", "1年最小", "1年中央値", "1年最大",
                "EPS成長", "年数", "BPS", "BPS成長", "BPS毎期増加", "PBR",
                "配当利回り(%)", "優待利回り(%)",
                "優待内容", "カテゴリ", "最低投資額(万円)", "権利確定月"]
    csv_path = os.path.join(args.out_dir, f"yutai_value_{stamp}.csv")
    hit[csv_cols].round(2).to_csv(csv_path, index=False, encoding="utf-8-sig")

    params = {"max_min_ratio": args.max_min_ratio, "max_pct": args.max_pct,
              "max_peak": args.max_peak, "require_recent": require_recent,
              "min_growth": args.min_growth, "min_years": args.min_years}
    html_path = os.path.join(args.out_dir, f"yutai_value_{stamp}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(build_html(hit, params, data_date))

    print(f"  → {csv_path}")
    print(f"  → {html_path}")

    if len(hit):
        print(f"\n{'コード':<6}{'銘柄名':<20}{'株価':>7}{'PER':>7}{'最小':>7}{'最小比':>7}"
              f"{'下位':>6}{'配当':>7}{'優待':>7}  優待")
        for _, r in hit.head(30).iterrows():
            name = str(r["name"])[:18]
            pad = " " * max(0, 20 - sum(2 if ord(c) > 0x2000 else 1 for c in name))
            dv = "—" if pd.isna(r["配当利回り(%)"]) else f"{r['配当利回り(%)']:.2f}%"
            yt = "—" if pd.isna(r["優待利回り(%)"]) else f"{r['優待利回り(%)']:.2f}%"
            print(f"{r['code']:<6}{name}{pad}{r['株価']:7,.0f}{r['PER']:7.1f}{r['最小PER']:7.1f}"
                  f"{r['最小比']:7.2f}{r['下位%']:5.0f}%{dv:>7}{yt:>7}  {str(r['優待内容'])[:20]}")
        if len(hit) > 30:
            print(f"  ... 他 {len(hit) - 30} 銘柄 (全件はファイル参照)")


if __name__ == "__main__":
    main()
