#!/bin/sh
set -eu

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
install_dir=${PI_WORKFLOWS_HOME:-"$HOME/.pi-workflows"}
python_bin=${PI_WORKFLOWS_PYTHON_BOOTSTRAP:-python3}
user_bin=${PI_WORKFLOWS_BIN_DIR:-"$HOME/.local/bin"}
keep_backups=${PI_WORKFLOWS_KEEP_BACKUPS:-2}

codex_skill="$HOME/.agents/skills/pi-workflows"
claude_skill="$HOME/.claude/skills/pi-workflows"
pi_skill="$HOME/.pi/agent/skills/pi-workflows"

usage() {
  cat <<EOF
Usage: ./install.sh [--uninstall] [--help]

Installs pi workflows to $install_dir, exposes piw from $user_bin, and links
the skill for Codex and Claude Code. With Pi on PATH it also registers the
local Pi package, which writes to ~/.pi/agent/settings.json.

Environment:
  PI_WORKFLOWS_HOME              install location (default ~/.pi-workflows)
  PI_WORKFLOWS_BIN_DIR           where piw is linked (default ~/.local/bin)
  PI_WORKFLOWS_PYTHON_BOOTSTRAP  python used to build the venv (default python3)
  PI_WORKFLOWS_KEEP_BACKUPS      previous installs to retain (default 2)
EOF
}

# Remove a path only when it is our symlink, or the install directory itself.
unlink_ours() {
  name=$1
  if [ -L "$name" ]; then
    rm -f "$name"
    printf 'removed %s\n' "$name"
  elif [ -e "$name" ]; then
    printf 'left in place (not a pi workflows symlink): %s\n' "$name" >&2
  fi
}

uninstall() {
  if command -v pi >/dev/null 2>&1; then
    if pi remove "$install_dir" --approve >/dev/null; then
      printf 'removed Pi package registration for %s\n' "$install_dir"
    else
      printf 'could not remove the Pi package registration; install left in place\n' >&2
      return 1
    fi
  fi
  unlink_ours "$user_bin/piw"
  unlink_ours "$codex_skill"
  unlink_ours "$claude_skill"
  unlink_ours "$pi_skill"
  if [ -d "$install_dir" ]; then
    rm -rf "$install_dir"
    printf 'removed %s\n' "$install_dir"
  fi
  for old in "$install_dir".backup-*; do
    [ -e "$old" ] || continue
    rm -rf "$old"
    printf 'removed %s\n' "$old"
  done
  printf 'pi workflows uninstalled.\n'
}

case "${1:-}" in
  --uninstall) uninstall; exit 0 ;;
  --help|-h) usage; exit 0 ;;
  "") ;;
  *) printf 'unknown option: %s\n\n' "$1" >&2; usage >&2; exit 2 ;;
esac

command -v "$python_bin" >/dev/null 2>&1 || {
  printf 'pi workflows cannot find Python: %s\n' "$python_bin" >&2
  exit 1
}

# Check the version here rather than letting `piw doctor` report it after a
# clean-looking install.
"$python_bin" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' || {
  printf 'pi workflows needs Python 3.10 or newer; %s is %s\n' \
    "$python_bin" "$("$python_bin" -c 'import sys; print(sys.version.split()[0])')" >&2
  printf 'Set PI_WORKFLOWS_PYTHON_BOOTSTRAP to a newer interpreter.\n' >&2
  exit 1
}

stage=$(mktemp -d "${TMPDIR:-/tmp}/pi-workflows.XXXXXX")
cleanup() { [ ! -d "$stage" ] || find "$stage" -depth -delete; }
trap cleanup EXIT HUP INT TERM

mkdir -p "$stage/product"
(cd "$source_dir" && tar \
  --exclude='./.git' --exclude='./.venv' --exclude='./.pytest_cache' \
  --exclude='./__pycache__' --exclude='./node_modules' --exclude='*/cache' --exclude='*/runs' \
  --exclude='*/batch-[0-9]*' \
  --exclude='./outputs' --exclude='./state' --exclude='./examples/.artifacts' \
  -cf - .) | (cd "$stage/product" && tar -xf -)

backup=""
if [ -e "$install_dir" ]; then
  backup="$install_dir.backup-$(date +%Y%m%d-%H%M%S)"
  mv "$install_dir" "$backup"
  printf 'Previous install moved to: %s\n' "$backup"
fi

# Restore the previous install if provisioning fails after we have moved it.
rollback() {
  status=$?
  [ "$status" -eq 0 ] && return 0
  if [ -n "$backup" ] && [ -d "$backup" ]; then
    rm -rf "$install_dir"
    mv "$backup" "$install_dir"
    printf 'Install failed; restored the previous version.\n' >&2
  fi
  cleanup
}
trap rollback EXIT HUP INT TERM

mkdir -p "$(dirname -- "$install_dir")"
mv "$stage/product" "$install_dir"

# The venv is built in its final location. A venv created under the staging
# directory and then moved keeps absolute shebangs pointing at the deleted
# staging path, which silently breaks pip and every other console script.
"$python_bin" -m venv "$install_dir/.venv"
"$install_dir/.venv/bin/python" -m pip install --quiet --disable-pip-version-check \
  -r "$install_dir/requirements.txt"
"$install_dir/.venv/bin/python" -m py_compile "$install_dir"/scripts/*.py
"$install_dir/.venv/bin/pip" --version >/dev/null

"$install_dir/bin/piw" schema --json >/dev/null

# `ln -sfn` replaces a real file without warning, and when the target is an
# existing real directory it silently creates the link *inside* it instead.
# Move anything that is not already our symlink out of the way first.
link() {
  target=$1
  name=$2
  if [ -e "$name" ] && [ ! -L "$name" ]; then
    moved="$name.backup-$(date +%Y%m%d-%H%M%S)"
    mv "$name" "$moved"
    printf 'Existing %s moved to: %s\n' "$name" "$moved" >&2
  fi
  ln -sfn "$target" "$name"
}

mkdir -p "$user_bin" "$(dirname -- "$codex_skill")" "$(dirname -- "$claude_skill")" "$(dirname -- "$pi_skill")"
link "$install_dir/bin/piw" "$user_bin/piw"
link "$install_dir" "$codex_skill"
link "$install_dir" "$claude_skill"
link "$install_dir" "$pi_skill"

if command -v pi >/dev/null 2>&1; then
  pi install "$install_dir" --approve >/dev/null
  printf 'Registered the Pi package (updates ~/.pi/agent/settings.json).\n'
else
  printf 'Pi is not on PATH; package registration skipped.\n' >&2
fi

# Old installs are kept for rollback, not forever. This used to grow without
# bound; 22 backups was ~490 MB on one machine.
# Glob directly and quote: `$(ls -dt ...)` word-splits, so a space anywhere in
# the path (PI_WORKFLOWS_HOME is user-settable) shattered each path into
# fragments that were then rm -rf'd — destroying unrelated directories AND the
# backups this is supposed to retain. Timestamps sort chronologically, so the
# newest are last; keep the tail.
set -- "$install_dir".backup-*
total=0
for old do
  [ -e "$old" ] || continue
  total=$((total + 1))
done
index=0
for old do
  [ -e "$old" ] || continue
  index=$((index + 1))
  [ "$((total - index))" -lt "$keep_backups" ] && continue
  rm -rf "$old"
  printf 'Pruned old backup: %s\n' "$old"
done

trap cleanup EXIT HUP INT TERM

printf '\npi workflows installed: %s\n' "$install_dir"
printf 'CLI: %s\n' "$user_bin/piw"
printf 'Codex skill: %s\n' "$codex_skill"
printf 'Claude Code skill: %s\n' "$claude_skill"
printf 'Pi skill: %s\n' "$pi_skill"
printf 'Run: piw doctor\n'
printf 'Uninstall: %s/install.sh --uninstall\n' "$install_dir"
case ":$PATH:" in
  *":$user_bin:"*) ;;
  *)
    printf '\nPATH note: add %s to PATH, or run %s/piw directly.\n' "$user_bin" "$user_bin" >&2
    ;;
esac
