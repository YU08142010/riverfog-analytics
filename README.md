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

**当日の短時間予測(ナウキャスト、2026-10-06〜)**: `nowcast.py`。前日夜の予測とは別に、気象(予報)・地点別川霧発生率に、発表時刻の観測(その地点の今・1〜2時間前の霧、全地点のうち霧の地点の割合)と先行時間を加えた全地点共通のLightGBMを毎晩`predict.py`の最後に学習し(`data/models/nowcast_state.joblib`)、毎時の`collect.py`の最後にその日の残りの日中(最大8時間先)を予測し直す(`data/models/nowcast_latest.json`、答え合わせ用に`predictions.db`の`nowcasts`テーブル。本番の`predictions`・精度の集計には混ぜない)。公開ページは本日分にこれを使う(`_build_public_forecast`)。直近6週を2週×3でずらした評価(予報入力)でAP 0.202→0.260(差の95%区間+0.011〜+0.104)、先行1〜8時間のどれでも改善(`data/experiments/nowcast_and_source_check2.json`)。早期打ち切りで木が少なすぎると改善が消えるため最低300本。基本モデルの確率を観測でロジスティック補正する方式は悪化したため不採用。毎晩の評価期間で基本モデルよりAPが0.02以上悪ければ使わない(`GUARD_AP_MARGIN`)。同じ実験で予報の気象モデルも比べた: Open-Meteoの既定(best_match)は日本では`jma_seamless`と完全に同じ値で、GFSは悪く、ECMWF(0.25度)は有意差なしだった。ただしこの時は座標の照合(許容0.1度)で65地点中32地点がはじかれていた。許容差を格子に合わせて比べ直すと、ECMWFとの平均が有意に良かったため、2026-10-07から平均している(下記)。

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

**方式の比較(2026-10-06〜)**: 毎晩の学習で、RandomForest+XGBoost(確率の平均。2026-10-04まで地点別モデルで使っていた方式の1つ)を今の全地点共通の構成・LightGBMと同じ手順(検証期間で木の本数・気候値の割合・しきい値、評価期間は予報入力)で学習・採点し、F1値の差(LightGBM − RF+XGBoost)と評価期間の日単位ブートストラップの95%区間を`global_metrics["comparison"]`に記録、分析ページ(accuracy)に表示する(`fog_model._compare_rf_xgboost`)。本番はLightGBMのまま。初回(2026-10-06、評価22日・川霧309件)はF1 0.252 対 0.230(差+0.022、95%区間-0.005〜+0.048で有意差なし)、APはRF+XGBoostが0.192対0.186とわずかに上。

**特徴量と重要度**: 露点温度は「気温 = 露点温度 + 気温露点差」で完全に重複するため特徴量から外している(`fog_model.EXCLUDED_FEATURES`。外しても精度は誤差の範囲、`data/experiments/feature_selection_check.json`)。分析ページの特徴量重要度は、学習時に評価期間(予報入力)で求めたSHAP値の絶対値の平均の割合(`global_metrics["importance"]`)で、まとまり(`FEATURE_GROUPS`)ごとの合計も示す。gainは似た特徴量に偏って配られるため使わない(検証の経緯は`data/experiments/feature_importance_check.json`)。

**時間帯の扱い**: 日の出前・日の入り後(`fog_features.is_daylight`)の画像は暗くて川霧を判定できないため、学習・評価・答え合わせ・予測の対象外(`predictions.daylight=0`、予報では`fog_model.NOT_PREDICTED`)。

**見た目**: `web_static/glass.css`(macOS風の磨りガラス。base.html・base_auth.html・public_base.htmlでstyle.cssの後に読み込み、色・角丸・影だけを上書きする。外せば元の見た目に戻る)。地点ページ(研究用・公開とも)は`_weather_scene.html`+`web_static/weather_scene.css`で、最新の観測(現象コード)と地点の日の出・日の入りから決めた時間帯に応じた空と、流れる霧・雲・雨を背景に描く(`webapp._weather_scene`)。動きは`web_static/weather_scene.js`(Canvas。霧のかたまり・雲・雨・星を流し、マウスとスクロールで層ごとにずらして奥行きを出す。JSが無い場合はCSSの霧の帯で代用)。ダッシュボードと公開ページのトップも、全地点の最新の観測から決めた代表の状況(`_overview_weather_scene`)で同じ背景を出す。ボタン等の押したときの反応(押すと縮んでばねのように戻る・押した位置から広がる波紋・触れると光が横切る・ページ移動のなめらかな切り替え)は`glass.css`の「動き」の節と`web_static/ui_motion.js`。Spotlight風の検索(Ctrl+K/⌘K・ヘッダーの検索ボタン・「/」。`web_static/spotlight.js`、候補は`webapp._spotlight_items`)、ロック画面風のログイン(base_auth.htmlに全体の背景の空、login.htmlに大きな時計)、天気アプリの都市リスト風の地点一覧(`_station_card_data`。地点ごとの空の色・現在の状況・気温・今日の川霧の出やすさ)。ほかの演出: 動く天気アイコン(`_wx_icon.html`)、地点ページの時間ごとの出やすさの帯(`_station_hourly`)、スクロールで浮かび上がるカードと数字のカウントアップ(`ui_motion.js`)、右上から降りる通知、グラフの読み込み中の光る枠と見た目の統一(base.htmlの`polishChart`)、地図の光る印とダークモードの暗い地図、ホーム画面に追加(`web_static/manifest.json`・`icons/`。サービスワーカーは使わない。ホーム画面から開くと全画面(`display: fullscreen`、非対応のiOS等は`display_override`でstandalone)。グラフの見た目は`chart_style.py`で統一している(色は色覚の多様性に配慮して検証した8色を決まった順に、量の濃淡は青1色の明→暗。線2px・点8px+縁取り・細い実線のグリッド・左寄せのタイトル・上の凡例)。webapp/experiment_viewのグラフは`_chart_json`/`_to_json`で、予測グラフ(fog_graphs.py)は書き出し前に通す。ダークモードの色はbase.htmlの`polishChart`(予測グラフはchart_style.DARK_POST_SCRIPT)。書体は英数字がInter・日本語がZen Kaku Gothic New(どちらもSIL OFL)で、CSPによりフォントは同一オリジンからしか読めないため`web_static/fonts/`に置いて自前配信している(`fonts.css`。Google Fontsの文字範囲ごとの分割ファイルのため、ブラウザはページで使う文字を含む分だけ読み込む)。

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
