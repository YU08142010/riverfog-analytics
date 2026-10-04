# 変更履歴

gitの全コミット履歴をバージョン番号順(1.0〜)に並べたものです(v1.44以降はgitタグから自動で追記)。VERSIONファイル導入(v1.44時点)より前のコミットは、初回コミットを1.0としてコミット順に遡って割り振っています。

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
| 3.5 | 2026-08-30 | `bd26807` | Add CHANGELOG.md documenting the full commit history from v1.0 onward |
| 3.6 | 2026-08-30 | `7808bfe` | Add a What-if fog simulator page |
| 3.7 | 2026-08-30 | `4be6365` | Fix and harden the what-if simulator |
| 3.8 | 2026-08-30 | `c9303c1` | Give the AI chat full knowledge of per-station models and server facts, and add more achievement badges |
| 3.9 | 2026-08-31 | `45171fb` | Fix fog images being misclassified as acquisition failure |
| 3.10 | 2026-08-31 | `d708945` | Add achievement points and avatar frame shop |
| 3.11 | 2026-08-31 | `c01e354` | Raise point values, add non-badge point sources, more achievements, and theme the profile card |
| 3.12 | 2026-08-31 | `4d6abac` | Narrow the fog-probability definition to river fog only |
| 3.13 | 2026-08-31 | `3edc58f` | Add a weather-condition vs river-fog occurrence graph |
| 3.14 | 2026-08-31 | `0121773` | Rename fog-probability labels to river-fog, and split the dual-axis chart |
| 3.15 | 2026-08-31 | `d005209` | Add member-to-member direct messaging |
| 3.16 | 2026-08-31 | `adb75ec` | Add file attachments to DMs, a shared team file space, and message notifications |
| 3.17 | 2026-08-31 | `5f15e73` | Add a one-tap station-share-to-member button |
| 3.18 | 2026-08-31 | `f626d8e` | Remove the file extension whitelist for uploads |
| 3.19 | 2026-08-31 | `56018d1` | Add folders, right-click menu, and previews to the shared files page |
| 3.20 | 2026-08-31 | `2e6102b` | Fix DM bubble height, restyle attach icon, simplify inbox list, and move shared-files actions to a reusable right-click menu |
| 3.21 | 2026-08-31 | `345e674` | Add onboarding README for fresh AI coding agents |
| 3.22 | 2026-08-31 | `95c58cb` | Merge DM inbox thread list and unstarted-member list into one unified list |
| 3.23 | 2026-08-31 | `f46f292` | Document the real users.db path in README to prevent path confusion |
| 3.24 | 2026-08-31 | `af9a5e0` | Preserve original filenames on upload and add DM message delete |
| 3.25 | 2026-09-01 | `f399f8a` | Add a combined-station river-fog-only weather histogram |
| 3.26 | 2026-09-01 | `118d980` | Add a one-off importer for historical JMA-format weather/phenomenon xlsx |
| 3.27 | 2026-09-01 | `188caf7` | Move the river-fog weather histogram to its own top-nav page |
| 3.28 | 2026-09-01 | `6cd314a` | Add dewpoint, solar radiation, sunshine, and visibility to the river-fog histogram |
| 3.29 | 2026-09-01 | `3f80eb9` | Show river-fog histogram bars as percentage instead of raw count |
| 3.30 | 2026-09-02 | `9188b78` | Use uneven low-speed-focused bins for wind speed histogram |
| 3.31 | 2026-09-02 | `82c8106` | Add online/offline presence status for members and DMs |
| 3.32 | 2026-09-02 | `2affaa6` | Consolidate top nav into 4 grouped sections with in-page tabs |
| 3.33 | 2026-09-02 | `4c90f89` | Add account lockout, block admin routes via Funnel, fix real client IP detection |
| 3.34 | 2026-09-03 | `8d50f47` | Add admin role-escalation approval flow, weekly dependency audit and audit-log digest |
| 3.35 | 2026-09-03 | `4860f2b` | Add prediction accuracy breakdown by weather condition (precipitation, temperature) |
| 3.36 | 2026-09-03 | `b68610f` | Add Game tab: move point shop under /game and add fog gambling game |
| 3.37 | 2026-09-03 | `97c03cf` | Cache xlsx parsing to fix major page-load slowness, add ClamAV malware scan on upload |
| 3.38 | 2026-09-03 | `84cce52` | Exclude backfilled weather from training, log classification reasoning, add frame-diff escalation trigger, fix backfill_images.py unpacking bug |
| 3.39 | 2026-09-03 | `dbd8be5` | Add human correction of Gemini phenomena classification and per-station correction summary |
| 3.40 | 2026-09-03 | `6637d7c` | Add dew point depression (shissu) to the river-fog weather histogram |
| 3.41 | 2026-09-03 | `8b067e3` | Migrate weather/phenomena data store from xlsx to SQLite, fix rain-day accuracy, add advection nowcast feature |
| 3.42 | 2026-09-03 | `7278c50` | Add monthly login ranking rewards, expand the point shop, and speed up the site |
| 3.43 | 2026-09-03 | `aa08fa2` | Add three free daily games alongside the paid fog-gambling game |
| 3.44 | 2026-09-03 | `8cf5660` | Expand the shop with 5 more cosmetic categories and 2 more daily-quiz consumables |
| 3.45 | 2026-09-03 | `c47de98` | Add schema and data-access functions for group chat, message features, and file search/favorites |
| 3.46 | 2026-09-03 | `47b1a59` | Add group chat and retrofit DM with reply/react/edit/pin/search/mentions/typing/read receipts |
| 3.47 | 2026-09-03 | `d5b3904` | Fix fog-bet race condition that resolved bets before data existed, and messaging UI bugs |
| 3.48 | 2026-09-03 | `f8b1efe` | Add file-sharing search, favorites, auto-tagging, and bulk move/delete/download |
| 3.49 | 2026-09-03 | `e4c7abd` | Add inbox sort toggle and always-visible quick actions (mark unread, pin conversation) |
| 3.50 | 2026-09-03 | `551e48c` | Fix reply-cancel button not hiding, add Enter-to-send in chats |
| 3.51 | 2026-09-03 | `9ea88ee` | Add achievement tiers, 8 new shop categories, shop UX overhaul, and live nav status |
| 3.52 | 2026-09-03 | `41c04fe` | Let the what-if simulator run with only some fields filled in |
| 3.53 | 2026-09-03 | `e5a7232` | Render reply-to previews immediately instead of only after reload |
| 3.54 | 2026-09-03 | `1bfa893` | Fix fukubukuro jackpot odds being ~100% instead of 1-in-5 |
| 3.55 | 2026-09-03 | `2c4cca0` | Let admins bypass maintenance mode, and live-update group read counts |
| 3.56 | 2026-09-03 | `790cfa4` | Remove the character limit on the self-introduction field |
| 3.57 | 2026-09-03 | `1ed4e36` | Remove the gambling max-bet cap, add two titles and an admin-only title |
| 3.58 | 2026-09-03 | `7aacca8` | Raise gacha cost to 5000pt, fix stale equip tag after buying an item |
| 3.59 | 2026-09-03 | `8954259` | Add observation reporting and automatic anomaly correction |
| 3.60 | 2026-09-03 | `29966ae` | Reward the reporter when an observation report turns out correct |
| 3.61 | 2026-09-03 | `0a4f740` | Raise the observation report reward to 2000pt |
| 3.62 | 2026-09-03 | `6a986dc` | Show the station camera photo alongside each observation report |
| 3.63 | 2026-09-03 | `0e414f4` | Redesign shared files page with a Teams-style toolbar and table |
| 3.64 | 2026-09-03 | `780dd22` | Add structural safeguards against another points-economy exploit |
| 3.65 | 2026-09-03 | `46b5d7e` | Remove the admin-only title, raise both daily quiz rewards to 500pt |
| 3.66 | 2026-09-03 | `8af7999` | Fix DM chat misattributing own messages, and stale bfcache page state |
| 3.67 | 2026-09-03 | `624e276` | Add ranking types beyond login count |
| 3.68 | 2026-09-04 | `e942bbb` | Add Aizu region (20 stations) alongside Tadami river, split Excel export by region |
| 3.69 | 2026-09-04 | `c2a8f7e` | Fix mobile layout: header nav overflow, chat header/compose bar, input zoom |
| 3.70 | 2026-09-04 | `7124ab7` | Add 8 more Aizu stations resolved from user follow-up (28 total) |
| 3.71 | 2026-09-05 | `e67536c` | Fix camera image fetch for Aizu stations using river.go.jp's cctv_* id scheme |
| 3.72 | 2026-09-05 | `14b886d` | Rename site from "只見川霧観測" to "川霧観測・予測", fix mobile notif panel |
| 4.0 | 2026-09-05 | `7343fbe` | Bring Aizu stations to full feature parity and add community feedback |
| 4.1 | 2026-09-05 | `3e70d33` | Add remaining Aizu stations, retire decommissioned camera, and small UX fixes |
| 4.2 | 2026-09-05 | `4cb67ed` | Add the last requested Aizu station (大石) |
| 4.3 | 2026-09-05 | `1b45041` | Apply 只見川/会津 region grouping site-wide, not just the dashboard |
| 5.0 | 2026-09-06 | `bd7cd49` | v5.0: Security hardening, ClamAV/Tailscale admin pages, login-history fix, and region-scoped observer roles |
| 5.1 | 2026-09-06 | `1041349` | Move the 観測報告 nav link next to 地点 instead of the far end of the bar |
| 5.2 | 2026-09-06 | `67b6ab8` | Show who answered each announcement survey (choice and free-text) |
| 5.3 | 2026-09-06 | `cc09b5a` | Add an observer role: full observation-report access, no other admin tabs |
| 5.4 | 2026-09-11 | `2126ec7` | Add region-split dashboards, points/gems economy overhaul, and gamble rename |
| 5.5 | 2026-09-11 | `1fa3af4` | Reward auto-detected corrections with gems and rework the gem shop |
| 5.6 | 2026-09-11 | `95b6548` | Make the merged gem items in the shop easier to find |
| 5.7 | 2026-09-11 | `75373bc` | Add a gem-priced top item to every remaining shop category |
| 5.8 | 2026-09-11 | `c989308` | Show neighboring hours phenomena, not just images, on every report |
| 5.9 | 2026-09-11 | `549e0bf` | Fix Funnel detection false negative for internal Tailscale serve access |
| 5.10 | 2026-09-11 | `869b993` | Move nginx internal header secret out of source code |
| 5.11 | 2026-09-12 | `ed18b9c` | Expand analysis page with precision/recall/F1, dark-mode charts, and fix a missing permission check on direct corrections |
| 5.12 | 2026-09-13 | `1b82d16` | Fix audit findings, add report reasons and tree-depth viz, unify fog terminology |
| 5.13 | 2026-09-13 | `cfa9a93` | Replace the phenomenon correction dropdown with a two-step button picker |
| 5.14 | 2026-09-13 | `9be14b1` | Apply the phenomenon picker to the observation reports queue, extract shared assets |
| 5.15 | 2026-09-13 | `f4e5a73` | Redesign the phenomenon picker: real colors, focus-based shortcuts, dismiss key |
| 5.16 | 2026-09-13 | `abcbbc6` | Support recording and predicting up to two simultaneous phenomenon codes |
| 5.17 | 2026-09-14 | `ca8867c` | Migrate dashboard, distribution, and forecast-archive charts from matplotlib to Plotly |
| 5.18 | 2026-09-14 | `19a8bd1` | Surface secondary-code (dual phenomenon) status and share chart theme colors |
| 5.19 | 2026-10-03 | `7324fe6` | Bill monthly item fees on the 1st of each month instead of one month after purchase |
| 5.20 | 2026-10-03 | `4d99bc4` | Stop feeding weather hints into the Gemini classification prompt by default |
| 5.21 | 2026-10-03 | `bc40a1f` | Add an admin experiment tab for a binary fog model and label-quality audit |
| 5.22 | 2026-10-03 | `64b6e18` | Compare old and new models on the same unseen test rows in the regression guard |
| 5.23 | 2026-10-03 | `60d8e58` | Move observation reports out of the admin area into its own top-level page |
| 5.24 | 2026-10-04 | `44808d5` | Replace per-station model selection with one shared binary LightGBM fog model |
