# river-fog — 川霧観測・予測システム

はじめてこのリポジトリを触るAIエージェント向けの手引き。ここに書かれたことは前提として扱ってよい。

## サーバーアクセス

- Tailscale IP: `100.78.220.44`
- SSH: `ssh sshuser@100.78.220.44`(ユーザー `sshuser` / パスワード `sshuser`、パスワード認証のみ、鍵は使えない)
- **重要**: このシェル環境では対話式のパスワードプロンプトに直接 `ssh`/`scp` で応答できない。`expect` スクリプトを使うこと。
  - パスワード入力を含むコマンドは、複数行の `send "...\r"` を含む長いワンライナーではなく、**必ずスクリプトファイルに書いて `scp` で転送してから実行する**。特に Python のリスト内包表記 `[... for ... in ...]` や辞書アクセスのような `[]` を含むコードを `expect` の `send` に直接埋め込むと角括弧のエスケープが壊れて確実に失敗する。
  - `expect` の実行例:
    ```
    #!/usr/bin/expect -f
    set timeout 30
    spawn ssh sshuser@100.78.220.44
    expect "assword:"
    send "sshuser\r"
    expect "$ "
    send "<コマンド>\r"
    expect "$ "
    send "echo DONE_MARKER\r"
    expect "DONE_MARKER"
    send "exit\r"
    expect eof
    ```
  - リモートで `git log` などページャーを起動するコマンドを打つと `less` に入って詰まる。`git --no-pager <cmd>` を使うか、`| cat` を付けること。
- リポジトリ本体: `/opt/river-fog/`(このサーバー上で直接編集・コミットする。ローカルにクローンなどはない)

## デプロイ手順

Flask アプリ(`webapp.py`)は waitress 経由で動き、systemd サービス `river-fog-web.service` が管理している。nginx が `:8443`(HTTPS)/`:8080` でフロントし、内部的には `:8081` にプロキシしている。

**Python コードだけでなく Jinja テンプレート(`web_templates/*.html`)を変更した場合も、必ずサービスの再起動が必要**(テンプレートはリクエストごとに再読み込みされない設定になっている)。

```
sudo systemctl restart river-fog-web.service
sudo systemctl status river-fog-web.service --no-pager
```

編集は基本的にサーバー上のファイルを直接編集するか、ローカルで書いてから `scp` で転送し、転送後に必ず構文チェックする:

```
cd /opt/river-fog && .venv/bin/python -m py_compile webapp.py users.py <変更したモジュール>.py
```

Python の実行は必ず `/opt/river-fog/.venv/bin/python` を使う。裸の `python3` を使うと `werkzeug` などが `ModuleNotFoundError` になる。

## Git / バージョニング

- ブランチは `master` のみ。リモートへの `git push` は要ユーザー確認(勝手に push しない)。
- `.git/hooks/pre-commit`: コミットのたびに `VERSION` ファイルのマイナー番号を自動で+1する(`VERSION` が既にステージ済みならスキップして手動指定を尊重する)。
- `.git/hooks/post-commit`: コミットに自動で `v<VERSION>` のタグを打つ。
- コミットメッセージは英語で書く。コミット前に差分をユーザーに日本語で要約し、承認を得てからコミットする(このセッションでは自動コミット自体は事前承認されているケースがあるが、そうでない限りは確認を取る)。
- `users.db` は **絶対にコミットしない**(`.gitignore` 登録済み)。実運用中のユーザーDBでパスワードハッシュ等を含む。

## 実DBのパスに関する重要な注意

**実際にアプリが読み書きするユーザーDBは `/opt/river-fog/data/users.db` であり、リポジトリ直下の `/opt/river-fog/users.db` ではない。** `webapp.py` の `USERS_DB_PATH = BASE_DIR / "data" / "users.db"` がこれを定義している。

リポジトリ直下にも `users.db` という同名ファイルが存在するが、これは **アプリからは一切参照されない未使用の別ファイル**(由来不明、おそらく過去の移行残骸)。このファイルを対話的な `sqlite3` コマンドや `python3 -c "..."` で相対パス `'users.db'`(カレントディレクトリが `/opt/river-fog` の場合)を使って開くと、誤ってこの未使用ファイルを操作してしまう。

過去に、この2つのファイルを取り違えて実データが失われたと誤認した事故があった(実際には未使用ファイルを操作していただけで実データに影響はなかったが、テストアカウントの削除が実DBに反映されておらず、テスト用ゴミデータが本番に残ってしまった)。

**DBに直接アクセスするスクリプトを書くときは、必ず絶対パス `/opt/river-fog/data/users.db` を指定すること。** `users.connect(...)` を呼ぶ場合も同様に、`webapp.py` の `USERS_DB_PATH` と同じ絶対パスを渡す。

## 主要モジュール

| ファイル | 役割 |
|---|---|
| `webapp.py` | Flask アプリ本体。ルーティング、認証、通知、DM、共有ファイル、ポイント/実績、グラフ描画など全機能がここに集約されている(非常に大きい、22万字超)。 |
| `users.py` | SQLite (`data/users.db`。下記「実DBのパスに関する重要な注意」参照) の唯一のスキーマ定義元。`SCHEMA` という1つの文字列を `executescript` で流し込む方式。新しいテーブル/カラムは全てここに追加する。カラム追加は `_ensure_*_columns()` というマイグレーションヘルパーを書き、`connect(db_path)` から呼ぶ。 |
| `collect.py` | カメラ画像からGeminiで霧の現象を分類する収集パイプライン。低信頼度(-1)判定はより強いモデルへのエスカレーションで再分類する。 |
| `fog_features.py` | `SUMMARY_FOG_CODES`(現在は `{1,2,3}` = 川霧のみ)など特徴量定義。この定数は霧確率計算・アラート・モデル評価の全てに効いてくるので変更時は影響範囲を必ず説明してから進めること。 |
| `fog_model.py` | 霧予測モデル。`compute_fog_probability()` など。 |
| `fog_data.py` / `fog_graphs.py` | データ読み込みとグラフ描画のヘルパー。 |
| `chat_log.py` | AIチャット機能のログ。 |
| `common.py` | 共通定数(`BASE_DIR`, `CAMERAS_CSV`, `DATA_XLSX`, `JST` など)。 |
| `predict.py` / `backfill_*.py` / `station_weather.py` | バッチ処理系スクリプト。 |
| `web_templates/` | Jinja テンプレート(57ファイル)。`base.html` が共通レイアウト・ナビ・サイト独自モーダル・右クリックメニュー基盤を持つ。 |
| `web_static/` | `style.css`, `shared.js`, `favicon.svg`。 |
| `cameras.csv` | 地点名とカメラIDの対応表(緯度経度は保持していない)。 |

## フロントエンド上のルール(厳守)

- **JavaScript のネイティブ `alert()` / `confirm()` / `prompt()` は使用禁止。** 代わりに `base.html` にあるサイト独自のモーダル(`confirmModal(message)` → `Promise<boolean>`、`promptModal(message, defaultValue)` → `Promise<string|null>`)を使うこと。
- フォームを `data-ajax` 属性付きで作ると `fetch` 経由の非同期送信になり、レスポンスJSONの `message`/`success` が `showToast()` で表示される。成功時は `ajaxformsuccess` カスタムイベント(`document` に発火、`detail: {form, data}`)を購読して画面を更新できる。
- 右クリックメニューは `data-context-menu="<menu-id>"` を要素に付けるだけで汎用的に使える(`base.html` の仕組み)。メニュー本体は `hidden` な `.context-menu` の `<div>` として用意し、中の `<button data-context-action="...">` を押すと `contextmenuaction` カスタムイベントが飛ぶので、ページ側でそれを購読して処理する。既存の実装例は `web_templates/shared_files.html`。
- CSRF: 非AJAXフォームには必ず `<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">` を入れる。ミスマッチ時は `/` にリダイレクトされる(テストスクリプトでこの手のエラーが出たら、まず cookie jar の使い回しミスを疑うこと。特に `-b`/`-c` を全リクエストに揃えて付けているか確認する)。

## テストの作法

このプロジェクトに自動テストスイートは無い。動作確認は毎回、使い捨てアカウントを作って curl または実ブラウザで一連の操作を再現する方式で行う。

1. `users.connect("/opt/river-fog/data/users.db")` + `users.create_user()` + `werkzeug.security.generate_password_hash` で使い捨てアカウントを作る(既存ユーザーには絶対に触らない。パスを間違えないこと、上記の注意参照)。
2. ログインは `email` フィールドと、直前に取得した `csrf_token` を使う。
3. cookie jar は毎回 `rm -f` して新規に張り直し、以降のリクエストは全て `-b "$JAR" -c "$JAR"` を両方付ける(中間の GET リクエストで `-c` を忘れると CSRF token が更新されず、あとのPOSTで false-positive の "CSRF token mismatch" になる)。
4. 検証が終わったら、作成したユーザー・関連行・アップロードしたファイルを確実に削除し、ローカル/リモート双方の一時ファイル(`/tmp/*`, スクラッチパッド)も片付ける。生成データ・カメラID・座標などを絶対に捏造しない。

## その他の運用メモ

- 変更対象のサービスだけを再起動する(`river-fog-web.service` 以外に不用意に触らない)。
- Python: 3.14.4(`.venv` 内)。
- ログ: `/opt/river-fog/logs/`。
- バックアップ: `backup.py`(`.gitignore` で `backups/` は除外)。
- 過去の変更履歴は `CHANGELOG.md` を参照。
