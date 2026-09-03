#!/bin/sh
# コミットごとにVERSIONファイルのマイナー番号(x.yのy)を自動で+1し、そのコミットに含める。
# メジャー番号(x)を上げたい場合は、このフックが動く前にVERSIONを手動で書き換えて
# `git add VERSION` しておけば、このフックはそれを尊重して自動インクリメントをスキップする。
#
# あわせて、web_static/style.cssがこのコミットに含まれる場合は配信用の
# web_static/style.min.cssを再生成してコミットに含める(scripts/minify_css.py参照)。
# 原本(style.css)は常にそのまま残し、base.htmlが読み込むのはstyle.min.cssの方だけ
# にすることで、「配信は圧縮版・ソースは原本のまま」を両立している。
#
# このファイルは .git/hooks/ 配下には置けない(gitの標準仕様でコミット対象外のため)、
# リポジトリ側にも同じ内容を保存しておき、.git/hooks/pre-commit にコピーして使う。
#   cp scripts/pre-commit-version-bump.sh .git/hooks/pre-commit
#   chmod +x .git/hooks/pre-commit
#
# あわせて scripts/post-commit-version-tag.sh も .git/hooks/post-commit に導入すると、
# コミットのたびに v<VERSION> の軽量タグが自動で付き、`git tag` から直接
# バージョンとコミットの対応を追えるようになる(任意だが推奨)。

if git diff --cached --name-only | grep -qx "web_static/style.css"; then
    python3 scripts/minify_css.py web_static/style.css web_static/style.min.css
    git add web_static/style.min.css
fi

VERSION_FILE="VERSION"

if git diff --cached --name-only | grep -qx "$VERSION_FILE"; then
    # VERSIONが既にこのコミットでステージ済み(メジャーバージョンの手動指定など)なら、
    # 自動インクリメントはせずそのまま使う
    exit 0
fi

if [ -f "$VERSION_FILE" ]; then
    current=$(cat "$VERSION_FILE")
    major=$(echo "$current" | cut -d. -f1)
    minor=$(echo "$current" | cut -d. -f2)
    minor=$((minor + 1))
else
    major=1
    minor=0
fi

echo "${major}.${minor}" > "$VERSION_FILE"
git add "$VERSION_FILE"
