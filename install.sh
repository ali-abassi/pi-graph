#!/bin/sh
set -eu

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
install_dir=${PI_WORKFLOWS_HOME:-"$HOME/.pi-workflows"}
python_bin=${PI_WORKFLOWS_PYTHON_BOOTSTRAP:-python3}
user_bin=${PI_WORKFLOWS_BIN_DIR:-"$HOME/.local/bin"}

command -v "$python_bin" >/dev/null 2>&1 || {
  printf 'Pi Workflows cannot find Python: %s\n' "$python_bin" >&2
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

"$python_bin" -m venv "$stage/product/.venv"
"$stage/product/.venv/bin/python" -m pip install --quiet --disable-pip-version-check \
  'PyYAML>=6,<7' 'ruamel.yaml>=0.18,<0.19' 'jsonschema>=4.23,<5'
"$stage/product/.venv/bin/python" -m py_compile "$stage/product"/scripts/*.py

if [ -e "$install_dir" ]; then
  backup="$install_dir.backup-$(date +%Y%m%d-%H%M%S)"
  mv "$install_dir" "$backup"
  printf 'Previous Pi Workflows install moved to: %s\n' "$backup"
fi
mkdir -p "$(dirname -- "$install_dir")"
mv "$stage/product" "$install_dir"

"$install_dir/bin/piw" schema --json >/dev/null

mkdir -p "$user_bin" "$HOME/.agents/skills" "$HOME/.claude/skills"
ln -sfn "$install_dir/bin/piw" "$user_bin/piw"
ln -sfn "$install_dir" "$HOME/.agents/skills/pi-workflows"
ln -sfn "$install_dir" "$HOME/.claude/skills/pi-workflows"

if command -v pi >/dev/null 2>&1; then
  pi install "$install_dir" --approve >/dev/null
else
  printf 'Pi is not on PATH; package registration skipped.\n' >&2
fi

printf 'Pi Workflows installed: %s\n' "$install_dir"
printf 'CLI: %s\n' "$user_bin/piw"
printf 'Pi package: %s\n' "$install_dir"
printf 'Codex skill: %s\n' "$HOME/.agents/skills/pi-workflows"
printf 'Claude Code skill: %s\n' "$HOME/.claude/skills/pi-workflows"
printf 'Run: piw doctor\n'
case ":$PATH:" in
  *":$user_bin:"*) ;;
  *)
    printf 'PATH note: add %s to PATH, or run %s/piw directly.\n' "$user_bin" "$user_bin" >&2
    ;;
esac
