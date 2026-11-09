#!/usr/bin/env bash
# Cut a release: bump the version, commit it on master, tag it and push.
#
#   scripts/release.sh 0.2.0          # asks before pushing
#   scripts/release.sh 0.2.0 -y       # no prompt
#
# The tag is what publishes: .github/workflows/release.yml builds, tests,
# uploads to PyPI and opens the GitHub Release. Nothing is published if the
# tests fail here.
set -euo pipefail

usage() { echo "usage: $(basename "$0") X.Y.Z [-y]" >&2; exit 2; }

version="" ; assume_yes=0
for arg in "$@"; do
  case "$arg" in
    -y|--yes) assume_yes=1 ;;
    -h|--help) usage ;;
    -*) echo "unknown option: $arg" >&2; usage ;;
    *) [ -z "$version" ] || usage ; version="$arg" ;;
  esac
done
[ -n "$version" ] || usage
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "$version is not X.Y.Z" >&2; exit 2; }

cd "$(git rev-parse --show-toplevel)"
branch=$(git rev-parse --abbrev-ref HEAD)
[ "$branch" = "master" ] || { echo "releases come from master, you are on $branch" >&2; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "the working tree has changes; commit or stash them" >&2; exit 1; }

current=$(python3 -c 'import pathlib,re;print(re.search(r"^__version__ = \"([^\"]+)\"", pathlib.Path("ccdesk/__init__.py").read_text(), re.M).group(1))')

tag="v$version"
git rev-parse -q --verify "refs/tags/$tag" >/dev/null && { echo "$tag already exists" >&2; exit 1; }

# The version goes up, or it stays where it is for a version nobody released
# yet (the first release of 0.1.0 tags master as it stands, no bump commit).
if [ "$version" = "$current" ]; then
  git ls-remote --exit-code --tags origin "refs/tags/$tag" >/dev/null 2>&1 \
    && { echo "$tag is already released; the next one needs a higher version" >&2; exit 1; }
else
  python3 - "$current" "$version" <<'PY' || { echo "ccdesk is at $current; a release only goes up" >&2; exit 1; }
import sys
cur, new = sys.argv[1], sys.argv[2]
key = lambda v: tuple(int(x) for x in v.split("."))
raise SystemExit(0 if key(new) > key(cur) else 1)
PY
fi

echo "updating the remote"
git fetch --quiet origin master
[ "$(git rev-parse master)" = "$(git rev-parse origin/master)" ] \
  || { echo "master is not level with origin/master; pull or push first" >&2; exit 1; }

echo "ccdesk $current -> $version"
if [ "$assume_yes" -eq 0 ]; then
  printf 'run the unit tests, then bump, commit, tag %s and push? [y/N] ' "$tag"
  read -r answer
  case "$answer" in y|Y|yes) ;; *) echo "nothing done"; exit 1 ;; esac
fi

echo "unit tests"
uv run --extra test pytest -q -m "not display"

# uv may have refreshed uv.lock or written bytecode in the tree it was given;
# the release commit is the version bump and nothing else. An uv.lock that did
# not exist before (a checkout that never ran uv) is left alone, untracked.
other=$(git status --porcelain | grep -v -e '^?? uv.lock$' || true)
[ -z "$other" ] || { echo "the tests changed the tree, commit that and retry:" >&2; echo "$other" >&2; exit 1; }

if [ "$version" != "$current" ]; then
  python3 - "$version" <<'PY'
import pathlib, re, sys
path = pathlib.Path("ccdesk/__init__.py")
text, count = re.subn(r'^__version__ = "[^"]+"', f'__version__ = "{sys.argv[1]}"', path.read_text(), count=1, flags=re.M)
if count != 1:
    raise SystemExit("could not find the __version__ line in ccdesk/__init__.py")
path.write_text(text)
PY
  git add ccdesk/__init__.py
  git commit --quiet -m "Release $tag"
fi

git tag -a "$tag" -m "ccdesk $version"
git push --quiet origin "refs/heads/master" "refs/tags/$tag"

echo "pushed $tag. The workflow publishes it:"
gh run list --workflow=release.yml --limit 1 --json status,url \
  --jq '.[] | "\(.status) \(.url)"' 2>/dev/null || echo "see the repo's Actions tab"
