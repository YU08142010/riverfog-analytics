"""ページが開けるか・見てよい人だけが見られるか(データが空の状態でも500にならないこと)。"""
import unittest

from tests import support


class PublicPagesTest(unittest.TestCase):
    def setUp(self):
        self.c = support.client()

    def test_public_pages_open_without_login(self):
        for path in ("/login", "/signup", "/forecast"):
            with self.subTest(path=path):
                self.assertEqual(self.c.get(path).status_code, 200)

    def test_top_page_sends_visitors_to_public_forecast(self):
        r = self.c.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/forecast"))

    def test_member_pages_require_login(self):
        for path in ("/chat", "/settings", "/api/spotlight-items"):
            with self.subTest(path=path):
                r = self.c.get(path)
                self.assertEqual(r.status_code, 302)
                self.assertIn("/login", r.headers["Location"])


class CrawlTest(unittest.TestCase):
    """引数のない GET のページを全部開き、サーバーエラー(5xx)が出ないことを確かめる。"""

    def crawl(self, role, headers):
        c = support.client(role)
        failures = []
        for path in support.simple_get_rules():
            try:
                with c.get(path, headers=headers) as r:
                    code = r.status_code
            except Exception as e:  # noqa: BLE001 — 例外も失敗として一覧に出す
                code = f"{type(e).__name__}: {e}"
            if not isinstance(code, int) or code >= 500:
                failures.append((path, code))
        self.assertEqual(failures, [], f"{role} で開けなかったページ")

    def test_admin_can_open_every_page(self):
        self.crawl("admin", support.INTERNAL)

    def test_member_can_open_every_page(self):
        self.crawl("member", support.INTERNAL)


class AccessControlTest(unittest.TestCase):
    def admin_paths(self):
        return [p for p in support.simple_get_rules() if p.startswith("/admin")]

    def test_members_cannot_open_admin_pages(self):
        c = support.client("member")
        opened = [p for p in self.admin_paths() if c.get(p, headers=support.INTERNAL).status_code == 200]
        self.assertEqual(opened, [])

    def test_admin_pages_are_blocked_from_the_public_url(self):
        # 公開URL(Funnel)からの通信には nginx の合言葉ヘッダーも Tailscale の身元ヘッダーも付かない
        c = support.client("admin")
        opened = [p for p in self.admin_paths() if c.get(p).status_code == 200]
        self.assertEqual(opened, [])

    def test_admin_pages_exist(self):
        self.assertGreater(len(self.admin_paths()), 5)


if __name__ == "__main__":
    unittest.main()
