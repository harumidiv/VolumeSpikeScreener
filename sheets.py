# -*- coding: utf-8 -*-
"""
スクリーニング結果をGoogleスプレッドシートに追記し、一定期間後の株価を追跡するモジュール。

必要な環境変数(GitHub Secretsに設定):
    GOOGLE_SERVICE_ACCOUNT_JSON  サービスアカウントの認証情報JSON(中身をそのまま貼り付け)
    SPREADSHEET_ID               書き込み先スプレッドシートのID
                                 (URLの /spreadsheets/d/ と /edit の間の文字列)

シート構成(1行目がヘッダー):
    検出日 / 区分 / コード / 銘柄名 / 市場 / 検出時株価 / 出来高倍率
    / 1ヶ月後日付 / 1ヶ月後株価 / 1ヶ月騰落%
"""

import json
import os
import time
from datetime import date, datetime, timezone, timedelta

import pandas as pd
import yfinance as yf

JST = timezone(timedelta(hours=9))

WORKSHEET_NAME = "tracking"
FOLLOWUP_DAYS = 30             # 検出から何日後(暦日)の株価を追跡するか
BATCH_SIZE = 200               # yfinanceの一括ダウンロード単位

HEADER = [
    "検出日", "区分", "コード", "銘柄名", "市場", "検出時株価", "出来高倍率",
    "1ヶ月後日付", "1ヶ月後株価", "1ヶ月騰落%",
]

# 列番号(0始まり)
COL_DATE = 0
COL_KIND = 1
COL_CODE = 2
COL_PRICE = 5
COL_FOLLOW_DATE = 7
COL_FOLLOW_PRICE = 8
COL_FOLLOW_CHG = 9


def _resolve_worksheet():
    """認証してワークシートを返す。未設定・失敗時は None を返す(処理は継続する)。"""
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    sheet_id = os.environ.get("SPREADSHEET_ID")
    if not raw or not sheet_id:
        print("GOOGLE_SERVICE_ACCOUNT_JSON / SPREADSHEET_ID が未設定のため、スプレッドシート連携をスキップします。")
        return None

    try:
        import gspread
        from google.oauth2.service_account import Credentials

        creds = Credentials.from_service_account_info(
            json.loads(raw),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        client = gspread.authorize(creds)
        book = client.open_by_key(sheet_id)
    except Exception as e:
        print(f"スプレッドシートの認証・オープンに失敗しました: {e}")
        return None

    try:
        ws = book.worksheet(WORKSHEET_NAME)
    except Exception:
        ws = book.add_worksheet(title=WORKSHEET_NAME, rows=1000, cols=len(HEADER))
        ws.update(values=[HEADER], range_name="A1", value_input_option="RAW")
        print(f"ワークシート '{WORKSHEET_NAME}' を新規作成しました。")
        return ws

    # 既存シートが空ならヘッダーだけ入れる
    if not ws.get_all_values():
        ws.update(values=[HEADER], range_name="A1", value_input_option="RAW")

    return ws


_WS_CACHE = []


def _open_worksheet():
    """ワークシートを一度だけ解決してキャッシュする。未設定・失敗時は None。"""
    if not _WS_CACHE:
        _WS_CACHE.append(_resolve_worksheet())
    return _WS_CACHE[0]


def _to_date(s):
    """シート上の日付文字列を date に変換する。失敗時は None。"""
    s = str(s).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def build_records(df, kind, price_col, date_col="日付"):
    """スクリーニング結果のDataFrameをシート追記用の辞書リストに変換する。

    df        : スクリーニング結果
    kind      : 区分名(例: "引け後急増")
    price_col : 検出時株価として使う列名
    """
    if df is None or df.empty:
        return []

    records = []
    for _, r in df.iterrows():
        detect_date = r.get(date_col)
        if isinstance(detect_date, (datetime, pd.Timestamp)):
            detect_date = detect_date.date()
        if not isinstance(detect_date, date):
            detect_date = datetime.now(JST).date()

        price = r.get(price_col)
        records.append({
            "検出日": detect_date.isoformat(),
            "区分": kind,
            "コード": str(r["コード"]),
            "銘柄名": r.get("銘柄名", ""),
            "市場": r.get("市場", ""),
            "検出時株価": round(float(price), 1) if pd.notna(price) else "",
            "出来高倍率": float(r["出来高倍率"]) if pd.notna(r.get("出来高倍率")) else "",
        })
    return records


def append_detections(records):
    """検出結果をシートに追記する。(検出日, 区分, コード)が既にあれば追記しない。"""
    if not records:
        return

    ws = _open_worksheet()
    if ws is None:
        return

    existing = ws.get_all_values()
    known = {
        (row[COL_DATE].strip(), row[COL_KIND].strip(), row[COL_CODE].strip())
        for row in existing[1:] if len(row) > COL_CODE
    }

    rows = []
    for rec in records:
        key = (rec["検出日"], rec["区分"], rec["コード"])
        if key in known:
            continue
        known.add(key)
        rows.append([
            rec["検出日"], rec["区分"], rec["コード"], rec["銘柄名"], rec["市場"],
            rec["検出時株価"], rec["出来高倍率"], "", "", "",
        ])

    if not rows:
        print(f"スプレッドシート: {records[0]['区分']} は全て記録済みのため追記なし。")
        return

    ws.append_rows(rows, value_input_option="RAW")
    print(f"スプレッドシートに {len(rows)} 行追記しました（{records[0]['区分']}）。")


def _fetch_followup_prices(targets):
    """targets: [(code, detect_date), ...]
    戻り値: {(code, detect_date): (約定日, 終値)}"""
    if not targets:
        return {}

    codes = sorted({c for c, _ in targets})
    start = min(d for _, d in targets)
    end = datetime.now(JST).date() + timedelta(days=1)

    frames = {}
    for i in range(0, len(codes), BATCH_SIZE):
        batch = [c + ".T" for c in codes[i:i + BATCH_SIZE]]
        print(f"追跡用データ取得中 {i + 1}〜{min(i + BATCH_SIZE, len(codes))} / {len(codes)}")
        try:
            data = yf.download(
                batch, start=start.isoformat(), end=end.isoformat(), interval="1d",
                group_by="ticker", auto_adjust=False, threads=True, progress=False,
            )
        except Exception as e:
            print(f"  バッチ取得失敗: {e}")
            continue

        for t in batch:
            try:
                df = data[t] if len(batch) > 1 else data
                df = df.dropna(subset=["Close"])
                if not df.empty:
                    frames[t.replace(".T", "")] = df
            except Exception:
                continue
        time.sleep(1)

    out = {}
    for code, detect_date in targets:
        df = frames.get(code)
        if df is None:
            continue
        threshold = detect_date + timedelta(days=FOLLOWUP_DAYS)
        after = df[[ts.date() >= threshold for ts in df.index]]
        if after.empty:
            continue
        out[(code, detect_date)] = (
            after.index[0].date(),
            round(float(after["Close"].iloc[0]), 1),
        )
    return out


def fill_followups():
    """1ヶ月後の株価が未記入で、既に経過日数を満たしている行を埋める。"""
    ws = _open_worksheet()
    if ws is None:
        return

    values = ws.get_all_values()
    if len(values) < 2:
        return

    today = datetime.now(JST).date()
    pending = []   # (シート行番号, code, detect_date, 検出時株価)
    for i, row in enumerate(values[1:], start=2):
        if len(row) <= COL_CODE:
            continue
        if len(row) > COL_FOLLOW_PRICE and str(row[COL_FOLLOW_PRICE]).strip():
            continue  # 記入済み

        detect_date = _to_date(row[COL_DATE])
        if detect_date is None:
            continue
        if detect_date + timedelta(days=FOLLOWUP_DAYS) > today:
            continue  # まだ経過していない

        try:
            base_price = float(str(row[COL_PRICE]).replace(",", ""))
        except (ValueError, IndexError):
            continue

        pending.append((i, str(row[COL_CODE]).strip(), detect_date, base_price))

    if not pending:
        print("追跡対象の行はありません。")
        return

    print(f"追跡対象 {len(pending)} 行の株価を取得します。")
    prices = _fetch_followup_prices([(c, d) for _, c, d, _ in pending])

    updates = []
    for row_no, code, detect_date, base_price in pending:
        got = prices.get((code, detect_date))
        if got is None:
            continue
        follow_date, follow_price = got
        chg = round((follow_price / base_price - 1) * 100, 2) if base_price else ""
        updates.append({
            "range": f"H{row_no}:J{row_no}",
            "values": [[follow_date.isoformat(), follow_price, chg]],
        })

    if not updates:
        print("株価を取得できた行がありませんでした。")
        return

    ws.batch_update(updates, value_input_option="RAW")
    print(f"{len(updates)} 行の1ヶ月後株価を記入しました。")
