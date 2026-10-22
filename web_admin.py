"""webapp.py から分けた部分(web_admin)。webapp.py の最後で読み込まれ、ここで定義した名前は webapp にも戻される。
ルート・テンプレート・URLの名前(エンドポイント)は分ける前と同じ。"""
import io
import os
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from flask import abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from common import BASE_DIR, JST, build_data_backup_zip, now_jst_naive
import announcements
import users
import fog_data
import prediction_log
from fog_features import (
    COL_HUMIDITY, COL_PRECIP, COL_SUNSHINE, COL_TEMP, COL_WIND_SPEED, PHENOM_COLORS, PHENOM_LABELS, SUMMARY_FOG_CODES, encode_phenomena_cell, phenom_codes_set, primary_phenom_code,
)
import chat_log

from webapp import (ALLOWED_LOGS, CAMERA_HEALTH_DAYS, CHAT_DAILY_LIMIT_PER_USER, CHAT_DAILY_LIMIT_TOTAL, CHAT_DB_PATH, 
    DB_CLEANUP_DEFAULT_DAYS, DB_PATH, DISK_USAGE_PARTS, EDITABLE_ENV_KEYS, ENV_PATH, GEMINI_LOG_DAYS, IMAGE_CLEANUP_DEFAULT_DAYS, 
    IMAGE_DIR, LOG_TAIL_LINES, MIN_PASSWORD_LENGTH, OBSERVER_ROLE_REGIONS, REPORT_RATE_LIMIT_WINDOW, 
    RESTORE_BACKUP_KEEP_GENERATIONS, RESTORE_CONFIRM_PHRASE, RESTORE_MAX_UNCOMPRESSED_BYTES, RESTORE_MAX_UPLOAD_BYTES, 
    RUNNABLE_SCRIPTS, SMB_CONF_PATH, SMB_SHARE_NAME, STATION_REGION, SYSTEMD_UNITS, _accuracy_summary_safe,
    _accuracy_trend_safe, _admin_overview, _announcements_conn, _cached_chart_json, _camera_uptime_stats, _crontab_safe, 
    _dir_size_bytes, _env_status, _finish, _gemini_log_stats, _human_size, _is_funnel_request, _is_script_running, 
    _maintenance_state, _network_failure_summary, _read_editable_env_values, _read_server_resources, _read_smb_share_config, 
    _recent_log_issues, _record_audit, _report_rate_limited, _run_script_async, _send_alert_throttled, _set_maintenance_state, 
    _systemctl_show, _tailscale_autoupdate_prefs, _tailscale_status, _tailscale_version, _users_conn, _wants_json, 
    _write_editable_env_values, app, log, requires_admin)  # noqa: F401


@app.route("/admin")
@requires_admin
def admin_home():
    logs = [{"key": k, "path": str(p), "exists": p.exists(),
             "size_kb": round(p.stat().st_size / 1024, 1) if p.exists() else 0}
            for k, p in ALLOWED_LOGS.items()]
    return render_template("admin.html", logs=logs, overview=_admin_overview())


@app.route("/admin/audit_log")
@requires_admin
def admin_audit_log():
    conn = _users_conn()
    try:
        entries = users.list_audit_log(conn, limit=500)
    finally:
        conn.close()
    return render_template("admin_audit_log.html", entries=entries)


@app.route("/admin/points")
@requires_admin
def admin_points():
    conn = _users_conn()
    try:
        accounts = users.list_users(conn)
        balances = users.list_all_user_point_balances(conn)
        gem_balances = users.list_all_user_gem_balances(conn)
    finally:
        conn.close()
    return render_template(
        "admin_points.html", accounts=accounts, balances=balances, gem_balances=gem_balances,
    )


@app.route("/admin/points/<int:user_id>/adjust", methods=["POST"])
@requires_admin
def admin_points_adjust(user_id):
    conn = _users_conn()
    try:
        target = users.get_user_by_id(conn, user_id)
        if target is None:
            abort(404)
        raw = (request.form.get("amount") or "").strip()
        try:
            amount = int(raw)
        except ValueError:
            return _finish("admin_points", "ポイント数は整数で入力してください。", success=False)
        if amount == 0:
            return _finish("admin_points", "0ポイントは指定できません。", success=False)
        now_str = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
        source = f"admin_adjust:{session.get('user_id')}:{now_str}:{secrets.token_hex(4)}"
        users.grant_points(conn, user_id, source, amount, now_str)
        balance = users.available_points(conn, user_id)
    finally:
        conn.close()
    _record_audit(
        "ポイント手動調整",
        f"{target['name']}({target['email']})に{amount:+d}pt付与(調整後残高: {balance}pt)",
    )
    return _finish(
        "admin_points", f"{target['name']}さんに{amount:+d}ptを反映しました(残高: {balance}pt)。",
        extra={"user_id": user_id, "balance": balance},
    )


@app.route("/admin/points/<int:user_id>/adjust_gems", methods=["POST"])
@requires_admin
def admin_points_adjust_gems(user_id):
    conn = _users_conn()
    try:
        target = users.get_user_by_id(conn, user_id)
        if target is None:
            abort(404)
        raw = (request.form.get("amount") or "").strip()
        try:
            amount = int(raw)
        except ValueError:
            return _finish("admin_points", "ジェム数は整数で入力してください。", success=False)
        if amount == 0:
            return _finish("admin_points", "0ジェムは指定できません。", success=False)
        now_str = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
        source = f"admin_adjust_gems:{session.get('user_id')}:{now_str}:{secrets.token_hex(4)}"
        users.grant_gems(conn, user_id, source, amount, now_str)
        balance = users.available_gems(conn, user_id)
    finally:
        conn.close()
    _record_audit(
        "ジェム手動調整",
        f"{target['name']}({target['email']})に{amount:+d}ジェム付与(調整後残高: {balance}ジェム)",
    )
    return _finish(
        "admin_points", f"{target['name']}さんに{amount:+d}ジェムを反映しました(残高: {balance}ジェム)。",
        extra={"user_id": user_id, "gem_balance": balance},
    )


@app.route("/admin/accounts")
@requires_admin
def admin_accounts():
    conn = _users_conn()
    try:
        accounts = users.list_users(conn)
        pending = users.list_signup_requests(conn, status="pending")
        locked_failures = {
            a["id"]: users.recent_login_failures(conn, a["email"])
            for a in accounts if a["locked_at"]
        }
        role_requests = users.list_pending_role_change_requests(conn)
        role_request_users = users.get_users_by_ids(
            conn, [r["target_user_id"] for r in role_requests] + [r["requested_by"] for r in role_requests]
        )
    finally:
        conn.close()
    return render_template(
        "admin_accounts.html", accounts=accounts, pending=pending, locked_failures=locked_failures,
        role_requests=role_requests, role_request_users=role_request_users,
    )


@app.route("/admin/accounts/<int:user_id>/unlock", methods=["POST"])
@requires_admin
def admin_accounts_unlock(user_id):
    conn = _users_conn()
    try:
        target = users.get_user_by_id(conn, user_id)
        if target is None:
            abort(404)
        users.unlock_account(conn, user_id)
        _record_audit("ロック解除", f"{target['name']}({target['email']})のアカウントロックを解除")
        return _finish("admin_accounts", f"{target['name']}さんのロックを解除しました。")
    finally:
        conn.close()


@app.route("/admin/accounts/<int:user_id>/role", methods=["POST"])
@requires_admin
def admin_accounts_role(user_id):
    """管理者への昇格(general→admin)は、権限昇格の悪用・誤操作を防ぐため即時反映せず、
    別の管理者による承認を必須にする(admin_accounts_role_approve参照)。降格
    (admin→general)は安全側の変更なので従来どおり即時反映する。観測者/会津観測者/
    只見観測者は観測報告の確認・訂正権限に限られ、他の管理機能には一切関与しないため、
    管理者昇格ほどの慎重さは不要と判断し、即時反映にしている。"""
    role = request.form.get("role")
    if role not in ("general", "admin", "observer", *OBSERVER_ROLE_REGIONS):
        abort(400)
    conn = _users_conn()
    try:
        target = users.get_user_by_id(conn, user_id)
        if target is None:
            abort(404)
        if target["role"] == "admin" and role != "admin" and users.count_admins(conn) <= 1:
            return _finish("admin_accounts", "最後の管理者アカウントの権限は変更できません。", success=False)
        if role == "admin" and target["role"] != "admin":
            now_str = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
            users.create_role_change_request(conn, user_id, role, session["user_id"], now_str)
            _record_audit("管理者昇格を申請", f"{target['name']}({target['email']})を管理者に昇格申請")
            _send_alert_throttled(
                f"role_request:{user_id}",
                f"{session.get('name')}さんが{target['name']}さんの管理者昇格を申請しました。"
                f"他の管理者の承認が必要です。",
            )
            return _finish(
                "admin_accounts",
                f"{target['name']}さんの管理者昇格を申請しました。他の管理者の承認後に反映されます。",
            )
        users.set_role(conn, user_id, role)
        _record_audit("ロール変更", f"{target['name']}({target['email']})を{target['role']}→{role}に変更")
        return _finish("admin_accounts", f"{target['name']}さんのロールを変更しました。")
    finally:
        conn.close()


@app.route("/admin/accounts/role-requests/<int:request_id>/approve", methods=["POST"])
@requires_admin
def admin_accounts_role_approve(request_id):
    conn = _users_conn()
    try:
        req = users.get_role_change_request(conn, request_id)
        if req is None or req["status"] != "pending":
            abort(404)
        if req["requested_by"] == session["user_id"]:
            return _finish("admin_accounts", "自分自身が申請した昇格は承認できません。別の管理者に依頼してください。", success=False)
        target = users.get_user_by_id(conn, req["target_user_id"])
        if target is None:
            users.set_role_change_request_status(conn, request_id, "canceled")
            return _finish("admin_accounts", "対象のアカウントが見つかりませんでした。", success=False)
        users.set_role(conn, target["id"], req["requested_role"])
        users.set_role_change_request_status(conn, request_id, "approved")
        _record_audit("管理者昇格を承認", f"{target['name']}({target['email']})の管理者昇格を承認")
        return _finish("admin_accounts", f"{target['name']}さんを管理者に昇格しました。")
    finally:
        conn.close()


@app.route("/admin/accounts/role-requests/<int:request_id>/reject", methods=["POST"])
@requires_admin
def admin_accounts_role_reject(request_id):
    conn = _users_conn()
    try:
        req = users.get_role_change_request(conn, request_id)
        if req is None or req["status"] != "pending":
            abort(404)
        target = users.get_user_by_id(conn, req["target_user_id"])
        users.set_role_change_request_status(conn, request_id, "rejected")
        _record_audit(
            "管理者昇格を却下",
            f"{target['name'] if target else req['target_user_id']}の管理者昇格申請を却下",
        )
        return _finish("admin_accounts", "昇格申請を却下しました。")
    finally:
        conn.close()


@app.route("/admin/accounts/<int:user_id>/edit", methods=["POST"])
@requires_admin
def admin_accounts_edit(user_id):
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")

    conn = _users_conn()
    try:
        target = users.get_user_by_id(conn, user_id)
        if target is None:
            abort(404)

        if not name or not email:
            return _finish("admin_accounts", "お名前とメールアドレスは必須です。", success=False)
        if "@" not in email:
            return _finish("admin_accounts", "メールアドレスの形式が正しくありません。", success=False)
        existing = users.get_user_by_email(conn, email)
        if existing and existing["id"] != user_id:
            return _finish("admin_accounts", "このメールアドレスは既に別のアカウントで使われています。", success=False)
        if password and len(password) < MIN_PASSWORD_LENGTH:
            return _finish(
                "admin_accounts", f"パスワードを変更する場合は{MIN_PASSWORD_LENGTH}文字以上にしてください。",
                success=False,
            )

        password_hash = users.generate_password_hash(password) if password else None
        users.update_user(conn, user_id, name=name, email=email, password_hash=password_hash)
        if session.get("user_id") == user_id:
            session["name"] = name
        changes = []
        if target["name"] != name:
            changes.append(f"名前: {target['name']}→{name}")
        if target["email"] != email:
            changes.append(f"メール: {target['email']}→{email}")
        if password:
            changes.append("パスワード変更")
        _record_audit("アカウント編集", f"{name}({email}): " + ("、".join(changes) if changes else "変更なし"))
        return _finish("admin_accounts", f"{name}さんの情報を更新しました。")
    finally:
        conn.close()


@app.route("/admin/accounts/<int:user_id>/delete", methods=["POST"])
@requires_admin
def admin_accounts_delete(user_id):
    conn = _users_conn()
    try:
        target = users.get_user_by_id(conn, user_id)
        if target is None:
            abort(404)
        if target["role"] == "admin" and users.count_admins(conn) <= 1:
            return _finish("admin_accounts", "最後の管理者アカウントは削除できません。", success=False)
        users.delete_user(conn, user_id)
        message = f"{target['name']}さんのアカウントを削除しました。"
    finally:
        conn.close()
    # chat.dbはusers.dbと別ファイルのためON DELETE CASCADEが効かない。ここで明示的に
    # 削除しないとAIチャット履歴が孤立したまま残り続ける(2026-09-13の監査で指摘)。
    chat_conn = chat_log.connect(CHAT_DB_PATH)
    try:
        chat_log.delete_user_data(chat_conn, user_id)
    finally:
        chat_conn.close()
    _record_audit("アカウント削除", f"{target['name']}({target['email']})を削除")
    if session.get("user_id") == user_id:
        session.clear()
        if _wants_json():
            return jsonify({"message": message, "success": True, "redirect": url_for("login")})
        flash(message, "success")
        return redirect(url_for("login"))
    return _finish("admin_accounts", message)


@app.route("/admin/accounts/requests/<int:request_id>/approve", methods=["POST"])
@requires_admin
def admin_accounts_approve(request_id):
    conn = _users_conn()
    try:
        req = users.get_signup_request(conn, request_id)
        created_at = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
        ok = users.approve_signup_request(conn, request_id, created_at)
    finally:
        conn.close()
    if ok and req:
        _record_audit("登録申請承認", f"{req['name']}({req['email']})")
    return _finish(
        "admin_accounts", "承認してアカウントを作成しました。" if ok else "この申請は既に処理済みです。",
        success=ok,
    )


@app.route("/admin/accounts/requests/<int:request_id>/reject", methods=["POST"])
@requires_admin
def admin_accounts_reject(request_id):
    conn = _users_conn()
    try:
        req = users.get_signup_request(conn, request_id)
        users.reject_signup_request(conn, request_id)
    finally:
        conn.close()
    if req:
        _record_audit("登録申請拒否", f"{req['name']}({req['email']})")
    return _finish("admin_accounts", "申請を拒否しました。")


@app.route("/admin/accounts/requests/bulk", methods=["POST"])
@requires_admin
def admin_accounts_requests_bulk():
    """選択した複数の登録申請を、まとめて承認/拒否する。"""
    action = request.form.get("bulk_action")
    request_ids = request.form.getlist("request_ids")
    if action not in ("approve", "reject") or not request_ids:
        return _finish("admin_accounts", "対象の申請を選択してください。", success=False)

    conn = _users_conn()
    processed = []
    try:
        created_at = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
        n = 0
        for rid in request_ids:
            try:
                rid = int(rid)
            except ValueError:
                continue
            req = users.get_signup_request(conn, rid)
            if action == "approve":
                if users.approve_signup_request(conn, rid, created_at):
                    n += 1
                    if req:
                        processed.append(req["email"])
            else:
                users.reject_signup_request(conn, rid)
                n += 1
                if req:
                    processed.append(req["email"])
    finally:
        conn.close()
    verb = "承認" if action == "approve" else "拒否"
    if processed:
        _record_audit(f"登録申請一括{verb}", f"{n}件: " + "、".join(processed))
    return _finish("admin_accounts", f"{n}件の申請を{verb}しました。")


@app.route("/admin/logs/<key>")
@requires_admin
def admin_log(key):
    path = ALLOWED_LOGS.get(key)
    if path is None:
        abort(404)
    if not path.exists():
        return render_template("admin_log.html", key=key, lines=["(ログファイルがまだありません)"])
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()[-LOG_TAIL_LINES:]
    return render_template("admin_log.html", key=key, lines=lines)


@app.route("/admin/errors")
@requires_admin
def admin_errors():
    return render_template(
        "admin_errors.html", issues=_recent_log_issues(),
        network_failures=_network_failure_summary(),
    )


@app.route("/admin/status")
@requires_admin
def admin_status():
    props = ["ActiveState", "SubState", "Result", "ActiveEnterTimestamp",
              "ExecMainStartTimestamp", "ExecMainExitTimestamp"]
    units = [{"unit": unit, **_systemctl_show(unit, props)} for unit in SYSTEMD_UNITS]
    timer_info = _systemctl_show("river-fog-collect.timer", ["NextElapseUSecRealtime", "LastTriggerUSec"])
    running = {key: _is_script_running(script) for key, script in RUNNABLE_SCRIPTS.items()}
    return render_template(
        "admin_status.html", units=units, timer_info=timer_info,
        crontab=_crontab_safe(), running=running,
        resources=_read_server_resources(),
    )


@app.route("/admin/auth-check")
def admin_auth_check():
    """nginxのauth_request用。/cockpit-ui/(Cockpit)はFlaskを通らずnginxから直接
    プロキシされるため、以前はtailnet内からなら誰でもCockpitのログイン画面で
    Basic認証の総当たりを試せた(2026-10-08の監査で指摘)。nginxがこのURLに副リクエストを
    送り、管理者としてログイン中(requires_adminと同じ条件)の時だけ204を返して通す。
    リダイレクトはauth_requestでは使えないため401を返す。"""
    if session.get("user_id") and session.get("role") == "admin" and not _is_funnel_request():
        return "", 204
    return "", 401


@app.route("/admin/cockpit")
@requires_admin
def admin_cockpit():
    return render_template("admin_cockpit.html")


@app.route("/admin/status/resources.json")
@requires_admin
def admin_status_resources_json():
    """サーバーリソース(CPU負荷・メモリ・温度)をJSONで返す。admin_status.htmlが
    数秒おきにポーリングしてCockpit風にリアルタイム更新するために使う。
    /proc・/sysの読み取りのみでsubprocessを呼ばないため、頻繁に呼んでも軽い。
    """
    return jsonify(_read_server_resources())


@app.route("/admin/smb")
@requires_admin
def admin_smb():
    """Samba([river-fog]共有)の設定・サービス状態を閲覧専用で表示する。
    設定の変更やsmbd/nmbdの再起動はここでは行わない(sudoersの追加が必要になり、
    ネットワーク共有の設定に管理画面から書き込めるようにするリスクが見合わないと
    判断し、閲覧のみに留めた)。設定を変更したい場合はサーバー上で直接
    /etc/samba/smb.confを編集し、`sudo systemctl reload smbd`すること。
    """
    share = _read_smb_share_config()
    smbd_state = _systemctl_show("smbd", ["ActiveState", "SubState"])
    nmbd_state = _systemctl_show("nmbd", ["ActiveState", "SubState"])
    return render_template(
        "admin_smb.html",
        share=share, share_name=SMB_SHARE_NAME,
        smbd_state=smbd_state, nmbd_state=nmbd_state,
        smb_conf_path=str(SMB_CONF_PATH),
    )


@app.route("/admin/tailscale")
@requires_admin
def admin_tailscale():
    """Tailscaleの接続状態を閲覧専用で表示する。状態を変更する操作(up/down/logout等)は
    一切用意しない: このサーバーへのSSH・管理画面アクセスの経路そのものがTailscale
    経由であり、誤操作で無効化すると物理アクセスかLAN側からの復旧作業が必要になる
    (admin_smb.htmlと同じ「sudoersを増やすリスクとの見合い」の考え方だが、
    ここは影響がリモートアクセス経路そのものに及ぶためより厳格に閲覧専用とした)。
    """
    status = _tailscale_status()
    self_info = (status or {}).get("Self") or {}
    peers = sorted(
        (status or {}).get("Peer", {}).values(),
        key=lambda p: (not p.get("Online"), (p.get("HostName") or "").lower()),
    )
    return render_template(
        "admin_tailscale.html",
        backend_state=(status or {}).get("BackendState"),
        self_info=self_info, peers=peers,
        version=_tailscale_version(), autoupdate=_tailscale_autoupdate_prefs(),
    )


@app.route("/admin/run/<key>", methods=["POST"])
@requires_admin
def admin_run(key):
    script = RUNNABLE_SCRIPTS.get(key)
    if script is None:
        abort(404)
    if _is_script_running(script):
        return _finish("admin_status", f"{script} は既に実行中のため、開始しませんでした。", success=False)
    _run_script_async(script)
    return _finish("admin_status", f"{script} の実行を開始しました。数分後にログを確認してください。")


@app.route("/admin/restart", methods=["POST"])
@requires_admin
def admin_restart():
    """webappサービス自体の再起動。今応答しているプロセスがまさに落ちるため、応答を
    返し切ってから少し遅れて再起動するようにする（sudoers側でこのコマンド1つだけ
    パスワード無しの実行を許可している）。"""
    try:
        subprocess.Popen(
            ["/bin/sh", "-c", "sleep 1 && sudo -n systemctl restart river-fog-web.service"],
            start_new_session=True,
        )
    except Exception:
        log.exception("Failed to trigger webapp restart")
        return _finish("admin_status", "再起動の開始に失敗しました。詳細はログを確認してください。", success=False)
    return _finish("admin_status", "webappサービスを再起動します(数秒後にページを再読み込みしてください)。")


@app.route("/admin/models")
@requires_admin
def admin_models():
    """モデルの精度・F1・混同行列・特徴量重要度・地点別状況は、2026-09-12に
    /accuracy(分析ページ、管理者専用ではない)へ統合した。ここにはpredict.logの
    ロールバック履歴(モデルの自動ロールバックという運用上の出来事のログ)のみ残す。"""
    rollback_lines = []
    predict_log_path = ALLOWED_LOGS.get("predict")
    if predict_log_path and predict_log_path.exists():
        try:
            with open(predict_log_path, encoding="utf-8", errors="replace") as f:
                rollback_lines = [ln.rstrip("\n") for ln in f if "Rolled back to previous model" in ln]
        except Exception as e:
            log.warning("Failed to scan predict.log for rollbacks: %s", e)

    return render_template("admin_models.html", rollback_lines=rollback_lines[-50:])


def _image_archive_day_dirs():
    """images/配下の日付フォルダを(date, Path)のリストで日付昇順に返す(不正な名前は除外)。"""
    if not IMAGE_DIR.exists():
        return []
    dirs = []
    for d in IMAGE_DIR.iterdir():
        if not d.is_dir():
            continue
        try:
            day = datetime.strptime(d.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        dirs.append((day, d))
    dirs.sort(key=lambda t: t[0])
    return dirs


@app.route("/admin/disk")
@requires_admin
def admin_disk():
    rows = []
    for label, path in DISK_USAGE_PARTS:
        size = _dir_size_bytes(path)
        rows.append({"label": label, "human": _human_size(size)})
    total, used, free = shutil.disk_usage(BASE_DIR)
    disk = {
        "total": _human_size(total), "used": _human_size(used), "free": _human_size(free),
        "used_pct": round(100.0 * used / total, 1) if total else 0,
    }
    image_days = _image_archive_day_dirs()
    image_archive = {
        "day_count": len(image_days),
        "oldest": image_days[0][0].strftime("%Y-%m-%d") if image_days else None,
        "newest": image_days[-1][0].strftime("%Y-%m-%d") if image_days else None,
        "human": _human_size(_dir_size_bytes(IMAGE_DIR)),
    }
    return render_template(
        "admin_disk.html", rows=rows, disk=disk, image_archive=image_archive,
        image_cleanup_default_days=IMAGE_CLEANUP_DEFAULT_DAYS,
    )


@app.route("/admin/disk/images_cleanup", methods=["POST"])
@requires_admin
def admin_disk_images_cleanup():
    """容量が圧迫してきた場合に、指定日数より古いカメラ画像アーカイブを手動で削除する
    (通常の自動削除は行わず無期限保存としているため、この手動操作が唯一の削除手段)。"""
    try:
        days = int(request.form.get("days", IMAGE_CLEANUP_DEFAULT_DAYS))
    except ValueError:
        days = IMAGE_CLEANUP_DEFAULT_DAYS
    days = max(1, days)
    cutoff = datetime.now(JST).date() - timedelta(days=days)
    deleted = 0
    freed_bytes = 0
    for day, day_dir in _image_archive_day_dirs():
        if day < cutoff:
            freed_bytes += _dir_size_bytes(day_dir)
            shutil.rmtree(day_dir)
            deleted += 1
    if deleted:
        _record_audit(
            "カメラ画像アーカイブ削除",
            f"{cutoff.strftime('%Y-%m-%d')}より前を{deleted}日分削除(解放: {_human_size(freed_bytes)})",
        )
        return _finish(
            "admin_disk",
            f"{cutoff.strftime('%Y-%m-%d')} より前のカメラ画像アーカイブを{deleted}日分削除しました(解放: {_human_size(freed_bytes)})。",
        )
    return _finish("admin_disk", "削除対象の画像アーカイブはありませんでした。")


@app.route("/admin/backup.zip")
@requires_admin
def admin_backup():
    """data/一式をZIPにまとめてダウンロードする（現状バックアップが無いことの補い）。
    容量が大きくなりうるため、メモリ上ではなく一時ファイルに書き出してから送る。
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    tmp_path = tmp.name
    try:
        # .envの内容(APIキー等)を含むが、このエンドポイント自体が管理者限定
        # (@requires_admin)のため許容する。backup.py(定期自動バックアップ)とも
        # 共通のロジック(common.build_data_backup_zip)を使う。
        build_data_backup_zip(tmp_path)
        stamp = datetime.now(JST).strftime("%Y%m%d_%H%M")
        response = send_file(
            tmp_path, mimetype="application/zip", as_attachment=True,
            download_name=f"river-fog-backup_{stamp}.zip",
        )
        response.call_on_close(lambda: os.path.exists(tmp_path) and os.remove(tmp_path))
        return response
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def _prune_restore_backups(pattern, keep=RESTORE_BACKUP_KEEP_GENERATIONS):
    """BASE_DIR直下のdata_before_restore_*/.env_before_restore_*のうち、直近keep件を
    残して古いものを削除する(復元を繰り返すたびに無期限に溜まっていくのを防ぐ)。
    """
    paths = sorted(BASE_DIR.glob(pattern), key=lambda p: p.name, reverse=True)
    for old in paths[keep:]:
        try:
            if old.is_dir():
                shutil.rmtree(old)
            else:
                old.unlink()
            log.info("Pruned old restore backup: %s", old.name)
        except Exception as e:
            log.warning("Failed to prune old restore backup %s: %s", old.name, e)


def _safe_extract_zip(zf, dest_dir):
    """zip-slip対策: 各エントリの展開先がdest_dir配下に収まることを、実際に展開する前に
    全件確認する（絶対パスや".."を含むエントリで想定外の場所に書き込まれるのを防ぐ）。
    また、展開後の合計サイズがRESTORE_MAX_UNCOMPRESSED_BYTESを超えないことも確認する
    (圧縮率の高いZIPでディスクを枯渇させるzip爆弾対策)。
    """
    dest_dir = dest_dir.resolve()
    total_size = 0
    for member in zf.infolist():
        member_path = (dest_dir / member.filename).resolve()
        if member_path != dest_dir and dest_dir not in member_path.parents:
            raise ValueError(f"Unsafe path in zip: {member.filename}")
        total_size += member.file_size
        if total_size > RESTORE_MAX_UNCOMPRESSED_BYTES:
            raise ValueError("展開後の合計サイズが上限を超えています")
    zf.extractall(dest_dir)


@app.route("/admin/backup/restore", methods=["POST"])
@requires_admin
def admin_backup_restore():
    """アップロードされたバックアップZIPでdata/一式(および含まれていれば.env)を丸ごと復元する。
    アカウント情報を含む全データを置き換える取り消しにくい操作のため、確認フレーズの
    一致を必須にし、復元前のdata/・.envは削除せず日時付きファイル/ディレクトリに退避しておく
    （何か問題があれば手動で戻せるように）。
    """
    confirm = request.form.get("confirm", "").strip().lower()
    if confirm != RESTORE_CONFIRM_PHRASE:
        flash(f'確認のため、入力欄に半角で "{RESTORE_CONFIRM_PHRASE}" と入力してください。', "error")
        return redirect(url_for("admin_disk"))

    file = request.files.get("backup_zip")
    if not file or not file.filename:
        flash("ZIPファイルを選択してください。", "error")
        return redirect(url_for("admin_disk"))
    if not file.filename.lower().endswith(".zip"):
        flash("ZIPファイル(.zip)を選択してください。", "error")
        return redirect(url_for("admin_disk"))

    tmp_zip = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp_zip_path = Path(tmp_zip.name)
    tmp_zip.close()
    staging_dir = None
    try:
        file.save(tmp_zip_path)
        if tmp_zip_path.stat().st_size > RESTORE_MAX_UPLOAD_BYTES:
            flash(f"ファイルが大きすぎます({RESTORE_MAX_UPLOAD_BYTES // (1024 ** 3)}GB以下にしてください)。", "error")
            return redirect(url_for("admin_disk"))

        try:
            zf = zipfile.ZipFile(tmp_zip_path)
        except zipfile.BadZipFile:
            flash("有効なZIPファイルではありません。", "error")
            return redirect(url_for("admin_disk"))

        staging_dir = Path(tempfile.mkdtemp(prefix="river-fog-restore-", dir=str(BASE_DIR)))
        with zf:
            _safe_extract_zip(zf, staging_dir)

        stamp = datetime.now(JST).strftime("%Y%m%d_%H%M%S")

        # .envはadmin_backup()がdata/の外(BASE_DIR直下)に別途含めたものなので、
        # staging_dir(このままdata/になる)の外へ退避してから個別に反映する。
        # 元の.envもdata/と同様に日時付きでバックアップしておく(何か問題があれば手動で戻せるように)。
        staged_env = staging_dir / ".env"
        if staged_env.exists():
            if ENV_PATH.exists():
                shutil.copy2(ENV_PATH, BASE_DIR / f".env_before_restore_{stamp}")
            shutil.move(str(staged_env), str(ENV_PATH))

        pre_restore_dir = BASE_DIR / f"data_before_restore_{stamp}"
        data_dir = BASE_DIR / "data"
        if data_dir.exists():
            data_dir.rename(pre_restore_dir)
        staging_dir.rename(data_dir)
        staging_dir = None
        _prune_restore_backups("data_before_restore_*")
        _prune_restore_backups(".env_before_restore_*")

        _record_audit(
            "バックアップ復元",
            f"アップロードされたZIPからdata/を復元(復元前を{pre_restore_dir.name}に退避)",
        )
        flash(
            f"data/一式を復元しました。復元前のデータは {pre_restore_dir.name} "
            "に退避してあります(問題なければ後で手動削除してください)。サービスを再起動します。",
            "success",
        )
        subprocess.Popen(
            ["/bin/sh", "-c", "sleep 1 && sudo -n systemctl restart river-fog-web.service"],
            start_new_session=True,
        )
    except Exception:
        log.exception("Backup restore failed")
        flash("復元に失敗しました。詳細はログを確認してください。", "error")
    finally:
        if tmp_zip_path.exists():
            tmp_zip_path.unlink()
        if staging_dir is not None and staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
    return redirect(url_for("admin_disk"))


@app.route("/admin/db")
@requires_admin
def admin_db():
    stats = None
    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            try:
                stats = prediction_log.db_stats(conn)
            finally:
                conn.close()
        except Exception as e:
            log.warning("Failed to read predictions.db stats: %s", e)
    return render_template("admin_db.html", stats=stats, default_days=DB_CLEANUP_DEFAULT_DAYS)


@app.route("/admin/db/cleanup", methods=["POST"])
@requires_admin
def admin_db_cleanup():
    try:
        days = int(request.form.get("days", DB_CLEANUP_DEFAULT_DAYS))
    except ValueError:
        days = DB_CLEANUP_DEFAULT_DAYS
    days = max(1, days)
    before_dt = (now_jst_naive() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            deleted = prediction_log.delete_verified_before(conn, before_dt)
        finally:
            conn.close()
        _record_audit("予測DBクリーンアップ", f"{before_dt}より前の検証済みレコードを{deleted}件削除")
        return _finish("admin_db", f"{before_dt} より前の答え合わせ済みレコードを{deleted}件削除しました。")
    except Exception:
        log.exception("DB cleanup failed")
        return _finish("admin_db", "クリーンアップに失敗しました。詳細はログを確認してください。", success=False)


@app.route("/admin/camera_health")
@requires_admin
def admin_camera_health():
    rows = _camera_uptime_stats()
    return render_template("admin_camera_health.html", rows=rows, days=CAMERA_HEALTH_DAYS)


@app.route("/admin/env")
@requires_admin
def admin_env():
    return render_template("admin_env.html", env_status=_env_status())


@app.route("/admin/settings", methods=["GET", "POST"])
@requires_admin
def admin_settings():
    if request.method == "POST":
        updates = {key: request.form.get(key, "").strip() for key in EDITABLE_ENV_KEYS}
        try:
            _write_editable_env_values(updates)
            flash("設定を保存しました。次回のcollect.py/predict.py実行から反映されます。", "success")
        except Exception:
            log.exception("Failed to write .env")
            flash("設定の保存に失敗しました。詳細はログを確認してください。", "error")
        return redirect(url_for("admin_settings"))
    return render_template("admin_settings.html", values=_read_editable_env_values())


@app.route("/admin/accuracy_report")
@requires_admin
def admin_accuracy_report():
    summary = _accuracy_summary_safe()
    by_station = []
    if summary:
        by_station = sorted(
            summary["by_station"],
            key=lambda r: (r["n"] == 0, r["correct"] / r["n"] if r["n"] else 1.0),
        )
    return render_template("admin_accuracy_report.html", by_station=by_station, overall=summary["overall"] if summary else None)


def _generate_report_pdf(start_date, end_date):
    """指定期間の川霧発生状況・気象概況・予測精度をまとめたPDFレポートを生成し、bytesで返す。
    matplotlibのPdfPages機能を使う(新規ライブラリの追加は不要)。
    """
    from matplotlib.backends.backend_pdf import PdfPages
    from matplotlib.ticker import MaxNLocator

    main_df, phenom_df, station_names = fog_data.load_river_fog_data()
    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt = datetime.combine(end_date, datetime.max.time())
    mask = (phenom_df["datetime"] >= start_dt) & (phenom_df["datetime"] <= end_dt)
    period_df = phenom_df[mask]
    period_weather_df = main_df[(main_df["datetime"] >= start_dt) & (main_df["datetime"] <= end_dt)]

    # 地点ごとのコード系列(0=現象なし、1〜10=PHENOM_LABELS、2コード同時観測=2026-09対応は
    # パック済み値)をまとめて計算しておき、川霧発生回数の集計・現象内訳の集計の両方で使い回す。
    codes_by_station = {}
    for name in station_names:
        if name in period_df.columns:
            codes_by_station[name] = period_df[name].map(encode_phenomena_cell)

    # 川霧発生回数は主コードだけで判定できる(家族グループの並び上、2コード同時でも
    # 川霧は必ず主コード側に入る)。
    fog_counts = {
        name: int(codes.apply(primary_phenom_code).isin(SUMMARY_FOG_CODES).sum())
        for name, codes in codes_by_station.items()
    }
    for name in station_names:
        fog_counts.setdefault(name, 0)
    total_slots = len(period_df)

    tadami_stations = [n for n in station_names if STATION_REGION.get(n) == "只見川"]
    aizu_stations = [n for n in station_names if STATION_REGION.get(n) == "会津"]

    # 現象内訳は主・副どちらのコードもそれぞれ1件としてカウントする(2コード同時観測の
    # 場合、value_counts()だとパック済み値が独立した1つの値として扱われ、内訳の
    # どちらのコードにも計上されなくなってしまうため、phenom_codes_setで展開してから数える)。
    phenom_code_totals = {code: 0 for code in PHENOM_LABELS}
    for codes in codes_by_station.values():
        for value in codes:
            for code in phenom_codes_set(value):
                if code in phenom_code_totals:
                    phenom_code_totals[code] += 1

    trend = _accuracy_trend_safe()
    trend_in_range = [r for r in trend if start_date.isoformat() <= r["date"] <= end_date.isoformat() and r["n"] > 0]
    # レポートのタイトルには「その期間の」的中率を出したいため、全期間集計のoverallではなく
    # 期間内の日別集計(trend_in_range)を合算する(以前はaccuracy["overall"]=全期間累計の値を
    # 誤って「この期間の」として表示してしまっていた)。
    period_correct = sum(r["correct"] for r in trend_in_range)
    period_n = sum(r["n"] for r in trend_in_range)

    def _weather_avg(col):
        if col not in period_weather_df.columns:
            return None
        series = period_weather_df[col].dropna()
        return float(series.mean()) if len(series) else None

    def _weather_sum(col):
        if col not in period_weather_df.columns:
            return None
        series = period_weather_df[col].dropna()
        return float(series.sum()) if len(series) else None

    buf = io.BytesIO()
    with PdfPages(buf) as pdf:
        fig = plt.figure(figsize=(8.27, 11.69))
        fig.text(0.5, 0.62, "川霧観測・予測レポート", ha="center", fontsize=24, fontweight="bold")
        fig.text(0.5, 0.55, f"{start_date} 〜 {end_date}", ha="center", fontsize=14)
        if period_n:
            fig.text(
                0.5, 0.48,
                f"この期間の予測的中率: {100.0 * period_correct / period_n:.1f}% "
                f"({period_correct}/{period_n}件)",
                ha="center", fontsize=11,
            )
        fig.text(0.5, 0.08, f"生成日時: {now_jst_naive().strftime('%Y-%m-%d %H:%M')}", ha="center", fontsize=9, color="gray")
        pdf.savefig(fig)
        plt.close(fig)

        # 地域別サマリー(観測コマ数・川霧発生回数・発生率を只見川/会津それぞれで比較できるように)。
        fig = plt.figure(figsize=(8.27, 6))
        fig.text(0.5, 0.92, "地域別サマリー", ha="center", fontsize=16, fontweight="bold")
        y = 0.75
        for label, names in (("只見川", tadami_stations), ("会津", aizu_stations)):
            region_slots = total_slots * len(names)
            region_fog = sum(fog_counts.get(n, 0) for n in names)
            rate = (100.0 * region_fog / region_slots) if region_slots else 0.0
            fig.text(
                0.5, y,
                f"【{label}】地点数 {len(names)} / 川霧発生 {region_fog}コマ"
                f"(全{region_slots}コマ中、発生率 {rate:.1f}%)",
                ha="center", fontsize=12,
            )
            y -= 0.12
        pdf.savefig(fig)
        plt.close(fig)

        def _station_fog_chart(title, names):
            if not total_slots or not names:
                fig = plt.figure(figsize=(8.27, 4))
                fig.text(0.5, 0.5, "この期間の観測データがありません。", ha="center", fontsize=12)
                pdf.savefig(fig)
                plt.close(fig)
                return
            sorted_counts = sorted(((n, fog_counts.get(n, 0)) for n in names), key=lambda kv: kv[1])
            names_sorted = [n for n, _ in sorted_counts]
            counts = [c for _, c in sorted_counts]
            fig, ax = plt.subplots(figsize=(8.27, 11.69))
            ax.barh(names_sorted, counts, color="#1a5276")
            ax.set_xlabel(f"川霧発生回数(観測コマ数、全{total_slots}コマ中)")
            ax.set_title(title)
            ax.tick_params(axis="y", labelsize=7)
            ax.xaxis.set_major_locator(MaxNLocator(integer=True))
            fig.tight_layout()
            pdf.savefig(fig)
            plt.close(fig)

        _station_fog_chart("只見川 地点別 川霧発生回数", tadami_stations)
        _station_fog_chart("会津 地点別 川霧発生回数", aizu_stations)

        # 気象概況(代表地点の実況値の期間平均・合計)。
        fig = plt.figure(figsize=(8.27, 6))
        fig.text(0.5, 0.92, "期間の気象概況(代表地点)", ha="center", fontsize=16, fontweight="bold")
        avg_temp = _weather_avg(COL_TEMP)
        total_precip = _weather_sum(COL_PRECIP)
        avg_humidity = _weather_avg(COL_HUMIDITY)
        avg_wind = _weather_avg(COL_WIND_SPEED)
        total_sunshine = _weather_sum(COL_SUNSHINE)
        if avg_temp is None:
            fig.text(0.5, 0.6, "この期間の気象データがありません。", ha="center", fontsize=12)
        else:
            rows = [
                f"平均気温: {avg_temp:.1f}℃",
                f"降水量合計: {total_precip:.1f}mm" if total_precip is not None else "降水量合計: -",
                f"平均相対湿度: {avg_humidity:.0f}%" if avg_humidity is not None else "平均相対湿度: -",
                f"平均風速: {avg_wind:.1f}m/s" if avg_wind is not None else "平均風速: -",
                f"日照時間合計: {total_sunshine:.1f}時間" if total_sunshine is not None else "日照時間合計: -",
            ]
            y = 0.75
            for row in rows:
                fig.text(0.5, y, row, ha="center", fontsize=12)
                y -= 0.1
        pdf.savefig(fig)
        plt.close(fig)

        # 現象内訳(全地点合算、川霧に限らず層雲・霧雨・雨まで含めた内訳)。
        if total_slots and any(phenom_code_totals.values()):
            sorted_items = sorted(phenom_code_totals.items(), key=lambda kv: kv[1])
            labels = [PHENOM_LABELS[c] for c, _ in sorted_items]
            counts = [c for _, c in sorted_items]
            colors = [PHENOM_COLORS.get(code, "#1a5276") for code, _ in sorted_items]
            fig, ax = plt.subplots(figsize=(8.27, 6))
            ax.barh(labels, counts, color=colors)
            ax.set_xlabel("発生回数(全地点合算・観測コマ数)")
            ax.set_title("現象別 発生内訳(全地点合算)")
            fig.tight_layout()
            pdf.savefig(fig)
            plt.close(fig)

        if trend_in_range:
            dates = [datetime.strptime(r["date"], "%Y-%m-%d") for r in trend_in_range]
            rates = [100.0 * r["correct"] / r["n"] for r in trend_in_range]
            fig, ax = plt.subplots(figsize=(8.27, 5.5))
            ax.plot(dates, rates, marker="o", markersize=3, color="#1a5276")
            ax.set_title("予測的中率の推移")
            ax.set_ylabel("的中率(%)")
            ax.set_ylim(0, 100)
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
            fig.autofmt_xdate()
            fig.tight_layout()
            pdf.savefig(fig)
            plt.close(fig)

    return buf.getvalue()


@app.route("/report", methods=["GET", "POST"])
def period_report():
    """期間レポート(PDF)。管理者専用ではなく、地点の分析ページ群と並ぶ一般公開の機能
    (元々/admin/reportだったが、内容が観測・予測の分析であり管理機能ではないため移動した)。"""
    today = now_jst_naive().date()
    default_start = today - timedelta(days=30)
    if request.method == "POST":
        try:
            start_date = datetime.strptime(request.form.get("start_date", ""), "%Y-%m-%d").date()
            end_date = datetime.strptime(request.form.get("end_date", ""), "%Y-%m-%d").date()
        except ValueError:
            flash("開始日・終了日を正しく指定してください。", "error")
            return redirect(url_for("period_report"))
        if start_date > end_date:
            flash("開始日は終了日より前の日付にしてください。", "error")
            return redirect(url_for("period_report"))
        if _report_rate_limited(session.get("user_id")):
            flash(f"短時間に生成しすぎです。{REPORT_RATE_LIMIT_WINDOW.seconds // 60}分ほど待ってからお試しください。", "error")
            return redirect(url_for("period_report"))
        try:
            pdf_bytes = _generate_report_pdf(start_date, end_date)
        except Exception:
            log.exception("Report generation failed")
            flash("レポートの生成に失敗しました。詳細はログを確認してください。", "error")
            return redirect(url_for("period_report"))
        filename = f"river-fog-report_{start_date}_{end_date}.pdf"
        return send_file(
            io.BytesIO(pdf_bytes), mimetype="application/pdf",
            as_attachment=True, download_name=filename,
        )
    return render_template("report.html", default_start=default_start, default_end=today)


@app.route("/admin/gemini")
@requires_admin
def admin_gemini():
    conn = chat_log.connect(CHAT_DB_PATH)
    try:
        chat_rows = {r["date"]: r["count"] for r in chat_log.daily_message_counts(conn, days=GEMINI_LOG_DAYS)}
    finally:
        conn.close()
    rows = _gemini_log_stats()
    for r in rows:
        r["chat_calls"] = chat_rows.get(r["date"], 0)
    return render_template(
        "admin_gemini.html", rows=rows,
        chat_daily_limit_per_user=CHAT_DAILY_LIMIT_PER_USER, chat_daily_limit_total=CHAT_DAILY_LIMIT_TOTAL,
    )


@app.route("/admin/maintenance", methods=["GET", "POST"])
@requires_admin
def admin_maintenance():
    if request.method == "POST":
        enabled = request.form.get("enabled") == "on"
        message = request.form.get("message", "").strip()
        _set_maintenance_state(enabled, message)
        return _finish(
            "admin_maintenance",
            "メンテナンスモードを有効にしました。" if enabled else "メンテナンスモードを解除しました。",
        )
    return render_template("admin_maintenance.html", state=_maintenance_state())


@app.route("/admin/announcements")
@requires_admin
def admin_announcements():
    conn = _announcements_conn()
    try:
        items = announcements.list_announcements(conn)
        for item in items:
            item["response_count"] = announcements.response_count(conn, item["id"])
    finally:
        conn.close()
    return render_template("admin_announcements.html", items=items)


@app.route("/admin/announcements/new", methods=["POST"])
@requires_admin
def admin_announcements_new():
    kind = request.form.get("kind", "message")
    message = request.form.get("message", "").strip()
    start_at = request.form.get("start_at", "").strip() or None
    end_at = request.form.get("end_at", "").strip() or None
    choices = None
    if kind == "choice":
        choices = [c.strip() for c in request.form.get("choices", "").splitlines() if c.strip()]
        if len(choices) < 2:
            flash("選択式アンケートには選択肢を2つ以上入力してください。", "error")
            return redirect(url_for("admin_announcements"))
    if not message:
        flash("本文/質問文を入力してください。", "error")
        return redirect(url_for("admin_announcements"))

    created_at = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
    conn = _announcements_conn()
    try:
        announcements.create_announcement(conn, kind, message, choices, start_at, end_at, created_at)
    finally:
        conn.close()
    flash("お知らせ/アンケートを作成しました。", "success")
    return redirect(url_for("admin_announcements"))


@app.route("/admin/announcements/<int:announcement_id>/toggle", methods=["POST"])
@requires_admin
def admin_announcements_toggle(announcement_id):
    conn = _announcements_conn()
    try:
        ann = announcements.get_announcement(conn, announcement_id)
        if ann is None:
            abort(404)
        announcements.set_active(conn, announcement_id, not ann["active"])
    finally:
        conn.close()
    return redirect(url_for("admin_announcements"))


@app.route("/admin/announcements/<int:announcement_id>/delete", methods=["POST"])
@requires_admin
def admin_announcements_delete(announcement_id):
    conn = _announcements_conn()
    try:
        announcements.delete_announcement(conn, announcement_id)
    finally:
        conn.close()
    flash("削除しました。", "success")
    return redirect(url_for("admin_announcements"))


@app.route("/admin/announcements/<int:announcement_id>/results")
@requires_admin
def admin_announcements_results(announcement_id):
    conn = _announcements_conn()
    try:
        ann = announcements.get_announcement(conn, announcement_id)
        if ann is None:
            abort(404)
        summary = announcements.response_summary(conn, announcement_id)
        responses = announcements.list_responses(conn, announcement_id)
    finally:
        conn.close()
    users_conn = _users_conn()
    try:
        names_by_id = users.get_users_by_ids(users_conn, {r["user_id"] for r in responses if r["user_id"]})
    finally:
        users_conn.close()
    for r in responses:
        u = names_by_id.get(r["user_id"])
        r["respondent_name"] = u["name"] if u else "(退会済みユーザー)" if r["user_id"] else "(不明)"
        r["respondent_exists"] = u is not None
        if ann["kind"] == "choice" and r["choice_index"] is not None:
            choices = ann["choices"] or []
            r["choice_label"] = choices[r["choice_index"]] if r["choice_index"] < len(choices) else f"選択肢{r['choice_index']}"
    return render_template(
        "admin_announcement_results.html", ann=ann, summary=summary, responses=responses,
    )


@app.route("/announcements/<int:announcement_id>/respond", methods=["POST"])
def respond_announcement(announcement_id):
    my_id = session.get("user_id")
    if not my_id:
        abort(403)
    conn = _announcements_conn()
    try:
        ann = announcements.get_announcement(conn, announcement_id)
        if ann is None or not ann["active"]:
            abort(404)
        choice_index = request.form.get("choice_index")
        answer_text = request.form.get("answer_text", "").strip() or None
        submitted_at = now_jst_naive().strftime("%Y-%m-%d %H:%M:%S")
        announcements.record_response(
            conn, announcement_id, my_id,
            int(choice_index) if choice_index not in (None, "") else None,
            answer_text, submitted_at,
        )
    finally:
        conn.close()
    message = "回答ありがとうございました。"
    if _wants_json():
        return jsonify({"message": message, "success": True})
    flash(message, "success")
    return redirect(request.referrer or url_for("dashboard"))


# 管理者タブ「仮検証(二値モデル)」。本番の予測モデルとは別系統の検証結果を表示する
# (experiment_model.py / experiment_view.py)。読み込みに失敗しても他の画面に影響しないよう、
# 失敗時はログに残してこのタブだけ無効にする。
try:
    import experiment_view
    experiment_view.register(app, requires_admin, _cached_chart_json, _is_script_running)
except Exception:
    log.exception("Failed to register the experiment (仮検証) admin tab")
