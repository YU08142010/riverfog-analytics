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

Flask アプリ(`webapp.py`)は waitress 経由で動き、systemd サービス `river-fog-web.service` が管理している。nginx が `:8443`(HTTPS)/`:8080` でフロントし、内部的には `:8081` にプロキシしている。公開URL(`https://sshserver.taile43041.ts.net`、Tailscale Funnel)は nginx を通らず `:8081` の waitress に直接つながるため、静的ファイルも含めた全リクエストを waitress が捌く(2026-10-06〜 `threads=12`。既定の4では日本語フォントの分割ファイルの問い合わせで処理待ちが最大94件たまっていた)。静的ファイルのキャッシュは `fonts/`・`vendor/`・`icons/` が30日、それ以外(自前のCSS/JS・manifest)が10分(`_apply_security_headers`)。**CSS/JSを変えても閲覧者に反映されるまで最大10分かかる**。

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
| `collect.py` | カメラ画像からGeminiで霧の現象を分類する収集パイプライン(systemdの`river-fog-collect.timer`で4〜19時の毎時)。低信頼度(-1)判定はより強いモデルへのエスカレーションで再分類する。画像の取得に5地点以上失敗した時は、失敗した地点だけを90秒後・さらに4分後に取り直す(`refetch_failed_images`、2026-10-06〜。配信元への名前解決・接続が数分止まり、その時刻の大半が欠けることがあるため)。それでも欠けた時刻は毎晩の`backfill_images.py`が配信元の5分おきの過去画像から補う。最後に`nowcast.run_nowcast()`で当日の残りの時間を予測し直す(失敗しても収集は成功扱い)。 |
| `fog_features.py` | `SUMMARY_FOG_CODES`(現在は `{1,2,3}` = 川霧のみ)など特徴量定義。この定数は霧確率計算・アラート・モデル評価の全てに効いてくるので変更時は影響範囲を必ず説明してから進めること。 |
| `fog_model.py` | 川霧予測モデル。全地点共通のLightGBM二値分類(川霧あり/なし、2026-10-04〜)。`train_shared_model()`(学習・評価)、`apply_regression_guard()`(前回モデルとの比較・自動ロールバック)、`build_location_forecast()`(予報の二値化)など。予測DB(`predictions.db`)の`predicted_code`は二値(1=川霧あり)で、的中は「川霧かどうか」の一致で判定する(`prediction_log.py`参照)。 |
| `fog_data.py` / `fog_graphs.py` | データ読み込みとグラフ描画のヘルパー。 |
| `chat_log.py` | AIチャット機能のログ。 |
| `common.py` | 共通定数(`BASE_DIR`, `CAMERAS_CSV`, `DATA_XLSX`, `JST` など)。 |
| `predict.py` / `backfill_*.py` / `station_weather.py` | バッチ処理系スクリプト。 |
| `hindcast_lightgbm.py` | 旧モデルが予測していた過去の日時を、現行モデルで「当時の条件」(前日19:30までのデータ・当時の予報)で予測し直して`predictions.db`に追加する(`model_type`に`[再予測]`)。分析ページ等の精度は現行モデルの行だけを集計する(`prediction_log.CURRENT_MODEL_LIKE`)。何度実行しても重複しない。 |
| `relabel_without_weather_hint.py` | 2026-08-30〜10-02の現象ラベル(Geminiに気象のヒントを渡して判定していた期間)を、保存済み画像からヒントなしで判定し直す一回限りのスクリプト。変更前後は`data/experiments/relabel_without_weather_hint.csv`、進捗は同名の`.progress.json`。止まっても再実行で続きから再開し、完了後に予測DBの実測と再予測を更新する。 |

**運用メモ(2026-10-05)**: ラベルの判定し直し(relabel_without_weather_hint.py)はcrontabの@rebootと30分ごとのflock付き起動で、サーバーが再起動しても続きから再開する(夜ごとの判定数は`.nights.json`、完了すると`.done`)。的中率の見出しは翌日の予測(発表から24時間先まで)に限り、再予測の件数も示す(`prediction_log.accuracy_summary`の`next_day`)。レンズが曇りやすい4地点(cameras.csvの「レンズ曇り」)は全体霧と判定されやすいため画面に注意書きを出す(学習には使う。外すとかえって精度が下がる)。学習に使う「地点の実測」の気象はOpen-Meteoのモデル値で、本物の地上観測ではない(予報との風速の相関0.93。`data/experiments/sun_lens_wind_check.json`)。

**気候値との混合(2026-10-05〜)**: 川霧の確率は「(1-w)×LightGBM + w×地点×時刻の過去の発生率」(`BinaryFogModel.fog_probability`。混ぜる前は`raw_probability`)。wは検証期間(予報入力)でloglossが最小になる値で、しきい値は混ぜた後の確率で選ぶ(`global_metrics["climatology_weight"]`、混ぜない場合の成績は`model_unblended`)。学習は実測、予測は予報が入力のため確率が自信過剰になる分を和らげる。直近6週を2週×3でずらした評価でloglossが有意に改善しAPは同等だった(`data/experiments/model_improvement_check2.json`)。同じ評価で効果が無かった・悪化した案(水温の代わりの7日平均気温との差、夜の冷え込み、地点×時刻の発生率の特徴量化、予報値での学習、乱数違いの平均、単調制約、ロジスティック/isotonic補正)の結果は`data/experiments/model_improvement_check.json`と`model_improvement_check2.json`。

**当日の短時間予測(ナウキャスト、2026-10-06〜)**: **2026-10-08から止めている(`nowcast.ENABLED = False`)**。本体のモデルに地形・新しいデータの重み・JMA+ECMWFの平均を入れたことで、当日の残りの時間も本体のモデルの方が良く当たるようになった(直近6週を2週×3でずらした評価の同じ行で、本体AP 0.234、ナウキャスト0.221、地形・重みを入れたナウキャスト0.222。`data/experiments/model_improvement_check5_nowcast.json`)。止めている間は毎晩の学習も毎時の予測し直しもしない。以下は使っていたときの説明。 `nowcast.py`。前日夜の予測とは別に、気象(予報)・地点別川霧発生率に、発表時刻の観測(その地点の今・1〜2時間前の霧、全地点のうち霧の地点の割合)と先行時間を加えた全地点共通のLightGBMを毎晩`predict.py`の最後に学習し(`data/models/nowcast_state.joblib`)、毎時の`collect.py`の最後にその日の残りの日中(最大8時間先)を予測し直す(`data/models/nowcast_latest.json`、答え合わせ用に`predictions.db`の`nowcasts`テーブル。本番の`predictions`・精度の集計には混ぜない)。公開ページは本日分にこれを使う(`_build_public_forecast`)。直近6週を2週×3でずらした評価(予報入力)でAP 0.202→0.260(差の95%区間+0.011〜+0.104)、先行1〜8時間のどれでも改善(`data/experiments/nowcast_and_source_check2.json`)。早期打ち切りで木が少なすぎると改善が消えるため最低300本。基本モデルの確率を観測でロジスティック補正する方式は悪化したため不採用。毎晩の評価期間で基本モデルよりAPが0.02以上悪ければ使わない(`GUARD_AP_MARGIN`)。毎時の予測し直しは直近7時間の現象記録だけを読む(`fog_data.load_recent_phenomena`。全期間を読むと約4秒・メモリ約400MB)。毎晩の学習では、本体のモデルで組み立てた学習データと取得した過去の予報を、同じプロセス内でナウキャストの学習に使い回す(`fog_model._FRAME_CACHE`・`_HIST_CACHE`)。同じ実験で予報の気象モデルも比べた: Open-Meteoの既定(best_match)は日本では`jma_seamless`と完全に同じ値で、GFSは悪く、ECMWF(0.25度)は有意差なしだった。ただしこの時は座標の照合(許容0.1度)で65地点中32地点がはじかれていた。許容差を格子に合わせて比べ直すと、ECMWFとの平均が有意に良かったため、2026-10-07から平均している(下記)。

**地形・新しいデータの重み・気象モデルの平均(2026-10-07〜)**: 次の3つを本番に入れた。
- 地点の地形: `標高`、`谷の深さ_1km`・`谷の深さ_3km`(周囲の東西南北4点の平均標高 − 地点の標高。国土地理院の標高API)、`地域_会津`。値は`data/station_terrain.json`に保存し、新しい地点は学習時に自動で取得する(`fog_model.load_station_terrain`)。モデルが地点ごとの値を持ち(`BinaryFogModel.station_static`)、予測時に地点名から引く。
- 新しいデータの重み: 学習の基準時点から60日前の行の重みを半分にする(`RECENCY_HALF_LIFE_DAYS`)。30日では効かなかった。
- 気象モデルの平均: 予報を入力にした確率を、既定(JMA)とECMWF(`ecmwf_ifs025`、15日先まで)のそれぞれで計算して平均する(`ENSEMBLE_MODELS`)。学習時もECMWFの過去の予報を取って、平均した確率で気候値の割合・しきい値を選ぶ。取得できなかった夜は平均しないモデルになり、本番も平均しない(`BinaryFogModel.ensemble_models`)。ECMWFは0.25度格子のため、座標の照合の許容差は0.2度(`ENSEMBLE_COORD_TOLERANCE_DEG`)。

直近6週を2週×3でずらした評価(Open-Meteo Previous Runs APIの過去の予報を入力)で、3つ合わせてAP 0.222→0.280(差の95%区間+0.027〜+0.091)、loglossも有意に改善し、3期間すべてで改善した(`data/experiments/model_improvement_check3.json`・`model_improvement_check4.json`)。ECMWFとの平均は明日・3日後・7日後のどれでも有意に改善した。2026-10-07の試運転(評価期間9/16〜10/7、川霧291件)でも、変更前と比べてAP 0.180→0.197、F1 0.250→0.270、Brier 0.0184→0.0181。

同じ評価で採らなかった案:
- 前日の朝の川霧実績: APが変わらなかった。
- 日長: 悪化した。
- リードタイム別の気候値の割合: Previous Runs APIで1〜8日先を比べ、差が無かった(遠い日も確率はそれほど自信過剰ではなかった)。
- 日単位(その日に川霧が出るか)のモデル: 1時間ごとのモデルの日最大よりAPが有意に低かった(0.52対0.58)。

**学習の抜き取り(2026-10-08〜)**: 本体のモデルは列0.6・行0.7の抜き取りで学習する(`fog_model.LGBM_SAMPLING`、設定の候補`LGBM_PARAM_GRID`のすべてに付ける)。上の構成で同じ評価をして、loglossが有意に改善(差の95%区間 -0.0022〜-0.0007)、APも3期間すべてで少し上がった。同じ評価で、学習率0.02・小さな木(葉7)・extra_trees・ICON/GFSを平均に足すことは改善しなかった(`data/experiments/model_improvement_check5.json`)。

**方式の比較(2026-10-06〜)**: 毎晩の学習で、RandomForest+XGBoost(確率の平均。2026-10-04まで地点別モデルで使っていた方式の1つ)を今の全地点共通の構成・LightGBMと同じ手順(検証期間で木の本数・気候値の割合・しきい値、評価期間は予報入力)で学習・採点し、F1値の差(LightGBM − RF+XGBoost)と評価期間の日単位ブートストラップの95%区間を`global_metrics["comparison"]`に記録、分析ページ(accuracy)に表示する(`fog_model._compare_rf_xgboost`)。本番はLightGBMのまま。初回(2026-10-06、評価22日・川霧309件)はF1 0.252 対 0.230(差+0.022、95%区間-0.005〜+0.048で有意差なし)、APはRF+XGBoostが0.192対0.186とわずかに上。

**特徴量と重要度**: 露点温度は「気温 = 露点温度 + 気温露点差」で完全に重複するため特徴量から外している(`fog_model.EXCLUDED_FEATURES`。外しても精度は誤差の範囲、`data/experiments/feature_selection_check.json`)。分析ページの特徴量重要度は、学習時に評価期間(予報入力)で求めたSHAP値の絶対値の平均の割合(`global_metrics["importance"]`)で、まとまり(`FEATURE_GROUPS`)ごとの合計も示す。gainは似た特徴量に偏って配られるため使わない(検証の経緯は`data/experiments/feature_importance_check.json`)。

**時間帯の扱い**: 日の出前・日の入り後(`fog_features.is_daylight`)の画像は暗くて川霧を判定できないため、学習・評価・答え合わせ・予測の対象外(`predictions.daylight=0`、予報では`fog_model.NOT_PREDICTED`)。

**見た目**: `web_static/glass.css`(macOS風の磨りガラス。base.html・base_auth.html・public_base.htmlでstyle.cssの後に読み込み、色・角丸・影だけを上書きする。外せば元の見た目に戻る)。地点ページ(研究用・公開とも)は`_weather_scene.html`+`web_static/weather_scene.css`で、最新の観測(現象コード)と地点の日の出・日の入りから決めた時間帯に応じた空と、流れる霧・雲・雨を背景に描く(`webapp._weather_scene`)。動きは`web_static/weather_scene.js`(Canvas。霧のかたまり・雲・雨・星を流し、マウスとスクロールで層ごとにずらして奥行きを出す。JSが無い場合はCSSの霧の帯で代用)。ダッシュボードと公開ページのトップも、全地点の最新の観測から決めた代表の状況(`_overview_weather_scene`)で同じ背景を出す。ボタン等の押したときの反応(押すと縮んでばねのように戻る・押した位置から広がる波紋・触れると光が横切る・ページ移動のなめらかな切り替え)は`glass.css`の「動き」の節と`web_static/ui_motion.js`。Spotlight風の検索(Ctrl+K/⌘K・ヘッダーの検索ボタン・「/」。`web_static/spotlight.js`、候補は`webapp._spotlight_items`。地点・公開ページ・ページ・管理画面・操作(テーマ・全画面・URLのコピー等)・すぐ開く(最新の画像等)・日付(今日/昨日/3日前/10月6日)・月のカレンダー。「>」で操作、「@」で地点、「?」でショートカット一覧、Ctrl+Enterで新しいタブ)、日付のカレンダー(`web_static/calendar.js`。日付一覧ページの月表示、日付ページの前後の日とカレンダーの小窓、Ctrl+Kのカレンダー。データのある日付は`/api/calendar-dates`、観測の日付はDBから直接引いてキャッシュ)、ロック画面風のログイン(base_auth.htmlに全体の背景の空、login.htmlに大きな時計)、天気アプリの都市リスト風の地点一覧(`_station_card_data`。地点ごとの空の色・現在の状況・気温・今日の川霧の出やすさ)。ほかの演出: 動く天気アイコン(`_wx_icon.html`)、地点ページの時間ごとの出やすさの帯(`_station_hourly`)、スクロールで浮かび上がるカードと数字のカウントアップ(`ui_motion.js`)、右上から降りる通知、グラフの読み込み中の光る枠と見た目の統一(base.htmlの`polishChart`)、地図の光る印とダークモードの暗い地図、ホーム画面に追加(`web_static/manifest.json`・`icons/`。サービスワーカーは使わない。ホーム画面から開くと全画面(`display: fullscreen`、非対応のiOS等は`display_override`でstandalone)。ヘッダーの全画面ボタン(研究用・公開とも`web_static/fs_shell.js`)は、全画面の間に同じサイト内でページを移ると移動先を全画面の中の枠(iframe)に読み込むため解除されない(リンク・フォーム、Chrome/EdgeではNavigation APIでスクリプトでの移動も)。全画面を終えると枠で見ていたページを普通に開き直す。ブラウザのタブの切り替え等で外れたときは「全画面に戻す」ボタンを出す。グラフは画像で表示する(2026-10-07〜、`mpl_render.py`): 各グラフのURLはPlotlyのfigureのJSONを返すままで、`?format=png&theme=&w=&scale=&notitle=`を付けると`webapp._chart_as_png`がmatplotlibで描いた画像を返す(全グラフ共通・同じ条件はメモリに保持)。予報グラフ・全地点サマリーは`/predictions/<date>/figure/<file>`が保存済みのHTMLから図を取り出す。ページ側はbase.htmlの`mountPlotlyChart`が画像を置き、テーマの切替・幅の変化で描き直す(Plotly.jsは読み込まない)。2026-09-14からのPlotlyは触ると拡大・移動してしまい使いづらかったため。グラフの見た目は`chart_style.py`で統一している(色は色覚の多様性に配慮して検証した8色を決まった順に、量の濃淡は青1色の明→暗。線2px・点8px+縁取り・細い実線のグリッド・左寄せのタイトル・上の凡例)。webapp/experiment_viewのグラフは`_chart_json`/`_to_json`で、予測グラフ(fog_graphs.py)は書き出し前に通す。ダークモードの色はbase.htmlの`polishChart`(予測グラフはchart_style.DARK_POST_SCRIPT)。書体は英数字がInter・日本語がZen Kaku Gothic New(どちらもSIL OFL)で、CSPによりフォントは同一オリジンからしか読めないため`web_static/fonts/`に置いて自前配信している(`fonts.css`。Google Fontsの文字範囲ごとの分割ファイルのため、ブラウザはページで使う文字を含む分だけ読み込む)。

**公開ページ**: `/forecast`(と`/forecast/about`「研究の概要」)はログイン不要の一般向けページで、未ログインで`/`に来た人はここに転送される(研究用サイトには「関係者ログイン」から入る)。本番の最新予測を地点・日中の時刻ごとに3段階(`PUBLIC_LEVELS`。しきい値以上=発生可能性 高、しきい値の半分以上=中、それ以外=低)で今日〜明後日まで表示する(`webapp._build_public_forecast`)。地点ページ`/forecast/station/<地点名>`では、その地点の3日分と、過去の月別・時刻別の川霧の発生率(日中・訂正込み、`_build_public_station_stats`。集計に約20秒かかるため裏で作り直し6時間キャッシュ)を示す。カメラ画像・確率の値・利用者情報は出さず、noindex。気象業務法の予報業務許可との関係があるため「予報」ではなく研究用の試験情報として表示している。文章は研究の文体(である調、「発生可能性」「現況」「適合率・再現率」等の用語)で統一する。
トップ(`public_forecast.html`)の構成: 選んだ日の概要の1文と「中」以上の地点の上位6件(時刻の帯つき)/地図(点の色と霧のもやで段階を示し、5〜18時のボタンと「時系列で再生」で時刻ごとに切り替える。左下に表示中の時刻)/時刻別の表(既定は「中」以上と現況で川霧ありの地点のみ。選択は閲覧者のブラウザに保存。行・ランキングに重ねると地図の点に輪、地図の点の「表の該当行へ」で行へ移動)/予測と観測の照合(`_build_public_verification`。直近7日について、その日0時より前の最新の本番予測の「高」(=`predicted_code` 1)と、`predictions.actual_code`のAI判定を地点×時刻で照合し、的中・空振り・見逃し・適合率・再現率を出す。照合済みの行がある日だけ出る)。見た目(2026-10-06): 見出しの帯にロゴと明暗切り替えの丸ボタン(アイコンは`currentColor`。glass.cssでライト時は帯が明るくなるため)、更新時刻のピル、日付タブに「高」「中」の地点数、ランキングの段階バッジ、本日表示時は今の時刻の列・帯に印。地点ページは見出しの下に日ごとの見込みのチップ(`_public_station_outlook`)、位置の小さな地図、今月・今の時刻の棒の強調。研究の概要は手順の番号とF1値の比較の棒。公開ページの地図は国土地理院の淡色地図(CSPの`img-src`に`cyberjapandata.gsi.go.jp`を追加。研究用サイトの地図はOSMのまま)。トップの見出しの後ろに流れる霧の層(濃さは今カメラで川霧の地点数、`--mist`)、概要の数字の下に全地点を段階順に並べた点の列、日付・絞り込みを切り替えた時のふわっとした入れ替え(`swapAnim`、動きを減らす設定では止める)。「現況」は最新時刻の観測だが、画像を取れずその時刻が空欄の地点は直近3時間以内の観測で補い時刻を添える(`_public_now_by_station`、公開ページのみ。ほかの画面の`_latest_observations`はそのまま)。
| `web_templates/` | Jinja テンプレート(57ファイル)。`base.html` が共通レイアウト・ナビ・サイト独自モーダル・右クリックメニュー基盤を持つ。 |
| `web_static/` | `style.css`, `shared.js`, `favicon.svg`。 |
| `cameras.csv` | 地点名とカメラIDの対応表(緯度経度は保持していない)。 |

## フロントエンド上のルール(厳守)

- **JavaScript のネイティブ `alert()` / `confirm()` / `prompt()` は使用禁止。** 代わりに `base.html` にあるサイト独自のモーダル(`confirmModal(message)` → `Promise<boolean>`、`promptModal(message, defaultValue)` → `Promise<string|null>`)を使うこと。
- フォームを `data-ajax` 属性付きで作ると `fetch` 経由の非同期送信になり、レスポンスJSONの `message`/`success` が `showToast()` で表示される。成功時は `ajaxformsuccess` カスタムイベント(`document` に発火、`detail: {form, data}`)を購読して画面を更新できる。
- 右クリックメニューは `data-context-menu="<menu-id>"` を要素に付けるだけで汎用的に使える(`base.html` の仕組み)。メニュー本体は `hidden` な `.context-menu` の `<div>` として用意し、中の `<button data-context-action="...">` を押すと `contextmenuaction` カスタムイベントが飛ぶので、ページ側でそれを購読して処理する。既存の実装例は `web_templates/shared_files.html`。
- CSRF: 非AJAXフォームには必ず `<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">` を入れる。ミスマッチ時は `/` にリダイレクトされる(テストスクリプトでこの手のエラーが出たら、まず cookie jar の使い回しミスを疑うこと。特に `-b`/`-c` を全リクエストに揃えて付けているか確認する)。

- **メニュー(2026-10-12に作り直し)**: 項目は `base.html` の `app_nav`(グループの一覧)で1か所だけ定義し、`_app_nav.html` の `nav_groups` で
  PC(幅1100px以上)の左サイドバー `.app-side`(細くできる。状態は端末の `localStorage["rf-side-collapsed"]`)と、狭い画面の「メニュー」シート
  `.app-sheet` の両方に出す。狭い画面は下にタブバー `.app-tabbar`(ホーム・地点・見頃・写真・メニュー)。**新しいページをメニューに足すときは
  `app_nav` に1行足す**(`active` の条件も書く。未読などの数は `badge`、画面を開いたまま更新する数は `badge_key`)。狭い画面のヘッダーでは
  版数・全画面・文字の大きさ・設定・名前・ログアウトを隠す(設定とログアウトはシートにある)。
- **ホーム(`/`)**: 地図が中心(`webapp._home_map_data`)。「いま/1時間後/今日/明日」を切り替え、横(狭い画面では上下)に いまの霧・
  1時間後の見込み・24時間の予測・今朝の写真を出す。幅500px未満の画面の確認は、生のEdgeが500px未満にできないため、作業用の
  `shotsp.ps1`(幅390pxのiframeに入れて撮る)を使った。

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

## 毎日の再起動とチャット通知(2026-10-08〜)

- **毎日03:40にサーバーを再起動する**(所有者の依頼)。`rf-daily-reboot.timer` → `rf-daily-reboot.service` →
  `/usr/local/sbin/rf-daily-reboot.sh`(このリポジトリの外、root管理)。03:00のバックアップや予測がまだ動いていれば
  最大15分待ってから再起動し、04:00の観測収集の前に終わるようにしている。止めるときは
  `sudo systemctl disable --now rf-daily-reboot.timer`。待ちの判定だけ試すときは `sudo RF_DRY_RUN=1 /usr/local/sbin/rf-daily-reboot.sh`。
- **チャットの通知(音なし)**: ベルのパネルの切り替えでオンにする。サイトのタブが開いている間(裏でも)、
  共通の15秒ごとの未読確認(`api_nav_status`)でDM・グループの未読が増えたら、ブラウザの通知を`silent: true`で出す。
  オン/オフと前回の未読数はブラウザのlocalStorage。`web_static/sw.js`は通知を出すためだけの最小のService Worker
  (キャッシュしない、オンの時だけ登録)。ブラウザを閉じていると届かない(Web Pushではない)。

## 個人設定と通知(Web Push)(2026-10-09〜)

- **設定画面 `/settings`**(ヘッダーの歯車)。項目の定義は `user_settings.py` の `SETTINGS`(既定値と取りうる値)で、
  users.db の `user_settings` に利用者ごとにJSON1行で保存する。項目を足すときは `SETTINGS` と `SECTIONS` に足し、
  settings.html にフォームの行を足すだけ。不正値・定義から消えた項目は読み込み時に捨てる。
  - 表示(テーマ・文字の大きさ・動きを減らす・背景を動かさない)は base.html の `<head>` の `window.RF_PREFS` で描画前に当てる。
    テーマと文字の大きさは既定がNone(未設定)で、その間は従来どおり端末のlocalStorageに従う。ヘッダーのボタンで変えると
    `/settings/quick` でサーバーにも保存する。
  - 「最初に開くページ」はログイン直後と、参照元が外(ホーム画面のアイコン等)の `/` で効く。
  - 「オンライン状態を表示しない」は最終アクセス時刻を記録しない(`_require_login`)。
- **Web Push**: `push_notify.py`(pywebpush)。VAPIDの秘密鍵は `data/push_vapid_private.pem`(git管理外、無ければ自動生成。
  作り直すと全員の購読が無効になる)。購読は `push_subscriptions`、送信済みの記録は `push_sent`(14日で消す)。
  - DM・グループは送信時に `notify_async`。川霧の朝夕のまとめ・まもなく出やすい・観測した・カメラ・管理者向けは
    webapp の裏のスレッド `push-scheduler` が5分ごとに `run_scheduled` で判定する(購読が0件なら何もしない)。
  - 購読先は正規のプッシュサービス(FCM・Mozilla・Apple・WNS)のURLだけ受け付ける(`PUSH_ENDPOINT_HOST_RE`、SSRF対策)。
  - Service Worker は証明書が正しいHTTPSでしか動かないため、通知は公開URL(Funnel)で開いたときだけ使える
    (:8443の自己署名では不可)。iPhone・iPadはホーム画面に追加したアプリからのみ(iOS 16.4以降)。
  - テストは本物のプッシュサービスに送らず、`push_subscriptions` に `http://127.0.0.1:<port>/` の偽の受信口を直接入れ、
    受け取った本文を `http_ece.decrypt` で復号して確かめる(ルート経由では127.0.0.1は拒否される)。

## ラベルの質の改善(2026-10-09〜)

- **正解セット `/gold`**(観測者・管理者。観測報告のタブ)。`gold_set.py`、テーブルは`gold_samples`・`gold_labels`(users.py)。
  毎日、前日の日中の画像から「AIが霧(1〜6)」の層を10枚・それ以外を20枚無作為に選び(ページを開いたときに作る、過去7日分まで。
  `START_DATE`より前は作らない)、AIの判定を見せずに判定してもらう。1枚2人まで。全体の割合は層の重み(`weight`)で補正する。
  人の判定がAIとまとまり(現象なし/川霧/全体霧/層雲/雨)で違い、ほかの人の判定と食い違わなければ`phenomena_corrections`にも
  訂正として記録する。結果(`/gold/results`)はAIのラベルの適合率・再現率、2人の一致率、前日夜の予測を人の判定で答え合わせした精度。
- **AIの二重判定** `label_verify.py`: 1回目が川霧(1〜3)の画像を、同じカメラの「見通しの良い時」(現象なし・確信度high・9〜14時、
  前30日で一番近い時刻)の画像と並べてもう一度判定する(verdict: river/wide/none/unsure)。毎時35分に`label_verify_run.py`
  (crontab)が新しいもの→2026-09以降の過去分の順に40件まで判定し、`data/models/label_verify.csv`に追記する。APIキーは毎時の収集と
  別(GEMINI_API_KEYSの最後)。人の確認との照合は`label_verify_check.py`(`data/experiments/label_verify_check.json`)。
  学習から外すかは`fog_model.EXCLUDE_DISPUTED_LABELS`(verdict=noneかつ確信度high/medium)。
- **調べたが使えなかったもの**: ダムの放流量・河川水温。東北電力の阿賀野川水系ダム情報は画像での公開のみで、第三者への提供禁止。
  国交省の水文水質データベースは流量・水位のみで、只見川の発電ダムは対象外。使うなら研究用のデータ提供の依頼が要る。
- **二重判定の検証結果(2026-10-09、人が確認した日中の160件)**: 基準画像ありは「判断できない」9%(なし29%)。1回目が川霧の58件で、
  「霧なし・確信度high」の32件のうち29件は人も川霧ではないとしたが、本物の川霧8件のうち3件も落とした。評価の画像は「怪しい」と
  拾われたものに偏っているため、学習から外す(EXCLUDE_DISPUTED_LABELS)のは、正解セットが100枚ほど集まってから偏りのない画像で確かめて決める。
- **会津**: 9月以降の川霧ラベルは88件(0.5%)、全体霧は777件。盆地の霧の多くが全体霧に分類され、予測の対象(川霧のみ)から外れている。
  午後の川霧24件のうち10件は大石で、湿度は高く(中央値95%)、明らかな誤判定とは言えない。
- **分析ページのグラフ(mpl_render.py)**: 狭い幅で小さな図が3列以上並ぶときは列を減らして並べ直し(_regrid_for_narrow)、
  描いた後に文字の重なり・はみ出しを直す(_fit_texts。横軸の項目名は括弧の前・1枠の幅で改行、題名等は少し縮める、
  下・左にはみ出す分は余白を広げて1回だけ描き直す)。目盛りの文字は描くたびにフォーマッタから作り直されるので、
  Textを直接書き換えずFixedFormatterを差し替えること。ブラウザはいつも2倍の解像度で要求する(base.htmlのchartImageSrc)。

## 運用の見守り・画像の活用・月報(2026-10-11〜)

crontab(sshuser)に次を追加した(変更前は `backups/ui-20261009/crontab_before_20261011.txt`)。
- **夜間の巡回テスト** `smoke_test.py`(毎日02:20): 使い捨ての管理者 zz-smoke@example.invalid を作り(最後に必ず消す)、
  主要ページ・全地点のページ・公開ページ・分析ページのグラフ画像を開いて、ステータスと秒数を `data/health/smoke.json` に書く。
- **朝の健康チェック** `health_report.py`(毎日06:40): 昨晩の予測・毎時の収集の回数・Geminiのクォータ超過/全キー失敗・
  ディスク・バックアップ・巡回テスト・サービスを確かめ、`data/health/latest.json` に書く。問題があれば、通知を受け取っている
  管理者に送る(`ALERT_WEBHOOK_URL`があればそこにも)。画面は `/admin/health`。`--dry-run`で送らずに確認できる。
- **かすみ度** `haze_index.py`・`haze_run.py`(毎時40分に新しい画像、毎晩23:10に過去分。`/tmp/rf_haze.lock`): 同じカメラの
  「見通しの良い時」の画像(label_verifyと同じ選び方)と比べた、輝度の散らばりの比・輪郭(ラプラシアン)の比・ダークチャネルの差・
  明るさの差を `data/haze.db` に保存。かすみ度(0〜100)は `haze_index.score()`(輪郭の比だけ。2026-10-11の照合で霧のラベルとのAUCは霧全体0.77・川霧0.69。
  ラベルの良し悪しを単独で決めるほどの強さは無い)。地点ページに直近3時間の値を出し、ギャラリーの
  並び順に使う。霧のラベルとの照合は `haze_run.py --check`(`data/experiments/haze_check.json`)。
- **今朝の川霧** `gallery.py`・`/gallery`(地点のタブ、ダッシュボードのカード): その日5〜11時に川霧(人の訂正を反映、二重判定で
  霧なし・highは除く)が写っていた画像を地点ごとに1枚、かすみ度の高い順に。
- **タイムラプス動画** `timelapse.py`・`/stations/<地点>/timelapse/<日付>.mp4`: その日の毎時の画像をクロスフェードでつないだMP4
  (12fps・幅960、ffmpegはpipのimageio-ffmpeg同梱)。`data/timelapse/<日付>/<地点>_<枚数>.mp4` に保存して使い回し、画像が増えたら
  作り直す。地点ページのタイムラプスとギャラリーから開ける。
- **月ごとのレポート** `monthly_report.py`(毎月1日07:10、前月分)・`/reports`: 川霧を観測した日数(地点別・地域別)・日ごとの
  観測地点数・時刻別の割合・前日夜の本番の予測の成績(地域別。後からやり直した予測は数えない)・その月のいちばん霧が出ていた朝を
  `data/reports/<YYYY-MM>.pdf`(と要約の.json)にし、設定画面の「月ごとのレポート」がオンの人に通知する。
- **判定例(few-shot)** `collect.fewshot_examples`: 正解セットで人の判定がそろった画像を手本としてGeminiの指示の先頭に添える。
  `.env`の`GEMINI_FEWSHOT=1`で有効(既定はオフ)。効果は `fewshot_check.py`(正解セットの画像を手本あり/なしで判定し直す)。
- **境界層・逆転の強さの再検証** `bl_recheck.py`: 10/22 23:30に1回だけ(印 `data/experiments/bl_recheck.done`)。過去の予報を入力に
  直近6週を2週×3で比べ、`data/experiments/bl_recheck.json` に書いて管理者に通知する。`--force`は試し(印を付けず、
  `bl_recheck_trial.json` に書く)。
- **地域ごとの予測の対象** `fog_features.REGION_TARGET_CODES`・`REGION_TARGET_ENABLED`(既定はFalse): Trueにすると会津は全体霧
  (4〜6)も対象になり、学習の目的変数・答え合わせ(prediction_logの的中SQL・分析ページ)・公開ページの「いま霧」・通知が切り替わる。
  比較は `aizu_target_check.py`(`data/experiments/aizu_target_check*.json`、毎月2日01:00に案Cを比べ直して管理者に通知)。
  有効にすると、predict.pyは全地点共通のモデルを従来どおり川霧で学習して只見川に使い、会津は会津の地点だけで霧全体を学習した
  別のモデルで予測する。2026-10-11の評価期間(直近3週、予報入力): 共通モデルのまま会津だけ対象を広げると只見川が悪化(AP 0.200→0.187)、
  地域別の別モデルなら会津AP 0.017→0.204だが、会津は「地点×時刻の過去の発生率」(AP 0.230)にまだ及ばない(会津のデータは
  2026-09から)。そのため無効のまま。apply_regression_guardは先頭の地点のモデルしか見ないので、有効にするなら会津のモデルにも
  同じ確認を足すこと。

## 流れ図・実測・別の予測と判定・写真投稿・書き出し(2026-10-12〜、v6.3x)

本番の予報(predict.py)とGeminiの判定はそのままにして、**別の項目として**足したもの。
- **川霧の流れ図・見頃カレンダー** `/flow`(地点のタブ): 地点を川沿い(只見川は伊南川と支流→本流の上流→下流 `FLOW_MAIN_STEM`、
  会津は南→北)に並べ、時刻ごとの霧を色で塗った表と、時間のスライダーで動く地図。観測(過去の日の判定)と見頃(今日〜明後日の予報)。
- **研究用の書き出し** `export_data.py`・`/export`(レポートのページから): 期間(62日まで)と地点を選んで、観測(人の訂正を反映、
  ai/humanの別・日中かどうか)・地点の気象・予測の記録・かすみ度・画像(3000枚まで)をZIPに。中身の説明はZIPのREADME.txt。
- **アメダス(実測)** `amedas.py`(毎時15分)・`data/amedas.db`: 近くの10地点(`STATIONS`)の気温・露点・湿度・風・降水・日照。
  最新は気象庁のJSON(約9日分しか残らない)、過去は「過去の気象データ検索」のページから(`--backfill-from YYYY-MM-DD`、
  済んだ日は飛ばすので、失敗した日は同じコマンドで埋め直せる)。地点ページに「最寄りのアメダス(実測)」カード。
- **この後3時間** `short_nowcast.py`(`--train` 毎晩20:10、`--predict` 毎時50分): いまの画像の判定・かすみ度と近くのアメダスの
  実測から、1〜3時間後に川霧が写っているかを出す別のモデル(`data/models/short_nowcast*.{joblib,json}`)。
  比べ方は `data/experiments/short_nowcast_check.json`(直近14日を評価、そのままの持続・前日夜の本番の予報と比べる)。
- **もう一つの判定** `own_judge.py`(`--run` 毎時45分、`--train` 毎晩20:20、`data/own_judge.db`): 画像を3×3に分けた明るさ・
  輪郭・色の、晴れた日の同じカメラとの比を特徴量にした小さなモデルの判定(人の訂正を重み5で学習)。成績は
  `data/experiments/own_judge_check.json`。地点ページの「この後3時間・もう一つの判定(試験中)」カード。
- **毎時の順番**(2026-10-12に組み直し): 収集(:00〜:05ごろ)→ かすみ度 `haze_run.py`(:08)→ もう一つの判定 `own_judge.py --run`(:12)→
  アメダス(:15)→ この後3時間 `short_nowcast.py --predict`(:18)→ 二重判定 `label_verify_run.py`(:35)。
- **「1時間以内に川霧が出ていそう」通知**(設定 `push_fog_nowcast`、既定はオフ、種類 `fog_nowcast`): この後3時間の1時間後の確率が
  `push_threshold` 以上の地点(地点ごとに1日1回、すでに川霧を観測中の地点は除く)。`push_threshold` は学習のたびに検証期間で
  「当たる割合が0.35以上になる最も低いしきい値」を選び(当たりが5件未満ならNone=通知しない)、評価期間での成績を
  `short_nowcast_check.json` の `push_test` に書く(2026-10-12: しきい値0.31、当たる割合0.38・霧の見逃しを含めた捕捉0.12)。
- **判定の食い違いの確認待ち** `own_judge.flag_disagreements`(`--run` の最後): 自前の判定がGeminiと自信を持って食い違った画像
  (川霧の見落とし ≥0.6・川霧の誤判定 ≥0.7・全体霧の見落とし ≥0.8、人の訂正済みは除く)を観測報告の確認待ち
  (source=auto_detected、重要度「低」、理由つき)に1日6件まで。`label_verify_run.py` も食い違いのある画像を先に回す。
  自前の判定はGeminiのラベルで学習しているので、学習に使った画像ではほとんど食い違わない(新しい画像で効く)。
- **カメラの異常の自動検知** `camera_check.py`(毎日15:30)・`data/camera_check.db`・`data/health/camera_check.json`: AIの判定が
  「現象なし」の昼(10〜14時)の画像の輪郭の配置を、5〜1週間前の同じ条件の画像のうち最も似たものと比べ(日なた・日かげの
  違いで誤報しないよう、平均ではなく最も似た画像)、2日続けてふだんの範囲を下回ったら「向きのずれ・写り込み」、輪郭の強さが
  半分未満なら「汚れ・結露・ピンぼけ」、同じ画像が3回以上続いたら「更新の停止」。管理者に通知し、`/admin/health` に出す。
  画像をずらす・傾ける・ぼかした試験ではすべて検知。身神川排水機場は2つの向きを切り替えるカメラで、両方が過去にあるので異常にしない。
- **アメダスを本番の予報に足す比較** `amedas_check.py`(毎月3日01:00、本番は変えない): 前日18時の実測と前日の降水量・日照などを足した
  場合(B)といまの特徴量(A)を、bl_recheck.pyと同じ方法(過去に実際に出されていた予報を入力、10日×3期間)で比べ、
  `data/experiments/amedas_check.json` に書いて管理者に通知する。
  2026-10-12の1回目: AP 0.234→0.242(差の95%区間 -0.008〜+0.019)、loglossはわずかに悪化、最後の10日はAP 0.167→0.132で
  見送り。アメダスのデータが2か月分しか無いので、毎月比べ直す。
- **このPCへのバックアップ**: Windows側の `%USERPROFILE%\RiverFogBackup\pull.ps1`(タスク スケジューラ「RiverFogBackupPull」、
  8:00・13:00・21:00、ログオン中のみ)が、毎日のZIP(`backups/`、新しい60個を残す)と `data/images/` の日付フォルダ(無い日と直近3日)を
  取る。鍵 `rf_backup_ed25519` は `~/.ssh/authorized_keys` で `restrict,command="/usr/lib/openssh/sftp-server -R"`(読み取り専用の
  SFTPだけ)に制限してある。結果は `RiverFogBackup\status.json`・`logs\`。
  2026-10-12に鍵を登録(書き込み・コマンドは拒否されることを確認)。サーバーからの送り出しは約0.2MB/s(Tailscaleは直結)で、初回(約3.3GB)は数時間、
  以後は1日約100MBで10分ほど。画像の日付フォルダは .part に取ってから名前を変える(途中で止まった日を取得済みと見なさない)。タスクの制限時間は12時間。

## 確率の正直さ・季節の見張り・オフライン・保存の計画(2026-10-12〜)

- **確率の正直さ** `prediction_log.reliability_bins`・`/accuracy/reliability_chart.json`(分析ページ): 前日夜の本番の予測の確率を区切りごとに、
  実際に霧だった割合と比べる。2026-10-12時点: 40%未満はほぼ正直(例: 平均24% → 実際31%)、40%以上は42件で実際は5%ほど(高い確率が言いすぎ)。
- **季節の見張り** `drift_watch.py`(毎週月曜07:30)・`data/health/drift.json`: 前日夜の予測を週ごとに採点し、直近2週の川霧F1が
  それより前(最大6週、霧が少なければ学習時の評価期間)の0.6倍未満、または予測の確率の平均と実際の割合が2倍以上ずれたら管理者に通知。
  直近2週に霧が15件以上あるときだけ判定する。
- **ホームのキャッシュ** `_home_map_data_cached`: 地図のデータは全員同じなので1分使い回す。
- **電波の弱い場所** `web_static/offline_sw.js` を `/offline-sw.js`(範囲 "/"、ログイン中だけ登録)として配信: ホーム・地点・見頃・今朝の川霧・
  写真・地図のページはネット優先で取りつつ端末に保存し、つながらない(6秒)ときは保存したものを出す。CSS・JSは保存してすぐ出す。
  カメラ画像は最後に見た120枚まで。ログイン画面を開くと保存したページ・画像を消す。通知用の `/web_static/sw.js` とは別の登録で、通知には関係しない。
  このサイトのCSPではService Workerから他のサイトを取りに行けないため、Leafletは `web_static/vendor/leaflet/`(1.9.4)に置いて、地図のページはそれを読む。
  圏外での写真の投稿は `web_static/offline_queue.js` が端末(IndexedDB)に保存し、つながったときにどのページからでも送る(撮影時刻が空なら写真のファイルの時刻を入れる)。
- **地点ページのタブ**: 各カードの `data-st-tab`(now / forecast / photos / records)で振り分け、上に固定のタブと地点の状態。`#forecast` などで開ける。
  JavaScriptが無いときは全部出る。
- **使いやすさ**: 本文へのスキップリンク、メニューのシートの中でTabが外に出ない、細いメニューでも項目名を読み上げる、タブバーの aria-current。
- **保存の計画** `storage_plan.py`(毎日06:30)・`data/health/storage.json`: 画像の増え方(2026-10-12: 1日約62MB)からディスクが90%に届く日数
  (約560日)を出し、120日を切ったら通知。PCのバックアップの報告(`/api/backup-manifest`、`.env` の `RF_BACKUP_TOKEN`、Funnelからは404)が
  3日来なければ通知。180日より古い画像は、PCの報告で枚数・大きさが同じ日だけ間引ける(`.env` に `RF_IMAGE_THIN=1` を入れたときだけ。既定は数えるだけ)。
  PC側の `RiverFogBackup\pull.ps1` は取り終えたら `token.txt` で報告する。
- 地点ページの大きな気温の数字は、表示のときに0から数え上がる(ui_motion.jsのcountUp)。画面の確認の画像で4°や3°に見えたのはその途中。

- **利用規約(2026-10-13)**: 本文は `web_templates/_terms_body.html`(下書き、11条)、版は `webapp.TERMS_VERSION`。
  研究用サイト: ログインしている人は、いまの版に同意するまで `/terms`(とログアウト)以外は使えない(ページは `/terms?next=` へ移り、
  フォームやfetchは403のJSON)。同意は users.db の `terms_agreements`(利用者・版・日時)と操作ログに残し、セッションに覚える。
  公開ページ: 最初に開いたときに規約の全文とチェックボックスの画面を出し(`public_base.html`、端末の `localStorage` の `rf-pub-terms-<版>`)、
  `/forecast/terms` でいつでも読める。**版を上げると、両方とも全員にもう一度同意を求める。** 使い捨てのテスト用アカウント(smoke_test.py など)は
  作ったあとに `users.record_terms_agreement` で同意済みにすること(しないと全ページが規約の画面に移る)。
- **市民観測(写真投稿)は2026-10-13に削除**(利用者の判断)。`citizen_photos.py`・`/photos`・`photos*.html`・`offline_queue.js` を消した。
  users.db の `citizen_photos` テーブルは空のまま残っている(使っていない)。地点ページのタブの「写真」は「動画」(タイムラプス)にした。

- **背景の絵(2026-10-13)** web_templates/_landscape.html: 夜明けの谷の5層の稜線(遠いほど淡い)と、そのあいだをゆっくり漂う川霧の帯、紙のような粒。
  色は glass.css の --ls-*(明るい表示・暗い表示)。空の天気の演出(_weather_scene.html)のページには、手前の2層の稜線と霧だけを影として重ねる。
  以前の .bg-art(グラデーションの光と線)は使っていない(CSSに display:none で残っている)。

## 試して採らなかった改良(2026-10-08、その6)

直近6週を2週×3でずらし、過去の予報(lead 0、JMA+ECMWFの平均)を入力に比べた
(`data/experiments/model_improvement_check6.json`・`..._check6b.json`)。どれも本番には入れていない。
- **地域(只見川/会津)別のしきい値**: F1は変わらない(差の95%区間 -0.0007〜0)。会津は確率の並び自体が
  ほぼ当たっていない(会津のAP 0.036、川霧は6週で59件・0.5%)ので、しきい値では救えない。会津のデータは
  2026-09からしかなく、川霧59件のうち18件が14〜18時で、もやの誤判定が混じっている可能性がある。
- **水温の代わり(過去3日・7日の平均気温 − いまの気温)**: AP・loglossは変わらず、F1だけ少し上がった
  (しきい値のぶれの影響を受ける指標なので採らない)。
- **夜の冷え込み(12時間前からの気温変化・12時間の最低気温との差・12時間平均の風速と湿度)**: AP・loglossとも
  有意に悪化。学習側の実測の時系列は夜間の欠けが多く(12時間前の気温がある行は58%)、予報の時系列と食い違う。
- **予報の追加変数(下層雲量・雲量・境界層の高さ・気圧の3時間変化・850hPa気温 − 地上気温)**: 全部入れると変わらず。
  **境界層の高さと逆転の強さだけ**ならAP +0.009・loglossも有意に改善したが、3期間のうち最後の期間はAPが
  下がり(0.188→0.159)、全部入れた結果を見てから選んだ組み合わせなので、データが増えてから(2〜3週間後)
  新しい期間で確かめ直す。入れる場合は、学習期間全体の過去の予報(この2変数)を取得・保存する仕組みが要る。
