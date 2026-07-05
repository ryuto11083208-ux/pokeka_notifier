# pokeka_notifier

ポケモンカードの抽選・予約情報（入荷Now / 攻略大百科 / ポケカウォッチ）を定期取得し、
新着・更新をDiscordに通知するスクリプト。GitHub Actionsで15分おきに実行する想定。

## 監視対象

- 入荷Now（`https://nyuka-now.com/archives/2459`）: まとめページの「抽選・予約応募受付中のストア」
  「近日受付開始予定のストア」をスクレイピングし、店舗ごとの内容をハッシュ比較して新着・更新を検知。
- 攻略大百科（`https://premium.gamepedia.jp/pokeca/archives/124`）: 「受付中のショップ」一覧から
  各ショップの詳細（抽選開始/終了日時・結果発表・購入期間・応募URL等）を取得し、新着・更新を検知。
- ポケカウォッチ（抽選・予約情報カテゴリのRSS）: 新着記事を検知。

## セットアップ手順

### 1. Discord Webhookを作成する

1. 通知を受けたいDiscordサーバーの対象チャンネルの設定を開く
2. 「連携サービス」→「ウェブフック」→「新しいウェブフック」を作成
3. 発行されたWebhook URLをコピーしておく
4. スマホのDiscordアプリでそのチャンネルの通知をON（プッシュ通知が来るようになる）

### 2. GitHubリポジトリを作成してpushする

このディレクトリをリポジトリのルートとして、GitHubにpushする。

### 3. Secretsを設定する

リポジトリの Settings → Secrets and variables → Actions → New repository secret で

- `DISCORD_WEBHOOK_URL` : 手順1で発行したWebhook URL

を登録する。

### 4. Actionsを有効化する

push後、Actionsタブでワークフロー `Pokeka Lottery Notifier` が15分おきに自動実行される。
初回実行時は既存の抽選情報をすべて「新着」として通知してしまわないよう、
通知を送らずに現在の状態のみを保存する（2回目以降の実行から差分通知が始まる）。

手動で今すぐ動かしたい場合はActionsタブから `Run workflow` で実行できる。

## ローカルでのテスト

```
pip install -r requirements.txt
$env:DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/xxxx"
python main.py
```

## 注意点

- 入荷Now・攻略大百科のまとめ記事URLはサイト側の運用が変わると差し替えられる可能性がある。
  ページが見つからなくなった場合は環境変数 `NYUKA_NOW_URL` / `GAMEPEDIA_URL` で最新のURLに差し替える。
- スクレイピング対象サイトのHTML構造が変わると抽出に失敗する可能性がある。
- 攻略大百科は「受付中のショップ」一覧に載っていても詳細ページ（開始日時等）がまだ用意されていない
  ショップがあり、その場合は一覧の簡易情報（ショップ名・種別・受付終了日時）のみで通知する。
