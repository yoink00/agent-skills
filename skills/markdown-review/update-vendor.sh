#!/usr/bin/env bash
#
# update-vendor.sh — (re)download the front-end libraries the browser viewer
# uses (marked, highlight.js) into ./vendor so the viewer works fully offline.
#
# The list of files and their source URLs lives in ONE place: the VENDOR_ASSETS
# manifest in mdedit.py. This script reads it via `mdedit.py vendor-manifest`,
# downloads each asset, and records URLs + sha256 checksums in vendor/MANIFEST.json.
#
# Usage:
#   ./update-vendor.sh            # download/refresh all vendored assets
#   ./update-vendor.sh --check    # verify vendored files match MANIFEST.json
#
# Requires: python3 (for the manifest), and curl OR wget for downloads.
# To change versions, edit VENDOR_ASSETS in mdedit.py and re-run this script.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MDEDIT="$SCRIPT_DIR/mdedit.py"
VENDOR_DIR="$SCRIPT_DIR/vendor"
MANIFEST="$VENDOR_DIR/MANIFEST.json"

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

# ---- helpers ---------------------------------------------------------------

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

# Download $1 (url) to $2 (path), using curl or wget.
fetch() {
  local url="$1" out="$2"
  if have curl; then
    curl -fsSL "$url" -o "$out"
  elif have wget; then
    wget -qO "$out" "$url"
  else
    die "need curl or wget to download assets"
  fi
}

sha256() {
  if have sha256sum; then sha256sum "$1" | awk '{print $1}'
  elif have shasum; then shasum -a 256 "$1" | awk '{print $1}'
  else python3 -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$1"
  fi
}

have python3 || die "python3 is required"
[ -f "$MDEDIT" ] || die "cannot find mdedit.py next to this script"

# ---- read the asset manifest from mdedit.py --------------------------------
# Emits lines: "<filename>\t<url>"
mapfile -t ASSETS < <(
  python3 "$MDEDIT" vendor-manifest \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print("\n".join(f"{k}\t{v}" for k,v in d["assets"].items()))'
)
[ "${#ASSETS[@]}" -gt 0 ] || die "no assets found in manifest"

mkdir -p "$VENDOR_DIR"

# ---- check mode ------------------------------------------------------------
if [ "$CHECK_ONLY" -eq 1 ]; then
  [ -f "$MANIFEST" ] || die "no MANIFEST.json — run ./update-vendor.sh first"
  rc=0
  for line in "${ASSETS[@]}"; do
    fname="${line%%$'\t'*}"
    path="$VENDOR_DIR/$fname"
    if [ ! -f "$path" ]; then
      printf 'MISSING  %s\n' "$fname"; rc=1; continue
    fi
    want="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['files'].get(sys.argv[2],{}).get('sha256',''))" "$MANIFEST" "$fname")"
    got="$(sha256 "$path")"
    if [ -n "$want" ] && [ "$want" != "$got" ]; then
      printf 'CHANGED  %s\n' "$fname"; rc=1
    else
      printf 'OK       %s\n' "$fname"
    fi
  done
  exit "$rc"
fi

# ---- download mode ---------------------------------------------------------
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

ENTRIES=""
for line in "${ASSETS[@]}"; do
  fname="${line%%$'\t'*}"
  url="${line#*$'\t'}"
  printf 'downloading %-26s <- %s\n' "$fname" "$url"
  fetch "$url" "$TMP/$fname"
  [ -s "$TMP/$fname" ] || die "downloaded empty file for $fname"
  mv "$TMP/$fname" "$VENDOR_DIR/$fname"
  sum="$(sha256 "$VENDOR_DIR/$fname")"
  bytes="$(wc -c < "$VENDOR_DIR/$fname" | tr -d ' ')"
  ENTRIES="$ENTRIES{\"file\":\"$fname\",\"url\":\"$url\",\"sha256\":\"$sum\",\"bytes\":$bytes},"
done

# Write MANIFEST.json (provenance for the vendored files).
python3 - "$MANIFEST" "${ENTRIES%,}" <<'PY'
import json, sys, time
manifest_path, entries = sys.argv[1], sys.argv[2]
items = json.loads("[" + entries + "]")
out = {
    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "note": "Vendored front-end assets for the markdown-review viewer. "
            "Regenerate with ./update-vendor.sh. Source URLs come from "
            "VENDOR_ASSETS in mdedit.py.",
    "files": {it["file"]: {k: it[k] for k in ("url", "sha256", "bytes")} for it in items},
}
with open(manifest_path, "w") as f:
    json.dump(out, f, indent=2)
    f.write("\n")
print(f"wrote {manifest_path} ({len(items)} files)")
PY

echo "done — vendored ${#ASSETS[@]} asset(s) into $VENDOR_DIR"
