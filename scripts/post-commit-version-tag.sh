#!/bin/sh
# 直前のコミットに、そのコミットで確定したVERSIONの値をもとに軽量タグ(v<VERSION>)を
# 付ける。以前はVERSIONファイルの中身を見ないと「どのバージョンがどのコミットか」を
# 追えなかったが、`git tag`/`git log --tags`から直接たどれるようにするための対応。
#
# pre-commit-version-bump.sh と同様、.git/hooks/ 配下には置けないため、
# リポジトリ側に保存しておき、以下でコピーして使う:
#   cp scripts/post-commit-version-tag.sh .git/hooks/post-commit
#   chmod +x .git/hooks/post-commit
#
# 既に同名のタグが存在する場合(手動でのやり直しコミット等)は失敗を無視して続行する
# (post-commitフックの失敗はコミット自体を巻き戻さないが、余計なエラー出力を避けるため)。

VERSION_FILE="VERSION"

if [ ! -f "$VERSION_FILE" ]; then
    exit 0
fi

version=$(cat "$VERSION_FILE")
git tag "v${version}" 2>/dev/null || true
