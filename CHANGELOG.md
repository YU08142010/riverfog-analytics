# 変更履歴

gitの全コミット履歴をバージョン番号順(1.0〜)に並べたものです。VERSIONファイル導入(v1.44時点)より前のコミットは、初回コミットを1.0としてコミット順に遡って割り振っています。

| バージョン | 日付 | コミット | 内容 |
|---|---|---|---|
| 1.0 | 2026-08-25 | `0260a72` | Initial commit: baseline collect.py before reliability/AI-detection improvements |
| 1.1 | 2026-08-25 | `0d5a902` | Split shared constants into common.py; replace PNG content-type heuristic with Gemini-based placeholder detection (code -1); prevent duplicate rows by overwriting the existing row for the same timestamp instead of appending |
| 1.2 | 2026-08-25 | `24c5f6a` | Add backfill_weather.py to fill missing weather rows |
| 1.3 | 2026-08-25 | `1569729` | Archive images per collection hour instead of one flat folder per day |
| 1.4 | 2026-08-25 | `b00a3e8` | Use HH-time (e.g. 15時) instead of HHMM for image hour folder names |
| 1.5 | 2026-08-25 | `291f2e0` | Revert "Use HH-time (e.g. 15時) instead of HHMM for image hour folder names" |
| 1.6 | 2026-08-25 | `e38b540` | Teach Gemini prompt to distinguish lens water droplets/mud from real fog |
| 1.7 | 2026-08-25 | `9951921` | Note that 4 specific camera lenses have a chronic baseline haze |
| 1.8 | 2026-08-25 | `50708e7` | Add requirements.txt (pip freeze) and logrotate.conf for logs/cron.log |
| 1.9 | 2026-08-26 | `e509ff7` | Improve backfill_weather.py: detect fully-missing rows, batch API calls by date, quiet old-row logging |
| 1.10 | 2026-08-26 | `87c83f3` | Add fog prediction/graphing with daily verification and retraining loop |
| 1.11 | 2026-08-26 | `952310a` | Drop unused nvidia-nccl-cu13 GPU dependency pulled in by xgboost |
| 1.12 | 2026-08-27 | `e748b6d` | Add per-station fog-rate feature to pooled model |
| 1.13 | 2026-08-27 | `93edf3a` | Rotate collect.log, backfill_weather.log, predict.log too |
| 1.14 | 2026-08-27 | `37deac4` | Add automatic rollback when retrained model regresses |
| 1.15 | 2026-08-27 | `9c18df7` | Auto re-classify low-confidence Gemini fog classifications |
| 1.16 | 2026-08-27 | `3f54dcd` | Replace fixed regression tolerance with bootstrap significance test |
| 1.17 | 2026-08-27 | `d4f5ad9` | Add web dashboard for browsing observations, images, and forecasts |
| 1.18 | 2026-08-27 | `bb4cdb4` | Add station pages, accuracy trend, fog alerts, timelapse, and admin log viewer |
| 1.19 | 2026-08-27 | `d1abad9` | Collapse fog alert list on dashboard to first 5 rows with expand toggle |
| 1.20 | 2026-08-27 | `db7b371` | Fix broken escalation model default; archive fetch failures as placeholders |
| 1.21 | 2026-08-27 | `e4585af` | Replace admin Basic Auth with a session login that expires after 15min |
| 1.22 | 2026-08-27 | `76dfbcd` | Improve list/nav UI: searchable station tiles, active nav highlight |
| 1.23 | 2026-08-27 | `4857f5c` | Fix accuracy trend chart axis defaulting to 7/1 and 1/1 tick labels |
| 1.24 | 2026-08-27 | `6686cec` | Add admin panel features: status, errors, models, disk, manual run |
| 1.25 | 2026-08-27 | `fd789c3` | Refresh overall visual design with a shared token system |
| 1.26 | 2026-08-27 | `49baba6` | Add camera health check, dark mode, comparison, weather charts, feature importance, model comparison, site search, downloads |
| 1.27 | 2026-08-27 | `648b18b` | Remove dark mode |
| 1.28 | 2026-08-27 | `5c3b945` | Add 13 admin features plus a dashboard announcement/survey system |
| 1.29 | 2026-08-27 | `1eec246` | Add admin login link to the maintenance page |
| 1.30 | 2026-08-27 | `151b8d0` | Replace shared admin password with real accounts, roles, and signup approval |
| 1.31 | 2026-08-27 | `286cf84` | Require login for the entire site, not just admin pages |
| 1.32 | 2026-08-27 | `e6551c6` | Let admins edit name, email, and password from account management |
| 1.33 | 2026-08-27 | `83bd68a` | Add cross-station fog feature, confidence-based label exclusion, and weather gap interpolation |
| 1.34 | 2026-08-27 | `c630f96` | Add toast notifications, breadcrumbs, chart spinners, and account management UX |
| 1.35 | 2026-08-27 | `e2e7411` | Keep camera image archive indefinitely, add manual cleanup from admin UI |
| 1.36 | 2026-08-27 | `b6f9959` | Group date lists by month instead of a flat button grid |
| 1.37 | 2026-08-27 | `ed3717a` | Add Members tab with login history, activity heatmap, and cropped avatars |
| 1.38 | 2026-08-28 | `f6c3db6` | Add admin operation audit log |
| 1.39 | 2026-08-28 | `5f1df7c` | Add backfill_images.py to recover camera images from cam.river.go.jp history |
| 1.40 | 2026-08-28 | `ec21d4b` | Re-add dark mode, fixing the contrast bug that caused the last revert |
| 1.41 | 2026-08-28 | `4a007bc` | Stop forecasting rain/drizzle, since general weather forecasts cover it |
| 1.42 | 2026-08-28 | `ca1b0a0` | Add backup ZIP restore alongside the existing backup download |
| 1.43 | 2026-08-28 | `2a90580` | Show the newest timelapse image by default, add month/day jump |
| 1.44 | 2026-08-28 | `f393a23` | Add a macOS-style station sidebar, soften the visual language |
| 1.45 | 2026-08-28 | `78c8b97` | Add a station map page and automatic version numbering |
| 1.46 | 2026-08-28 | `199cb99` | Embed a live Google Maps view on the map page instead of link-only |
| 1.47 | 2026-08-28 | `fca1b43` | Add prefecture to map search query to reduce name-collision ambiguity |
| 1.48 | 2026-08-28 | `317dada` | Remove the dot-map toggle from the map page |
| 1.49 | 2026-08-28 | `0dff6db` | Add LightGBM as a comparison model alongside RF-only/XGB-only metrics |
| 1.50 | 2026-08-28 | `8850645` | Add RF+LightGBM hybrid as a 5th model-comparison option |
| 1.51 | 2026-08-28 | `79ae3f1` | Add DNS fallback for external fetches and extend admin session lifetime |
| 1.52 | 2026-08-28 | `32c604c` | Fix map pinpoint accuracy and expand member profile features |
| 1.53 | 2026-08-28 | `0a1ffce` | Add per-station weather going forward, keeping history on the shared feed |
| 1.54 | 2026-08-28 | `6cc6a65` | Make the admin nav searchable like the station list page |
| 1.55 | 2026-08-28 | `5749b2f` | Add an embedded web terminal to the admin menu |
| 1.56 | 2026-08-28 | `3003a2e` | Move admin menu into a left sidebar, and give login/signup a standalone page |
| 1.57 | 2026-08-28 | `cf6a8ab` | Record which station a network failure happened at |
| 2.0 | 2026-08-28 | `c37c4d1` | Major UI overhaul (Cockpit-inspired) and add Gemini-generated summaries (v2.0) |
| 2.1 | 2026-08-28 | `78aafcb` | Fix confirm modal showing on every page load, blocking all clicks |
| 2.2 | 2026-08-28 | `8cd12f4` | Widen main layout and pair up chart cards to reduce scrolling |
| 2.3 | 2026-08-28 | `e1a026e` | Replace map page with a real all-stations map, and add a fog-themed login screen |
| 2.4 | 2026-08-29 | `7a3133f` | Un-pair dashboard's image/summary cards, and raise fog alert threshold to 85% |
| 2.5 | 2026-08-29 | `74a06b1` | Add a notification bell, raise fog alert threshold to 95%, and retire the manual WEATHER_LAT/LON setting |
| 2.6 | 2026-08-29 | `2f683ec` | Address 133-item code review, add SMB status page, live resource monitoring, and Gemini quota handling |
| 2.7 | 2026-08-29 | `d79531d` | Add subtle UI animations and convert several actions to AJAX |
| 2.8 | 2026-08-29 | `35d0415` | Add favorite-station notification filtering, camera-failure alerts, PDF reports, a changelog page, and a prediction feature breakdown |
| 2.9 | 2026-08-29 | `8d96c12` | Add AI chat feature with daily rate limits, and fix deprecated Gemini model names causing silent classification failures |
| 2.10 | 2026-08-29 | `ea251a4` | Redesign AI chat as a full page, fix header nav wrapping, and give the chat far more system/live knowledge |
| 2.11 | 2026-08-29 | `9e0cab1` | Turn AI chat into multi-conversation history with AI-generated titles |
| 2.12 | 2026-08-29 | `3e57d27` | Allow deleting a chat conversation |
| 2.13 | 2026-08-29 | `e7ef3bb` | Improve member ranking styling, add monthly ranking toggle and achievement badges |
| 2.14 | 2026-08-29 | `04d4841` | Expand achievement badges with real icons, default ranking to this month, and drop native confirm() |
| 2.15 | 2026-08-29 | `3e9a0d9` | Fix timelapse image display size varying by camera resolution |
| 3.0 | 2026-08-30 | `d7f0231` | Auto-select the best model per station (RF/XGBoost/LightGBM and their blends), show live per-station weather on station pages |
| 3.1 | 2026-08-30 | `c3abd41` | Fix overlapping labels on the model-comparison chart and update the stale hybrid-only admin text |
| 3.2 | 2026-08-30 | `ab7ca52` | Normalize legacy hybrid model-type label surviving in regression-rolled-back stations |
| 3.3 | 2026-08-30 | `11e475d` | Lay out station weather info as a wrapping tile grid instead of a narrow table |
| 3.4 | 2026-08-30 | `6a5432d` | Improve the fog classification prompt: explicit boundary rules, chain-of-thought reasoning, weather context, and smarter escalation |
