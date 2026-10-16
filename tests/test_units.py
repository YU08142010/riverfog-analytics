"""小さな関数の計算・変換が正しいか。"""
import gzip
import unittest

from tests import support

webapp = support.webapp


class MinifyCssTest(unittest.TestCase):
    def test_removes_comments_and_spaces(self):
        css = "/* note */\n.a  {\n  color : red;\n  margin: 0 auto;\n}\n"
        self.assertEqual(webapp._minify_css(css), ".a{color :red;margin:0 auto}")

    def test_keeps_space_before_pseudo_class(self):
        # `.a :hover` と `.a:hover` は意味が違うので、「:」の前の空白は残す
        self.assertIn(".a :hover", webapp._minify_css(".a :hover { color: red; }"))


class CompressionTest(unittest.TestCase):
    def test_css_is_minified_and_gzipped_for_direct_requests(self):
        c = support.client()
        with c.get("/web_static/glass.css") as plain, \
                c.get("/web_static/glass.css", headers={"Accept-Encoding": "gzip"}) as zipped:
            self.assertEqual(zipped.headers.get("Content-Encoding"), "gzip")
            self.assertEqual(gzip.decompress(zipped.data), plain.data)
            self.assertNotIn(b"/*", plain.data)

    def test_nginx_requests_are_left_for_nginx_to_compress(self):
        with support.client().get("/web_static/glass.css", headers={"Accept-Encoding": "gzip", **support.INTERNAL}) as r:
            self.assertIsNone(r.headers.get("Content-Encoding"))


class HistogramBinsTest(unittest.TestCase):
    def test_fixed_width_bins_cover_all_values(self):
        labels, centers, (a, b) = webapp._river_fog_histogram_bins(5, [0, 4.9, 5, 12], [7])
        self.assertEqual(labels, ["0〜5", "5〜10", "10〜15"])
        self.assertEqual(centers, [2.5, 7.5, 12.5])
        self.assertEqual(a, [2, 1, 1])
        self.assertEqual(b, [0, 1, 0])

    def test_uneven_bins_use_open_ends(self):
        labels, centers, (a,) = webapp._river_fog_histogram_bins([1, 3], [0.5, 1, 2.9, 3, 10])
        self.assertEqual(labels, ["〜1", "1〜3", "3〜"])
        self.assertIsNone(centers)
        self.assertEqual(a, [1, 2, 2])

    def test_bin_labels(self):
        self.assertEqual(webapp._fmt_bin(2.0), "2")
        self.assertEqual(webapp._fmt_bin(2.5), "2.5")
        self.assertEqual(webapp._fmt_bin(-0.5), "-0.5")


class UserSettingsTest(unittest.TestCase):
    def test_normalize_falls_back_to_defaults(self):
        defaults = webapp.user_settings.defaults()
        self.assertEqual(webapp.user_settings.normalize(None), defaults)
        self.assertEqual(webapp.user_settings.normalize("{not json"), defaults)


class TemplatesTest(unittest.TestCase):
    def test_every_template_compiles(self):
        env = webapp.app.jinja_env
        names = env.list_templates()
        self.assertGreater(len(names), 50)
        for name in names:
            with self.subTest(template=name):
                env.get_template(name)


if __name__ == "__main__":
    unittest.main()
