#!/usr/bin/env bash
# stage-release-zip.sh <source_tree> <bins_dir> <stage_dir> <zip_path>
#
# Builds the install-ready staging tree and the release zip, then verifies the
# zip it just wrote. Shared by release.yml (`assemble`) and test.yml
# (`package`) so the PR check and the release execute the very same logic
# (ticket #195). CI-agnostic: no ${{ }} and no $GITHUB_* access -- callers
# pass everything as arguments and read the outputs themselves.
#
#   source_tree  tree holding the shippable non-binary files (stamped source
#                in release, the checkout on a PR)
#   bins_dir     directory of per-OS payloads: <bins_dir>/bin-*/{worktree,worktree.exe}
#   stage_dir    staging directory to (re)create
#   zip_path     zip file to write
#
# Env: PYTHON_BIN  python interpreter (default python3).
set -euo pipefail

if [ "$#" -ne 4 ]; then
  echo "usage: $0 <source_tree> <bins_dir> <stage_dir> <zip_path>" >&2
  exit 2
fi
SRC="$1"; BINS="$2"; STAGE="$3"; ZIP="$4"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# Everything that ships from the source tree. Copied unconditionally: a
# moved or renamed entry fails the run instead of silently shipping less.
SHIP=(.claude-plugin .codex-plugin .mcp.json README.md description.md skills assets)

rm -rf "$STAGE"
mkdir -p "$STAGE" "$(dirname "$ZIP")"
rm -f "$ZIP"

for p in "${SHIP[@]}"; do
  if [ ! -e "$SRC/$p" ]; then
    echo "::error::shipped path missing from source tree: $p" >&2
    exit 1
  fi
  cp -a "$SRC/$p" "$STAGE/"
done

# Merge every per-OS bin payload (bins/bin-<os>/worktree[.exe]).
mkdir -p "$STAGE/bin"
shopt -s nullglob
for d in "$BINS"/bin-*/; do
  echo "Merging $d -> $STAGE/bin/"
  cp -a "$d"* "$STAGE/bin/"
done

echo "=== merged bin/ ==="
ls -la "$STAGE/bin"

if [ ! -f "$STAGE/bin/worktree.exe" ]; then
  echo "::error::missing Windows binary in merged stage" >&2; exit 1
fi
if [ ! -f "$STAGE/bin/worktree" ]; then
  echo "::error::missing Linux binary in merged stage" >&2; exit 1
fi
chmod +x "$STAGE/bin/worktree"

# Python's zipfile lets us stamp Unix mode bits into the central directory,
# required so the Linux binary keeps its exec bit after `unzip` on
# Linux/macOS. The same block then re-reads the zip and verifies it.
"$PYTHON_BIN" - "$STAGE" "$ZIP" <<'PY'
import os, sys, time, zipfile

stage, zip_path = sys.argv[1], sys.argv[2]
EXECS = {"bin/worktree", "bin/worktree.exe"}
EXE_ATTR = (0o755 << 16) | 0x8000  # S_IFREG | 0755
REG_ATTR = (0o644 << 16) | 0x8000

staged = []
with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
    for dirpath, dirnames, filenames in os.walk(stage):
        dirnames.sort()
        for name in sorted(filenames):
            abs_path = os.path.join(dirpath, name)
            rel = os.path.relpath(abs_path, stage).replace(os.sep, "/")
            staged.append(rel)
            st = os.stat(abs_path)
            zi = zipfile.ZipInfo(filename=rel, date_time=time.localtime(st.st_mtime)[:6])
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.create_system = 3  # Unix
            zi.external_attr = EXE_ATTR if rel in EXECS else REG_ATTR
            with open(abs_path, "rb") as fh:
                zf.writestr(zi, fh.read())

# --- self-verify: what we wrote is what we intended to ship ---------------
errors = []
with zipfile.ZipFile(zip_path) as zf:
    infos = {zi.filename: zi for zi in zf.infolist()}
for rel in staged:
    if rel not in infos:
        errors.append(f"staged file missing from zip: {rel}")
# Anchored on the binaries actually present under bin/, not on EXECS alone,
# so a renamed/added binary that EXECS does not list cannot ship non-exec.
for rel in staged:
    if rel.startswith("bin/") or rel in EXECS:
        mode = (infos[rel].external_attr >> 16) & 0o777 if rel in infos else 0
        if mode != 0o755:
            errors.append(f"{rel} lacks exec bit in zip (mode {oct(mode)})")
for rel in sorted(EXECS):
    if rel not in infos:
        errors.append(f"expected executable not in zip: {rel}")
if errors:
    for e in errors:
        print(f"::error::{e}", file=sys.stderr)
    sys.exit(1)
print(f"wrote {zip_path} ({os.path.getsize(zip_path)} bytes), verified {len(staged)} entries")
PY
