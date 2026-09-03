"""style.cssから配信用の圧縮版style.min.cssを生成する。

style.css自体(可読な原本)はそのまま残し、base.htmlが読み込む先だけをstyle.min.cssに
切り替える。ビルドツール(webpack等)を導入するほどの規模ではないため、外部依存無しの
単純な正規表現ベースの圧縮にとどめる(コメント除去+空白の圧縮。CSSカスタムプロパティの
文字列値等、めったに崩れない範囲のみを対象にした保守的な変換)。
"""
import re
import sys


def minify_css(src):
    # /* ... */ コメントを除去(複数行にまたがるものも対象)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    # 改行・連続する空白を1個の半角スペースに圧縮
    src = re.sub(r"\s+", " ", src)
    # 記号の前後の不要な空白を除去(: ; { } , の前後)
    src = re.sub(r"\s*([:{};,])\s*", r"\1", src)
    # 最後のセミコロンの直前に}が来るケースの余分な;を除去して微減(無くても動作は変わらない)
    src = re.sub(r";}", "}", src)
    return src.strip()


def main():
    src_path, dest_path = sys.argv[1], sys.argv[2]
    with open(src_path, encoding="utf-8") as f:
        original = f.read()
    minified = minify_css(original)
    with open(dest_path, "w", encoding="utf-8") as f:
        f.write(minified)
    print(f"{src_path}: {len(original)} bytes -> {dest_path}: {len(minified)} bytes "
          f"({100.0 * len(minified) / len(original):.1f}%)")


if __name__ == "__main__":
    main()
