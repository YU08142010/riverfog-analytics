#!/bin/sh
# コミットごとにVERSIONファイルのマイナー番号(x.yのy)を自動で+1し、そのコミットに含める。
# メジャー番号(x)を上げたい場合は、このフックが動く前にVERSIONを手動で書き換えて
# `git add VERSION` しておけば、このフックはそれを尊重して自動インクリメントをスキップする。
#
# このファイルは .git/hooks/ 配下には置けない(gitの標準仕様でコミット対象外のため)、
# リポジトリ側にも同じ内容を保存しておき、.git/hooks/pre-commit にコピーして使う。
#   cp scripts/pre-commit-version-bump.sh .git/hooks/pre-commit
#   chmod +x .git/hooks/pre-commit

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
