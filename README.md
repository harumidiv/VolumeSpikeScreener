# 日本株 出来高急増スクリーナー(GitHub Actions版)

平日17:00(JST)に自動実行し、直近営業日の出来高が過去7営業日平均の**2倍以上**になった銘柄を
`harumi.hobby@gmail.com` にメール通知します(本文にサマリー、CSVを添付)。

抽出条件:

- 当日出来高 ÷ 過去7営業日の平均出来高 ≥ 2.0
- 過去7営業日の平均売買代金 ≥ 10億円/日(閑散銘柄の除外)
- 過去7営業日に出来高ゼロの日がある銘柄は除外
- 祝日など休場日は自動スキップ(メールなし)

## セットアップ手順

### 1. Gmailアプリパスワードの発行

1. Googleアカウントで**2段階認証を有効化**(未設定の場合)
2. https://myaccount.google.com/apppasswords を開く
3. アプリ名を適当に入力(例: `screener`)して作成
4. 表示された**16桁のパスワード**を控える(この画面でしか見られません)

### 2. GitHubリポジトリの作成とアップロード

1. GitHubで新規リポジトリを作成(**Private推奨**)
2. このフォルダの中身を丸ごとアップロード
   - `screener.py`
   - `requirements.txt`
   - `.github/workflows/screener.yml` ← フォルダ構造ごと必要です

### 3. Secretsの登録

リポジトリの **Settings → Secrets and variables → Actions → New repository secret** で2つ登録:

| Name | Value |
|---|---|
| `GMAIL_ADDRESS` | 送信元のGmailアドレス |
| `GMAIL_APP_PASSWORD` | 手順1で発行した16桁のアプリパスワード |

### 4. 動作テスト

**Actions タブ → Volume Spike Screener → Run workflow** で手動実行できます。
10〜20分ほどでメールが届けば成功です。

## カスタマイズ

`screener.py` 冒頭の設定を編集:

- `RATIO_THRESHOLD` : 出来高倍率のしきい値(既定 2.0)
- `MIN_AVG_VALUE` : 平均売買代金の下限(既定 10億円)
- `MAIL_TO` : 通知先メールアドレス
- 実行時刻を変えるには `.github/workflows/screener.yml` の cron を編集
  (UTC指定。JST−9時間。例: 18:00 JST → `0 9 * * 1-5`)

## 注意事項

- Yahoo Financeの非公式データを利用しているため、仕様変更で動かなくなる可能性があります
- データ反映は大引け後30分〜1時間程度かかるため、実行時刻は16:30 JST以降を推奨
- 60日以上pushがないリポジトリはGitHubがスケジュール実行を自動停止します
  (メールで通知が来るので、その場合はActionsタブから再有効化してください)
