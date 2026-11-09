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
# X.Y.Z, or a pre-release of it: 0.2.0b1, 0.2.0rc2, 0.2.0.dev3.
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+((a|b|rc)[0-9]+|\.dev[0-9]+)?$ ]] \
  || { echo "$version is not X.Y.Z, X.Y.Zb1, X.Y.Zrc1 or X.Y.Z.dev1" >&2; exit 2; }

cd "$(git rev-parse --show-toplevel)"
branch=$(git rev-parse --abbrev-ref HEAD)

# The suffix picks the channel, and the channel picks the branch: a pre-release
# comes from develop, a stable one from master. Nothing to remember.
if [[ "$version" =~ (a|b|rc|\.dev)[0-9]+$ ]]; then
  channel="pre-release"; want="develop"
else
  channel="stable"; want="master"
fi
[ "$branch" = "$want" ] || { echo "$version is $channel, and those come from $want; you are on $branch" >&2; exit 1; }
# A checkout that never ran uv has no lock yet: the first test run writes one, and that
# one file must not block a release.
dirty=$(git status --porcelain | grep -v -e '^?? uv.lock$' || true)
[ -z "$dirty" ] || { echo "the working tree has changes; commit or stash them:" >&2; echo "$dirty" >&2; exit 1; }

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
import re, sys

LEVEL = {"dev": 0, "a": 1, "b": 2, "rc": 3}  # a plain X.Y.Z ranks above all of them


def key(v):
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+)|\.dev(\d+))?$", v)
    if not m:
        raise SystemExit(f"cannot read version {v!r}")
    major, minor, patch, pre, pre_n, dev = m.groups()
    if dev:
        return (int(major), int(minor), int(patch), LEVEL["dev"], int(dev))
    return (int(major), int(minor), int(patch), LEVEL[pre] if pre else 4, int(pre_n or 0))


cur, new = sys.argv[1], sys.argv[2]
raise SystemExit(0 if key(new) > key(cur) else 1)
PY
fi

echo "updating the remote"
git fetch --quiet origin "$branch"
[ "$(git rev-parse "$branch")" = "$(git rev-parse "origin/$branch")" ] \
  || { echo "$branch is not level with origin/$branch; pull or push first" >&2; exit 1; }

echo "ccdesk $current -> $version ($channel, from $branch)"
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
git push --quiet origin "refs/heads/$branch" "refs/tags/$tag"

echo "pushed $tag. The workflow publishes it:"
gh run list --workflow=release.yml --limit 1 --json status,url \
  --jq '.[] | "\(.status) \(.url)"' 2>/dev/null || echo "see the repo's Actions tab"
