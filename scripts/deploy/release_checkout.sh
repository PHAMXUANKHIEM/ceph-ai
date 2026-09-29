#!/usr/bin/env bash
# Prepare, activate, or roll back an immutable source checkout for deployment.
# The development checkout is only used as a Git object source; its working
# tree and index are never reset or modified.
set -euo pipefail

ROOT="${CEPH_AI_RELEASE_ROOT:-/var/lib/ceph-ai}"
RELEASES="$ROOT/releases"
CURRENT="$ROOT/current"
PREVIOUS="$ROOT/previous-release"
SOURCE_REPO="${CEPH_AI_SOURCE_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
command_name="${1:-}"
argument="${2:-}"

fail() { echo "ERROR: $*" >&2; exit 2; }
valid_sha() { [[ "${1:-}" =~ ^[0-9a-f]{40}$ ]]; }

safe_layout() {
  [ -d "$SOURCE_REPO/.git" ] || [ -f "$SOURCE_REPO/.git" ] || fail "source is not a Git checkout"
  [ ! -L "$ROOT" ] || fail "release root must not be a symlink"
  [ ! -L "$RELEASES" ] || fail "release directory must not be a symlink"
  [ ! -e "$CURRENT" ] || [ -L "$CURRENT" ] || fail "current release path exists and is not a symlink"
  [ ! -e "$PREVIOUS" ] || [ -L "$PREVIOUS" ] || fail "previous release path exists and is not a symlink"
  install -d -m 0750 "$RELEASES"
}

prepare() {
  local sha="$argument" target temp resolved
  valid_sha "$sha" || fail "usage: $0 prepare <full-40-character-commit-sha>"
  safe_layout
  git -C "$SOURCE_REPO" fetch --quiet origin "$sha"
  resolved="$(git -C "$SOURCE_REPO" rev-parse --verify "$sha^{commit}")"
  [ "$resolved" = "$sha" ] || fail "requested commit did not resolve exactly"
  target="$RELEASES/$sha"
  if [ -e "$target" ]; then
    [ ! -L "$target" ] || fail "release target must not be a symlink"
    [ -d "$target/.git" ] || [ -f "$target/.git" ] || fail "release target exists but is not a checkout"
    [ "$(git -C "$target" rev-parse HEAD)" = "$sha" ] || fail "existing release checkout has a different HEAD"
    [ -z "$(git -C "$target" status --porcelain --untracked-files=all)" ] || fail "existing release checkout is dirty"
    echo "RELEASE_CHECKOUT_READY sha=$sha path=$target reused=true"
    return
  fi
  temp="$(mktemp -d "$RELEASES/.prepare-$sha.XXXXXX")"
  if ! git clone --quiet --no-checkout "$SOURCE_REPO" "$temp"; then
    rmdir "$temp" 2>/dev/null || true
    fail "could not create isolated Git checkout"
  fi
  git -C "$temp" checkout --quiet --detach "$sha"
  [ "$(git -C "$temp" rev-parse HEAD)" = "$sha" ] || fail "checkout SHA verification failed"
  [ -z "$(git -C "$temp" status --porcelain --untracked-files=all)" ] || fail "new release checkout is dirty"
  mv "$temp" "$target"
  echo "RELEASE_CHECKOUT_READY sha=$sha path=$target reused=false"
}

set_link() {
  local link="$1" target="$2" temp
  temp="$ROOT/.link.$(basename "$link").$$"
  ln -s "$target" "$temp"
  # `current` and `previous-release` point to directories. Without `-T`,
  # GNU mv may follow an existing directory symlink and move the new link
  # inside the old release instead of replacing the pointer itself.
  mv -Tf "$temp" "$link"
}

activate() {
  local sha="$argument" target old
  valid_sha "$sha" || fail "usage: $0 activate <full-40-character-commit-sha>"
  safe_layout
  target="$RELEASES/$sha"
  [ -d "$target" ] || fail "release checkout is not prepared: $sha"
  [ ! -L "$target" ] || fail "release checkout must not be a symlink"
  [ "$(git -C "$target" rev-parse HEAD)" = "$sha" ] || fail "release checkout HEAD mismatch"
  [ -z "$(git -C "$target" status --porcelain --untracked-files=all)" ] || fail "refusing to activate a dirty release checkout"
  if [ -L "$CURRENT" ]; then
    old="$(readlink -f "$CURRENT")"
    case "$old" in "$RELEASES"/*) ;; *) fail "current release points outside release root" ;; esac
    if [ "$old" != "$target" ]; then set_link "$PREVIOUS" "$old"; fi
  fi
  set_link "$CURRENT" "$target"
  echo "RELEASE_CHECKOUT_ACTIVE sha=$sha path=$target"
}

rollback() {
  local target sha
  safe_layout
  [ -L "$PREVIOUS" ] || fail "no previous release checkout is recorded"
  target="$(readlink -f "$PREVIOUS")"
  case "$target" in "$RELEASES"/*) ;; *) fail "previous release points outside release root" ;; esac
  [ -d "$target" ] || fail "previous release checkout is missing"
  sha="$(basename "$target")"
  valid_sha "$sha" || fail "previous release directory name is not a commit SHA"
  [ "$(git -C "$target" rev-parse HEAD)" = "$sha" ] || fail "previous release checkout HEAD mismatch"
  [ -z "$(git -C "$target" status --porcelain --untracked-files=all)" ] || fail "previous release checkout is dirty"
  if [ -L "$CURRENT" ]; then set_link "$PREVIOUS" "$(readlink -f "$CURRENT")"; fi
  set_link "$CURRENT" "$target"
  echo "RELEASE_CHECKOUT_ROLLED_BACK sha=$sha path=$target"
}

case "$command_name" in
  prepare) prepare ;;
  activate) activate ;;
  rollback) rollback ;;
  *) fail "usage: $0 {prepare <sha>|activate <sha>|rollback}" ;;
esac
