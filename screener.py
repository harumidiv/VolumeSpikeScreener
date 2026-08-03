# -*- coding: utf-8 -*-
"""
日本株 出来高急増スクリーナー(GitHub Actions + Gmail通知版)
直近営業日の出来高が、その前7営業日の平均出来高の2倍以上の銘柄を抽出してメール送信する。

必要な環境変数(GitHub Secretsに設定):
    GMAIL_ADDRESS      送信元Gmailアドレス
    GMAIL_APP_PASSWORD Gmailのアプリパスワード(16桁)
"""

import io
import os
import sys
import time
import smtplib
import requests
import pandas as pd
import yfinance as yf
from datetime import date, datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
plt.rcParams['font.family'] = ['Noto Sans CJK JP', 'sans-serif']
import mplfinance as mpf
import tempfile

# ================= 設定 =================
RATIO_THRESHOLD = 2.0          # 出来高倍率のしきい値(2倍以上)
AVG_DAYS = 7                   # 平均をとる過去営業日数
MIN_AVG_VOLUME = 10_000        # 平均出来高(株数)の下限
MIN_AVG_VALUE = 1_000_000_000  # 平均売買代金の下限(円)。10億円/日
EXCLUDE_ZERO_VOL_DAYS = True   # 過去7日に出来高ゼロの日がある銘柄を除外
BATCH_SIZE = 200               # yfinanceの一括ダウンロード単位
SKIP_IF_STALE = True           # 最新データが当日でない(=休場日)ならメールを送らず終了
MAIL_TO = os.environ.get("MAIL_TO", "")
MARKETS = ["プライム（内国株式）", "スタンダード（内国株式）", "グロース（内国株式）"]
# 全市場対象にするなら MARKETS = None
# ========================================

JPX_LIST_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
JST = timezone(timedelta(hours=9))


def get_ticker_list():
    print("JPXから上場銘柄一覧を取得中...")
    resp = requests.get(JPX_LIST_URL, timeout=60)
    resp.raise_for_status()
    df = pd.read_excel(io.BytesIO(resp.content))
    df.columns = [str(c).strip() for c in df.columns]

    if MARKETS:
        df = df[df["市場・商品区分"].isin(MARKETS)]

    df = df[["コード", "銘柄名", "市場・商品区分"]].copy()
    df["コード"] = df["コード"].astype(str).str.strip()
    df["ticker"] = df["コード"] + ".T"
    print(f"対象銘柄数: {len(df)}")
    return df


def screen(tickers_df):
    results = []
    chart_data = {}
    latest_date = None
    tickers = tickers_df["ticker"].tolist()
    name_map = dict(zip(tickers_df["ticker"], tickers_df["銘柄名"]))
    market_map = dict(zip(tickers_df["ticker"], tickers_df["市場・商品区分"]))

    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i:i + BATCH_SIZE]
        print(f"取得中 {i + 1}〜{min(i + BATCH_SIZE, len(tickers))} / {len(tickers)}")
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

                today_vol = vol.iloc[-1]
                avg_vol = past_vol.mean()
                if avg_vol < MIN_AVG_VOLUME:
                    continue

                avg_value = (past_vol * past_close).mean()
                if avg_value < MIN_AVG_VALUE:
                    continue

                d = vol.index[-1].date()
                latest_date = max(latest_date, d) if latest_date else d

                ratio = today_vol / avg_vol
                if ratio >= RATIO_THRESHOLD:
                    chart_data[t] = df.tail(63).copy()
                    prev_close = close.iloc[-2] if len(close) >= 2 else None
                    last_close = close.iloc[-1]
                    chg = (last_close / prev_close - 1) * 100 if prev_close else None
                    results.append({
                        "コード": t.replace(".T", ""),
                        "銘柄名": name_map.get(t, ""),
                        "市場": market_map.get(t, ""),
                        "終値": round(float(last_close), 1),
                        "前日比%": round(float(chg), 2) if chg is not None else None,
                        "当日出来高": int(today_vol),
                        "7日平均出来高": int(avg_vol),
                        "7日平均売買代金(百万円)": round(float(avg_value) / 1_000_000, 1),
                        "出来高倍率": round(float(ratio), 2),
                        "日付": d,
                    })
            except Exception:
                continue
        time.sleep(1)

    return pd.DataFrame(results), latest_date, chart_data


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


def build_mail(result: pd.DataFrame, data_date, sender: str, charts=None):
    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = MAIL_TO

    if result.empty:
        msg["Subject"] = f"[出来高スクリーナー] {data_date} 該当なし"
        body = f"{data_date} の取引で、出来高が7日平均の{RATIO_THRESHOLD}倍以上となった銘柄はありませんでした。\n(フィルタ: 平均売買代金{MIN_AVG_VALUE // 100_000_000}億円/日以上)"
        msg.attach(MIMEText(body, "plain", "utf-8"))
        return msg

    msg["Subject"] = f"[出来高スクリーナー] {data_date} 該当 {len(result)} 銘柄"

    lines = [f"{data_date} の出来高急増銘柄(7日平均の{RATIO_THRESHOLD}倍以上、平均売買代金{MIN_AVG_VALUE // 100_000_000}億円/日以上)\n"]
    for _, r in result.iterrows():
        chg = f"{r['前日比%']:+.2f}%" if pd.notna(r["前日比%"]) else "-"
        lines.append(
            f"{r['コード']} {r['銘柄名']} [{r['市場']}]\n"
            f"  終値 {r['終値']:,}円 ({chg}) / 出来高倍率 {r['出来高倍率']}倍 "
            f"(当日 {r['当日出来高']:,} / 7日平均 {r['7日平均出来高']:,})\n"
        )
    lines.append("\n詳細は添付CSVをご覧ください。")
    msg.attach(MIMEText("\n".join(lines), "plain", "utf-8"))

    csv_bytes = result.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
    part = MIMEApplication(csv_bytes, Name=f"volume_spike_{data_date}.csv")
    part["Content-Disposition"] = f'attachment; filename="volume_spike_{data_date}.csv"'
    msg.attach(part)

    if charts:
        for ticker, img_bytes in charts.items():
            if img_bytes:
                code = ticker.replace('.T', '')
                part = MIMEApplication(img_bytes, Name=f"{code}.png")
                part["Content-Disposition"] = f'attachment; filename="{code}.png"'
                msg.attach(part)

    return msg


def send_mail(msg, sender, app_password):
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, app_password)
        server.send_message(msg)
    print(f"メール送信完了 → {MAIL_TO}")


def main():
    sender = os.environ.get("GMAIL_ADDRESS")
    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    mail_enabled = bool(sender and app_password)
    if not mail_enabled:
        print("GMAIL_ADDRESS / GMAIL_APP_PASSWORD が未設定のため、メール送信をスキップします。")

    tickers_df = get_ticker_list()
    result, latest_date, chart_data = screen(tickers_df)

    today_jst = datetime.now(JST).date()
    if SKIP_IF_STALE and latest_date and latest_date < today_jst:
        print(f"最新データ日付 {latest_date} が本日 {today_jst} より古いため休場日と判断し、メールは送信しません。")
        return

    if result.empty:
        print("該当銘柄なし。メールは送信しません。")
        return

    result = result.sort_values("出来高倍率", ascending=False).reset_index(drop=True)
    print(result.to_string(index=False))

    if not mail_enabled:
        return

    name_map = dict(zip(tickers_df["ticker"], tickers_df["銘柄名"]))
    charts = {}
    for _, row in result.iterrows():
        t = row["コード"] + ".T"
        if t in chart_data:
            charts[t] = generate_chart(t, name_map.get(t, ""), chart_data[t])

    msg = build_mail(result, latest_date or today_jst, sender, charts)
    send_mail(msg, sender, app_password)


if __name__ == "__main__":
    main()
