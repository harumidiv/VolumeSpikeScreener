# -*- coding: utf-8 -*-
"""
日本株 前場出来高急増スクリーナー(GitHub Actions + Gmail通知版)
前場(9:00-11:30)の出来高が過去7営業日の平均全日出来高を上回った銘柄を抽出してメール送信する。

必要な環境変数(GitHub Secretsに設定):
    GMAIL_ADDRESS      送信元Gmailアドレス
    GMAIL_APP_PASSWORD Gmailのアプリパスワード(16桁)
    MAIL_TO            通知先メールアドレス(省略時はGMAIL_ADDRESSと同じ)
"""

import base64
import io
import os
import re
import time
import smtplib
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
import matplotlib
matplotlib.use('Agg')
import japanize_matplotlib  # noqa: F401
import mplfinance as mpf
import tempfile
import jpholiday

# ================= 設定 =================
RATIO_THRESHOLD = 1.5          # 前場出来高 ÷ 7日平均全日出来高のしきい値
AVG_DAYS = 7                   # 平均をとる過去営業日数
MIN_AVG_VOLUME = 10_000        # 平均出来高(株数)の下限
MIN_AVG_VALUE = 1_000_000_000  # 平均売買代金の下限(円)。10億円/日
EXCLUDE_ZERO_VOL_DAYS = True   # 過去7日に出来高ゼロの日がある銘柄を除外
BATCH_SIZE = 200               # yfinanceの一括ダウンロード単位
EXCLUDE_SMALL_CAP = True       # 小型株を除外
AMA_START = "09:00"
AMA_END = "11:30"
NARABI_BODY_SIZE_RATIO_MIN = 0.8       # 並び赤2本の短い実体 ÷ 長い実体
NARABI_BODY_ALIGNMENT_RATIO = 0.2      # 始値・終値のずれの許容値（短い実体に対する比率）
PAGES_BASE_URL = "https://harumidiv.github.io/VolumeSpikeScreener"
# ========================================

JPX_LIST_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
JST = timezone(timedelta(hours=9))
MARKETS = ["プライム（内国株式）", "スタンダード（内国株式）", "グロース（内国株式）"]


def get_ticker_list():
    print("JPXから上場銘柄一覧を取得中...")
    resp = requests.get(JPX_LIST_URL, timeout=60)
    resp.raise_for_status()
    df = pd.read_excel(io.BytesIO(resp.content))
    df.columns = [str(c).strip() for c in df.columns]

    df = df[df["市場・商品区分"].isin(MARKETS)]

    if EXCLUDE_SMALL_CAP and "規模区分" in df.columns:
        df = df[df["規模区分"] != "小型株"]

    df = df[["コード", "銘柄名", "市場・商品区分", "規模区分"]].copy()
    df["コード"] = df["コード"].astype(str).str.strip()
    df["ticker"] = df["コード"] + ".T"
    print(f"対象銘柄数: {len(df)}")
    return df


def get_avg_volume(tickers_df):
    results = {}
    tickers = tickers_df["ticker"].tolist()

    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i:i + BATCH_SIZE]
        print(f"日足取得中 {i + 1}〜{min(i + BATCH_SIZE, len(tickers))} / {len(tickers)}")
        try:
            data = yf.download(
                batch, period="1mo", interval="1d", group_by="ticker",
                auto_adjust=False, threads=True, progress=False,
            )
        except Exception as e:
            print(f"  バッチ取得失敗: {e}")
            continue

        for t in batch:
            try:
                df = data[t] if len(batch) > 1 else data
                df = df.dropna(subset=["Volume", "Close"])
                if len(df) < AVG_DAYS + 1:
                    continue

                vol = df["Volume"]
                close = df["Close"]
                past_vol = vol.iloc[-(AVG_DAYS + 1):-1]
                past_close = close.iloc[-(AVG_DAYS + 1):-1]

                if EXCLUDE_ZERO_VOL_DAYS and (past_vol <= 0).any():
                    continue

                avg_vol = past_vol.mean()
                if avg_vol < MIN_AVG_VOLUME:
                    continue

                avg_value = (past_vol * past_close).mean()
                if avg_value < MIN_AVG_VALUE:
                    continue

                results[t] = {
                    "avg_vol": avg_vol,
                    "last_close": float(close.iloc[-1]),
                    "ohlcv": df.tail(63).copy(),
                }
            except Exception:
                continue
        time.sleep(1)

    return results


def get_ama_volume(tickers):
    results = {}

    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i:i + BATCH_SIZE]
        print(f"分足取得中 {i + 1}〜{min(i + BATCH_SIZE, len(tickers))} / {len(tickers)}")
        try:
            data = yf.download(
                batch, period="1d", interval="5m", group_by="ticker",
                auto_adjust=False, threads=True, progress=False,
            )
        except Exception as e:
            print(f"  バッチ取得失敗: {e}")
            continue

        for t in batch:
            try:
                df = data[t] if len(batch) > 1 else data
                df = df.dropna(subset=["Volume"])
                if df.empty:
                    continue

                if df.index.tz is None:
                    df.index = df.index.tz_localize("UTC").tz_convert(JST)
                else:
                    df.index = df.index.tz_convert(JST)

                ama = df.between_time(AMA_START, AMA_END)
                if ama.empty:
                    continue

                results[t] = {
                    "volume": int(ama["Volume"].sum()),
                    "today_open": float(ama["Open"].iloc[0]),
                    "today_high": float(ama["High"].max()),
                    "today_low": float(ama["Low"].min()),
                    "today_last": float(ama["Close"].iloc[-1]),
                }
            except Exception:
                continue
        time.sleep(1)

    return results


def _append_today_candle(ohlcv, ama_info):
    today = datetime.now(JST).date()
    if any(ts.date() == today for ts in ohlcv.index):
        return ohlcv
    ts = pd.Timestamp(today)
    if ohlcv.index.tz is not None:
        ts = ts.tz_localize(ohlcv.index.tz)
    row = pd.Series({
        "Open": ama_info["today_open"],
        "High": ama_info["today_high"],
        "Low": ama_info["today_low"],
        "Close": ama_info["today_last"],
        "Volume": float(ama_info["volume"]),
    }, name=ts)
    return pd.concat([ohlcv, row.to_frame().T.reindex(columns=ohlcv.columns)])


def check_uwabanaare_aka2(ohlcv, today_open, today_last):
    """上放れ赤2本: 前日ギャップアップ陽線 + 今日前場も陽線"""
    today_date = datetime.now(JST).date()
    idx_dates = [ts.date() for ts in ohlcv.index]
    complete = ohlcv[[d < today_date for d in idx_dates]]
    if len(complete) < 2:
        return False
    day_before = complete.iloc[-2]
    yesterday = complete.iloc[-1]
    gap_up = float(yesterday["Open"]) > float(day_before["High"])
    yesterday_bullish = float(yesterday["Close"]) > float(yesterday["Open"])
    today_bullish = today_last > today_open
    return gap_up and yesterday_bullish and today_bullish


def check_uwabanaare_narabiari(ohlcv, today_open, today_last):
    """上放れ並び赤: 窓を開けた陽線と、実体が横並びの陽線。"""
    today_date = datetime.now(JST).date()
    idx_dates = [ts.date() for ts in ohlcv.index]
    complete = ohlcv[[d < today_date for d in idx_dates]]
    if len(complete) < 2:
        return False
    day_before = complete.iloc[-2]
    yesterday = complete.iloc[-1]

    day_before_high = float(day_before["High"])
    yesterday_open = float(yesterday["Open"])
    yesterday_close = float(yesterday["Close"])
    yesterday_low = float(yesterday["Low"])

    # 始値だけでなく前日の安値が前々日の高値より上にあることを要求し、
    # ヒゲを含むローソク足同士に実際の窓が開いていることを確認する。
    gap_up = yesterday_low > day_before_high
    yesterday_bullish = yesterday_close > yesterday_open
    today_bullish = today_last > today_open

    if not (gap_up and yesterday_bullish and today_bullish):
        return False

    # 2本目の実体も窓より上に残っていることを確認する。
    if today_open <= day_before_high:
        return False

    yesterday_body = yesterday_close - yesterday_open
    today_body = today_last - today_open
    shorter_body = min(yesterday_body, today_body)
    longer_body = max(yesterday_body, today_body)

    # 実体の長さが近い2本だけを対象にする。
    if shorter_body / longer_body < NARABI_BODY_SIZE_RATIO_MIN:
        return False

    # 株価に対する±3%ではなく、実体の長さを基準に上下端のずれを測る。
    # これにより、実体が小さい銘柄で大きくずれた2本が候補になるのを防ぐ。
    alignment_tolerance = shorter_body * NARABI_BODY_ALIGNMENT_RATIO
    if abs(today_open - yesterday_open) > alignment_tolerance:
        return False
    if abs(today_last - yesterday_close) > alignment_tolerance:
        return False

    return True


def screen(tickers_df):
    name_map = dict(zip(tickers_df["ticker"], tickers_df["銘柄名"]))
    market_map = dict(zip(tickers_df["ticker"], tickers_df["市場・商品区分"]))
    scale_map = dict(zip(tickers_df["ticker"], tickers_df["規模区分"]))

    avg_data = get_avg_volume(tickers_df)
    valid_tickers = [t for t in tickers_df["ticker"] if t in avg_data]
    ama_data = get_ama_volume(valid_tickers)

    results = []
    pattern_results = []
    narabiari_results = []
    chart_data = {}
    for t in valid_tickers:
        if t not in ama_data:
            continue
        ama_info = ama_data[t]
        ama_vol = ama_info["volume"]
        avg_vol = avg_data[t]["avg_vol"]
        ratio = ama_vol / avg_vol
        ohlcv = avg_data[t]["ohlcv"]
        is_aka2 = check_uwabanaare_aka2(ohlcv, ama_info["today_open"], ama_info["today_last"])
        is_narabiari = check_uwabanaare_narabiari(ohlcv, ama_info["today_open"], ama_info["today_last"])

        if ratio >= RATIO_THRESHOLD:
            chart_data[t] = _append_today_candle(ohlcv, ama_info)
            results.append({
                "コード": t.replace(".T", ""),
                "銘柄名": name_map.get(t, ""),
                "市場": market_map.get(t, ""),
                "規模": scale_map.get(t, ""),
                "前場出来高": ama_vol,
                "7日平均出来高": int(avg_vol),
                "出来高倍率": round(float(ratio), 2),
                "上離れ赤2本": is_aka2,
                "上放れ並び赤": is_narabiari,
                "日付": datetime.now(JST).date(),
            })

        if is_aka2:
            if t not in chart_data:
                chart_data[t] = _append_today_candle(ohlcv, ama_info)
            pattern_results.append({
                "コード": t.replace(".T", ""),
                "銘柄名": name_map.get(t, ""),
                "出来高倍率": round(float(ratio), 2),
                "日付": datetime.now(JST).date(),
            })

        if is_narabiari:
            if t not in chart_data:
                chart_data[t] = _append_today_candle(ohlcv, ama_info)
            narabiari_results.append({
                "コード": t.replace(".T", ""),
                "銘柄名": name_map.get(t, ""),
                "出来高倍率": round(float(ratio), 2),
                "日付": datetime.now(JST).date(),
            })

    return pd.DataFrame(results), pd.DataFrame(pattern_results), pd.DataFrame(narabiari_results), chart_data


KABUTAN_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def get_kabutan_disclosures(code, target_date):
    """株探から直近7日以内の銘柄開示情報を取得する（最大3件）"""
    url = f"https://kabutan.jp/stock/news?code={code}&nmode=3"
    try:
        resp = requests.get(url, headers=KABUTAN_HEADERS, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print(f"  開示情報取得失敗 {code}: {e}")
        return []

    cutoff = target_date - timedelta(days=7)
    disclosures = []
    for row in resp.text.split("<tr>"):
        m_time = re.search(r'datetime="([^"]+)"', row)
        m_link = re.search(r'href="(https://kabutan\.jp/disclosures/[^"]+)"[^>]*>([^<]+)', row)
        if not m_time or not m_link:
            continue
        try:
            dt = datetime.fromisoformat(m_time.group(1)).date()
        except Exception:
            continue
        if cutoff < dt <= target_date:
            title = re.sub(r'\s+', ' ', m_link.group(2)).strip()
            disclosures.append((title, m_link.group(1)))

    return disclosures[:3]


def generate_chart(ticker, name, df):
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as f:
            tmp_path = f.name
        mpf.plot(
            df,
            type='candle',
            style='yahoo',
            title=f"{ticker.replace('.T', '')} {name}",
            volume=True,
            savefig=tmp_path,
            figsize=(10, 6),
        )
        with open(tmp_path, 'rb') as f:
            return f.read()
    except Exception as e:
        print(f"チャート生成失敗 {ticker}: {e}")
        return None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def build_mail(result, data_date, sender, mail_to, charts=None, disclosures_map=None):
    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = mail_to

    if result.empty:
        msg["Subject"] = f"[前場スクリーナー] {data_date} 該当なし"
        body = f"{data_date} の前場で、出来高が7日平均の{RATIO_THRESHOLD}倍を超えた銘柄はありませんでした。"
        msg.attach(MIMEText(body, "plain", "utf-8"))
        return msg

    msg["Subject"] = f"[前場スクリーナー] {data_date} 該当 {len(result)} 銘柄"

    rows = []
    for _, r in result.iterrows():
        code = r['コード']
        url = f"https://finance.yahoo.co.jp/quote/{code}.T"
        cid = f"chart_{code}"
        marks = []
        if r.get("上離れ赤2本"):
            marks.append("★")
        if r.get("上放れ並び赤"):
            marks.append("◆")
        pattern_mark = " ".join(marks)
        rows.append(
            f"<tr>"
            f"<td><a href='{url}'>{code} {r['銘柄名']}</a> {pattern_mark}</td>"
            f"<td style='text-align:right'>{r['出来高倍率']}倍</td>"
            f"</tr>"
        )
        if charts and (code + ".T") in charts and charts[code + ".T"]:
            rows.append(
                f"<tr><td colspan='2'><img src='cid:{cid}' style='max-width:100%'></td></tr>"
            )
        discs = disclosures_map.get(code, []) if disclosures_map else []
        if discs:
            links = "　".join(f"<a href='{u}'>{t}</a>" for t, u in discs)
            rows.append(f"<tr><td colspan='2' style='font-size:0.9em'>📋 開示: {links}</td></tr>")

    html = f"""<html><body>
<p><a href="{PAGES_BASE_URL}/ama.html">Webで確認する →</a></p>
<p>{data_date} 前場出来高急増銘柄（7日平均の{RATIO_THRESHOLD}倍超え）</p>
<table border='1' cellpadding='4' cellspacing='0'>
<tr><th>銘柄（★=上離れ赤2本 ◆=上放れ並び赤）</th><th>倍率</th></tr>
{"".join(rows)}
</table>
<p>詳細は添付CSVをご覧ください。</p>
</body></html>"""

    related = MIMEMultipart("related")
    related.attach(MIMEText(html, "html", "utf-8"))
    if charts:
        for ticker, img_bytes in charts.items():
            if img_bytes:
                code = ticker.replace(".T", "")
                img_part = MIMEImage(img_bytes)
                img_part.add_header("Content-ID", f"<chart_{code}>")
                img_part.add_header("Content-Disposition", "inline", filename=f"{code}.png")
                related.attach(img_part)
    msg.attach(related)

    csv_bytes = result.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
    part = MIMEApplication(csv_bytes, Name=f"ama_spike_{data_date}.csv")
    part["Content-Disposition"] = f'attachment; filename="ama_spike_{data_date}.csv"'
    msg.attach(part)

    return msg


def build_pattern_mail(pattern_result, data_date, sender, mail_to, charts=None):
    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = mail_to
    msg["Subject"] = f"[上離れ赤2本] {data_date} 該当 {len(pattern_result)} 銘柄"

    rows = []
    for _, r in pattern_result.iterrows():
        code = r["コード"]
        url = f"https://finance.yahoo.co.jp/quote/{code}.T"
        cid = f"chart_{code}"
        rows.append(
            f"<tr>"
            f"<td><a href='{url}'>{code} {r['銘柄名']}</a></td>"
            f"<td style='text-align:right'>{r['出来高倍率']}倍</td>"
            f"</tr>"
        )
        if charts and (code + ".T") in charts and charts[code + ".T"]:
            rows.append(
                f"<tr><td colspan='2'><img src='cid:{cid}' style='max-width:100%'></td></tr>"
            )

    html = f"""<html><body>
<p><a href="{PAGES_BASE_URL}/ama.html">Webで確認する →</a></p>
<p>{data_date} 前場時点で上離れ赤2本パターン検出銘柄（平均売買代金10億円/日以上）</p>
<table border='1' cellpadding='4' cellspacing='0'>
<tr><th>銘柄</th><th>前場出来高倍率</th></tr>
{"".join(rows)}
</table>
</body></html>"""

    related = MIMEMultipart("related")
    related.attach(MIMEText(html, "html", "utf-8"))
    if charts:
        for ticker, img_bytes in charts.items():
            code = ticker.replace(".T", "")
            if img_bytes and any(r["コード"] == code for _, r in pattern_result.iterrows()):
                img_part = MIMEImage(img_bytes)
                img_part.add_header("Content-ID", f"<chart_{code}>")
                img_part.add_header("Content-Disposition", "inline", filename=f"{code}.png")
                related.attach(img_part)
    msg.attach(related)

    return msg


def build_narabiari_pattern_mail(narabiari_result, data_date, sender, mail_to, charts=None):
    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = mail_to
    msg["Subject"] = f"[上放れ並び赤] {data_date} 該当 {len(narabiari_result)} 銘柄"

    rows = []
    for _, r in narabiari_result.iterrows():
        code = r["コード"]
        url = f"https://finance.yahoo.co.jp/quote/{code}.T"
        cid = f"chart_{code}"
        rows.append(
            f"<tr>"
            f"<td><a href='{url}'>{code} {r['銘柄名']}</a></td>"
            f"<td style='text-align:right'>{r['出来高倍率']}倍</td>"
            f"</tr>"
        )
        if charts and (code + ".T") in charts and charts[code + ".T"]:
            rows.append(
                f"<tr><td colspan='2'><img src='cid:{cid}' style='max-width:100%'></td></tr>"
            )

    html = f"""<html><body>
<p><a href="{PAGES_BASE_URL}/ama.html">Webで確認する →</a></p>
<p>{data_date} 前場時点で上放れ並び赤パターン検出銘柄（平均売買代金10億円/日以上）</p>
<p>上放れ並び赤: ギャップアップ後、前日と同水準・同実体サイズの陽線が続く強い上昇シグナル</p>
<table border='1' cellpadding='4' cellspacing='0'>
<tr><th>銘柄</th><th>前場出来高倍率</th></tr>
{"".join(rows)}
</table>
</body></html>"""

    related = MIMEMultipart("related")
    related.attach(MIMEText(html, "html", "utf-8"))
    if charts:
        for ticker, img_bytes in charts.items():
            code = ticker.replace(".T", "")
            if img_bytes and any(r["コード"] == code for _, r in narabiari_result.iterrows()):
                img_part = MIMEImage(img_bytes)
                img_part.add_header("Content-ID", f"<chart_{code}>")
                img_part.add_header("Content-Disposition", "inline", filename=f"{code}.png")
                related.attach(img_part)
    msg.attach(related)

    return msg


def send_mail(msg, sender, app_password):
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, app_password)
        server.send_message(msg)
    print(f"メール送信完了 → {msg['To']}")


def _write_index_html(data_date: str):
    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>スクリーナー {data_date}</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: sans-serif; }}
.header {{ background: #1a1a2e; color: #fff; padding: 12px 16px; font-size: 16px; font-weight: bold; }}
.tabs {{ display: flex; background: #16213e; }}
.tab-btn {{ flex: 1; padding: 12px; color: #aaa; background: none; border: none; cursor: pointer; font-size: 14px; border-bottom: 3px solid transparent; }}
.tab-btn.active {{ color: #fff; border-bottom-color: #4a90d9; background: #0f3460; }}
#content {{ padding: 0; min-height: 60vh; }}
</style>
</head>
<body>
<div class="header">スクリーナー結果 {data_date}</div>
<div class="tabs">
  <button class="tab-btn active" id="tab-ama" onclick="loadPage('ama.html', 'tab-ama')">前場</button>
  <button class="tab-btn" id="tab-eod" onclick="loadPage('eod.html', 'tab-eod')">引け後</button>
</div>
<div id="content"><p style="padding:16px">読み込み中...</p></div>
<script>
function loadPage(url, tabId) {{
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById(tabId).classList.add('active');
  fetch(url)
    .then(r => r.ok ? r.text() : null)
    .then(html => {{
      if (!html) {{ document.getElementById('content').innerHTML = '<p style="padding:16px;color:#888">まだデータがありません。</p>'; return; }}
      const doc = new DOMParser().parseFromString(html, 'text/html');
      document.getElementById('content').innerHTML = '<div style="padding:16px">' + doc.body.innerHTML + '</div>';
    }})
    .catch(() => {{ document.getElementById('content').innerHTML = '<p style="padding:16px;color:#888">読み込みに失敗しました。</p>'; }});
}}
loadPage('ama.html', 'tab-ama');
</script>
</body>
</html>"""
    os.makedirs("output", exist_ok=True)
    with open("output/index.html", "w", encoding="utf-8") as f:
        f.write(html)


def build_html_page(result, pattern_result, narabiari_result, data_date, charts):
    def make_rows(df):
        rows = []
        for _, r in df.iterrows():
            code = r["コード"]
            url = f"https://finance.yahoo.co.jp/quote/{code}.T"
            rows.append(
                f"<tr>"
                f"<td><a href='{url}'>{code} {r['銘柄名']}</a></td>"
                f"<td style='text-align:right'>{r['出来高倍率']}倍</td>"
                f"</tr>"
            )
            if charts and (code + ".T") in charts and charts[code + ".T"]:
                img_b64 = base64.b64encode(charts[code + ".T"]).decode('ascii')
                rows.append(
                    f"<tr><td colspan='2'><img src='data:image/png;base64,{img_b64}' style='max-width:100%'></td></tr>"
                )
        return "".join(rows)

    sections = []
    if not result.empty:
        sections.append(f"""<h2>出来高急増銘柄（前場） {data_date}</h2>
<p>前場出来高が7日平均の{RATIO_THRESHOLD}倍以上、平均売買代金{MIN_AVG_VALUE // 100_000_000}億円/日以上</p>
<table><tr><th>銘柄</th><th>倍率</th></tr>{make_rows(result)}</table>""")

    if not pattern_result.empty:
        sections.append(f"""<h2>上離れ赤2本パターン {data_date}</h2>
<table><tr><th>銘柄</th><th>倍率</th></tr>{make_rows(pattern_result)}</table>""")

    if not narabiari_result.empty:
        sections.append(f"""<h2>上放れ並び赤パターン {data_date}</h2>
<p>ギャップアップ後、前日と同水準・同実体サイズの陽線が続く強い上昇シグナル</p>
<table><tr><th>銘柄</th><th>倍率</th></tr>{make_rows(narabiari_result)}</table>""")

    body_content = "\n".join(sections) if sections else "<p>該当銘柄はありませんでした。</p>"

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>前場スクリーナー {data_date}</title>
<style>
body {{ font-family: sans-serif; max-width: 960px; margin: 0 auto; }}
h2 {{ font-size: 16px; margin: 16px 0 6px; }}
p {{ font-size: 13px; color: #555; margin-bottom: 12px; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13px; margin-bottom: 24px; }}
th, td {{ border: 1px solid #ddd; padding: 6px 10px; }}
th {{ background: #f5f5f5; text-align: left; }}
img {{ max-width: 100%; }}
a {{ color: #0066cc; text-decoration: none; }}
</style>
</head>
<body>
{body_content}
</body>
</html>"""

    os.makedirs("output", exist_ok=True)
    with open("output/ama.html", "w", encoding="utf-8") as f:
        f.write(html)
    _write_index_html(str(data_date))
    print("output/ama.html を保存しました")


def main():
    today = datetime.now(JST).date()
    if today.weekday() >= 5 or jpholiday.is_holiday(today):
        print(f"{today} は土日または祝日のため処理をスキップします。")
        return

    sender = os.environ.get("GMAIL_ADDRESS")
    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    mail_to = os.environ.get("MAIL_TO") or sender or ""
    mail_enabled = bool(sender and app_password)
    if not mail_enabled:
        print("GMAIL_ADDRESS / GMAIL_APP_PASSWORD が未設定のため、メール送信をスキップします。")

    tickers_df = get_ticker_list()
    result, pattern_result, narabiari_result, chart_data = screen(tickers_df)

    today_jst = datetime.now(JST).date()
    name_map = dict(zip(tickers_df["ticker"], tickers_df["銘柄名"]))

    # チャート生成（出来高急増＋パターン両方の対象）
    charts = {}
    all_codes = set()
    if not result.empty:
        all_codes.update(result["コード"].tolist())
    if not narabiari_result.empty:
        all_codes.update(narabiari_result["コード"].tolist())
    for code in all_codes:
        t = code + ".T"
        if t in chart_data:
            charts[t] = generate_chart(t, name_map.get(t, ""), chart_data[t])

    if mail_enabled:
        build_html_page(result, pattern_result, narabiari_result, today_jst, charts)

    if result.empty:
        print("出来高急増：該当銘柄なし。")
    else:
        result = result.sort_values("出来高倍率", ascending=False).reset_index(drop=True)
        print(result.to_string(index=False))

        if mail_enabled:
            print("開示情報を取得中...")
            disclosures_map = {}
            for _, row in result.iterrows():
                code = row["コード"]
                discs = get_kabutan_disclosures(code, today_jst)
                if discs:
                    disclosures_map[code] = discs
                    print(f"  {code}: {len(discs)}件")
                time.sleep(0.3)

            msg = build_mail(result, today_jst, sender, mail_to, charts, disclosures_map)
            send_mail(msg, sender, app_password)

    if pattern_result.empty:
        print("上離れ赤2本：該当銘柄なし。")
    else:
        pattern_result = pattern_result.sort_values("出来高倍率", ascending=False).reset_index(drop=True)
        print(f"上離れ赤2本 該当 {len(pattern_result)} 銘柄")
        print(pattern_result.to_string(index=False))

    if narabiari_result.empty:
        print("上放れ並び赤：該当銘柄なし。")
    else:
        narabiari_result = narabiari_result.sort_values("出来高倍率", ascending=False).reset_index(drop=True)
        print(f"上放れ並び赤 該当 {len(narabiari_result)} 銘柄")
        print(narabiari_result.to_string(index=False))

        if mail_enabled:
            msg = build_narabiari_pattern_mail(narabiari_result, today_jst, sender, mail_to, charts)
            send_mail(msg, sender, app_password)


if __name__ == "__main__":
    main()
