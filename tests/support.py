"""テスト共通の準備。webapp を読み込み、data/ 以下のDBの置き場所をすべて一時フォルダに差し替える。

本番の /opt/river-fog で実行しても、本物の users.db などには書き込まない(差し替え漏れがあれば止める)。
pytest は使わず標準の unittest で書く(本番の依存を増やさないため)。
実行: python -m unittest discover -s tests -v
"""
import atexit
import inspect
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# webapp は読み込み時に logs/webapp.log を開く。新しく取得したリポジトリには無いので作っておく
(ROOT / "logs").mkdir(exist_ok=True)
(ROOT / "data").mkdir(exist_ok=True)

import logging  # noqa: E402

import webapp  # noqa: E402
import users  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

logging.disable(logging.WARNING)
webapp.app.config["TESTING"] = True

TMP = Path(tempfile.mkdtemp(prefix="rf-test-"))
atexit.register(shutil.rmtree, TMP, ignore_errors=True)
_DATA = ROOT / "data"


def _moved(value):
    if isinstance(value, Path) and str(value).startswith(str(_DATA)):
        return TMP / value.relative_to(_DATA)
    return value


def _redirect_data_paths():
    """プロジェクト内の各モジュールの「data/ 以下を指す大文字の定数(Path)」と、関数の引数の既定値
    (def f(db_path=WEATHER_DB_PATH) のように定義の時点で決まっているもの)を一時フォルダに向け直す。"""
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None) or ""
        if not f.startswith(str(ROOT)) or "/tests/" in f.replace("\\", "/"):
            continue
        for name, value in list(vars(mod).items()):
            if name.isupper():
                setattr(mod, name, _moved(value))
            elif inspect.isfunction(value) and value.__module__ == mod.__name__:
                if value.__defaults__:
                    value.__defaults__ = tuple(_moved(v) for v in value.__defaults__)
                if value.__kwdefaults__:
                    value.__kwdefaults__ = {k: _moved(v) for k, v in value.__kwdefaults__.items()}


_redirect_data_paths()
if str(webapp.USERS_DB_PATH).startswith(str(_DATA)):
    raise RuntimeError("users.db の置き場所を一時フォルダに差し替えられなかったため、テストを中止します")

INTERNAL = {"X-Rf-Internal": webapp._NGINX_INTERNAL_HEADER_SECRET}
_users = {}


def make_user(role):
    """役割(admin / member)ごとにテスト用の利用者を1人作る(一時DBの中だけ)。"""
    if role not in _users:
        email = f"zz-test-{role}@example.invalid"
        conn = users.connect(webapp.USERS_DB_PATH)
        try:
            users.create_user(conn, f"zz-test-{role}", email, generate_password_hash("unused-" + role * 3), role,
                              "2026-10-16 00:00:00")
            _users[role] = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()[0]
        finally:
            conn.close()
    return _users[role]


def client(role=None):
    """テスト用のブラウザ。role を渡すと、その役割でログインした状態にする。"""
    c = webapp.app.test_client()
    if role:
        uid = make_user(role)
        with c.session_transaction() as s:
            s.update({"user_id": uid, "role": role, "name": f"zz-test-{role}", "_permanent": True})
    return c


def simple_get_rules():
    """引数のない GET のページ(ログアウト・端末・長く続く通信などの副作用・待ちがあるものは除く)。"""
    skip = ("logout", "terminal", "stream", "events", "backup", "export", ".zip", "download", "/sse")
    for rule in webapp.app.url_map.iter_rules():
        if "GET" not in rule.methods or rule.arguments or rule.endpoint == "static":
            continue
        if any(s in rule.rule for s in skip):
            continue
        yield rule.rule
