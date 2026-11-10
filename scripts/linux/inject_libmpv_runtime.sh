#!/usr/bin/env bash
# Copy the prepared libmpv runtime into a Flutter Linux bundle and make the
# media_kit video plugin load it from its own directory.
#
# Usage: inject_libmpv_runtime.sh <bundle-dir>
#   <bundle-dir> is the Flutter build output directory, i.e. the one containing
#   the executable, lib/ and data/ (build/linux/x64/release/bundle).
#   When omitted, $BUILD_OUTPUT_DIRECTORY is used (set by flutter_distributor
#   packaging hooks).
#
# Run scripts/linux/fetch_libmpv_runtime.py once beforehand to populate
# third_party/libmpv-linux/runtime.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_DIR="${LIBMPV_RUNTIME_DIR:-$SCRIPT_DIR/../../third_party/libmpv-linux/runtime}"
BUNDLE_DIR="${1:-${BUILD_OUTPUT_DIRECTORY:-}}"

if [[ -z "$BUNDLE_DIR" ]]; then
  echo "error: bundle directory not given (pass it as \$1)" >&2
  exit 1
fi
if [[ ! -d "$BUNDLE_DIR/lib" ]]; then
  echo "error: $BUNDLE_DIR does not look like a Flutter Linux bundle" >&2
  exit 1
fi
if [[ ! -f "$RUNTIME_DIR/libmpv.so.2" ]]; then
  echo "error: libmpv runtime not found at $RUNTIME_DIR" >&2
  echo "       run: python3 $SCRIPT_DIR/fetch_libmpv_runtime.py" >&2
  exit 1
fi
if ! command -v patchelf >/dev/null 2>&1; then
  echo "error: patchelf is required (apt install patchelf)" >&2
  exit 1
fi

echo "bundling libmpv runtime into $BUNDLE_DIR/lib"
cp -a "$RUNTIME_DIR"/. "$BUNDLE_DIR/lib"/
rm -f "$BUNDLE_DIR/lib/.complete"

# The video plugin links libmpv.so.2 and was built with a CI-specific RUNPATH.
# Point it (and the companion plugin) at the bundle's own lib directory.
for plugin in libmedia_kit_video_plugin.so libmedia_kit_libs_linux_plugin.so; do
  if [[ -f "$BUNDLE_DIR/lib/$plugin" ]]; then
    patchelf --set-rpath '$ORIGIN' "$BUNDLE_DIR/lib/$plugin"
  fi
done

echo "libmpv runtime ready"
