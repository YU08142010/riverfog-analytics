"""セキュリティの決まりごと(応答ヘッダー・CSRF・キャッシュ・ファイルの取り出し)。"""
import re
import unittest

from tests import support


class SecurityHeadersTest(unittest.TestCase):
    def test_headers_on_pages(self):
        r = support.client().get("/login")
        h = r.headers
        self.assertEqual(h.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(h.get("X-Frame-Options"), "SAMEORIGIN")
        self.assertIn("default-src", h.get("Content-Security-Policy", ""))
        self.assertTrue(h.get("Permissions-Policy"))
        self.assertEqual(h.get("Cross-Origin-Opener-Policy"), "same-origin")
        self.assertTrue(h.get("Referrer-Policy"))

    def test_server_does_not_announce_itself(self):
        self.assertNotIn("Werkzeug", support.client().get("/login").headers.get("Server", ""))

    def test_login_forms_are_not_cached(self):
        # 戻るボタン・キャッシュから古いフォームを出すと CSRF の合言葉が合わなくなる(2026-10-14 の不具合)
        for path in ("/login", "/signup"):
            with self.subTest(path=path):
                self.assertIn("no-store", support.client().get(path).headers.get("Cache-Control", ""))


class CsrfTest(unittest.TestCase):
    CREDENTIALS = {"email": "zz-test-member@example.invalid", "password": "unused-" + "member" * 3}

    def login(self, c, with_token, next_path=None):
        support.make_user("member")
        page = c.get("/login").get_data(as_text=True)
        data = dict(self.CREDENTIALS)
        if with_token:
            data["csrf_token"] = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        url = "/login" + (f"?next={next_path}" if next_path else "")
        return c.post(url, data=data, headers={"Referer": "http://localhost/login"})

    def logged_in(self, c):
        with c.session_transaction() as s:
            return "user_id" in s

    def test_login_with_token_works(self):
        # 下の「合言葉なしでは入れない」が、パスワード違いなど別の理由で通っていないことの確認
        c = support.client()
        self.login(c, with_token=True)
        self.assertTrue(self.logged_in(c))

    def test_login_without_token_does_not_log_in(self):
        c = support.client()
        r = self.login(c, with_token=False)
        self.assertEqual(r.status_code, 302)
        self.assertFalse(self.logged_in(c))

    def test_ajax_post_without_token_gets_400(self):
        r = support.client("member").post("/login", data=self.CREDENTIALS, headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["success"])

    def test_login_does_not_redirect_to_other_sites(self):
        for evil in ("//evil.example", "/\\evil.example", "https://evil.example/"):
            with self.subTest(next=evil):
                r = self.login(support.client(), with_token=True, next_path=evil)
                self.assertNotIn("evil.example", r.headers.get("Location", ""))

    def test_clear_session_keeps_csrf_token(self):
        with support.webapp.app.test_request_context("/"):
            support.webapp.session["csrf_token"] = "tok"
            support.webapp.session["user_id"] = 1
            support.webapp._clear_session_keep_csrf()
            self.assertEqual(dict(support.webapp.session), {"csrf_token": "tok"})


class FileServingTest(unittest.TestCase):
    def test_path_traversal_is_refused(self):
        c = support.client("admin")
        for path in ("/images/2026-10-01/0600/thumb/..%2f..%2f..%2fusers.db",
                     "/images/2026-10-01/0600/thumb/../../../../.env",
                     "/web_static/..%2f.env"):
            with self.subTest(path=path):
                r = c.get(path, headers=support.INTERNAL)
                self.assertNotEqual(r.status_code, 200)
                self.assertNotIn(b"SECRET_KEY", r.data)


if __name__ == "__main__":
    unittest.main()
