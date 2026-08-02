# -*- coding: utf-8 -*-
"""
日本株 前場出来高急増スクリーナー(GitHub Actions + Gmail通知版)
前場(9:00-11:30)の出来高が過去7営業日の平均全日出来高を上回った銘柄を抽出してメール送信する。

必要な環境変数(GitHub Secretsに設定):
    GMAIL_ADDRESS      送信元Gmailアドレス
    GMAIL_APP_PASSWORD Gmailのアプリパスワード(16桁)
    MAIL_TO            通知先メールアドレス(省略時はGMAIL_ADDRESSと同じ)
"""

import io
import os
import time
import smtplib
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

# ================= 設定 =================
RATIO_THRESHOLD = 1.0          # 前場出来高 ÷ 7日平均全日出来高のしきい値
AVG_DAYS = 7                   # 平均をとる過去営業日数
MIN_AVG_VOLUME = 10_000        # 平均出来高(株数)の下限
MIN_AVG_VALUE = 1_000_000_000  # 平均売買代金の下限(円)
EXCLUDE_ZERO_VOL_DAYS = True   # 過去7日に出来高ゼロの日がある銘柄を除外
BATCH_SIZE = 200               # yfinanceの一括ダウンロード単位
EXCLUDE_SMALL_CAP = True       # 小型株を除外
AMA_START = "09:00"
AMA_END = "11:30"
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

                results[t] = int(ama["Volume"].sum())
            except Exception:
                continue
        time.sleep(1)

    return results


def screen(tickers_df):
    name_map = dict(zip(tickers_df["ticker"], tickers_df["銘柄名"]))
    market_map = dict(zip(tickers_df["ticker"], tickers_df["市場・商品区分"]))
    scale_map = dict(zip(tickers_df["ticker"], tickers_df["規模区分"]))

    avg_data = get_avg_volume(tickers_df)
    valid_tickers = [t for t in tickers_df["ticker"] if t in avg_data]
    ama_data = get_ama_volume(valid_tickers)

    results = []
    for t in valid_tickers:
        if t not in ama_data:
            continue
        ama_vol = ama_data[t]
        avg_vol = avg_data[t]["avg_vol"]
        ratio = ama_vol / avg_vol
        if ratio >= RATIO_THRESHOLD:
            results.append({
                "コード": t.replace(".T", ""),
                "銘柄名": name_map.get(t, ""),
                "市場": market_map.get(t, ""),
                "規模": scale_map.get(t, ""),
                "前場出来高": ama_vol,
                "7日平均出来高": int(avg_vol),
                "出来高倍率": round(float(ratio), 2),
                "日付": datetime.now(JST).date(),
            })

    return pd.DataFrame(results)


def build_mail(result, data_date, sender, mail_to):
    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = mail_to

    if result.empty:
        msg["Subject"] = f"[前場スクリーナー] {data_date} 該当なし"
        body = f"{data_date} の前場で、出来高が7日平均を超えた銘柄はありませんでした。"
        msg.attach(MIMEText(body, "plain", "utf-8"))
        return msg

    msg["Subject"] = f"[前場スクリーナー] {data_date} 該当 {len(result)} 銘柄"
    lines = [f"{data_date} 前場出来高急増銘柄(7日平均全日出来高超え)\n"]
    for _, r in result.iterrows():
        lines.append(
            f"{r['コード']} {r['銘柄名']} [{r['市場']}／{r['規模']}]\n"
            f"  前場出来高 {r['前場出来高']:,} / 7日平均 {r['7日平均出来高']:,} "
            f"(倍率 {r['出来高倍率']}倍)\n"
        )
    lines.append("\n詳細は添付CSVをご覧ください。")
    msg.attach(MIMEText("\n".join(lines), "plain", "utf-8"))

    csv_bytes = result.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
    part = MIMEApplication(csv_bytes, Name=f"ama_spike_{data_date}.csv")
    part["Content-Disposition"] = f'attachment; filename="ama_spike_{data_date}.csv"'
    msg.attach(part)
    return msg


def send_mail(msg, sender, app_password):
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, app_password)
        server.send_message(msg)
    print(f"メール送信完了 → {msg['To']}")


def main():
    sender = os.environ.get("GMAIL_ADDRESS")
    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    mail_to = os.environ.get("MAIL_TO") or sender or ""
    mail_enabled = bool(sender and app_password)
    if not mail_enabled:
        print("GMAIL_ADDRESS / GMAIL_APP_PASSWORD が未設定のため、メール送信をスキップします。")

    tickers_df = get_ticker_list()
    result = screen(tickers_df)

    today_jst = datetime.now(JST).date()

    if not result.empty:
        result = result.sort_values("出来高倍率", ascending=False).reset_index(drop=True)
        print(result.to_string(index=False))

    if not mail_enabled:
        return

    msg = build_mail(result, today_jst, sender, mail_to)
    send_mail(msg, sender, app_password)


if __name__ == "__main__":
    main()
