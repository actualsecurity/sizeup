#!/usr/bin/env bash
# CI (.github/scripts/station-update.sh, in the station-install job), after install-linux.sh built trunk-recorder: the
# station's patch (#638, install-linux.sh tr_patch) tested on the real transmission_sink. tr_split_test.cc is built
# against the patched source tree and the libtrunk-recorder.a built from it, and run.
#   tr-split-test.sh [SOURCE_DIR [BUILD_DIR]]   (default /opt/sizeup/src/trunk-recorder and its build/)
set -euo pipefail
src=${1:-/opt/sizeup/src/trunk-recorder}
build=${2:-$src/build}
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT
grep -q "SIZEUP_TR_SPLIT" "$src/trunk-recorder/gr_blocks/transmission_sink.cc" || {
  echo "tr-split-test: $src is not patched" >&2
  exit 1
}
# the library's own defines (GNURADIO_VERSION picks std::shared_ptr) and include directories, as cmake wrote them
flags=$build/CMakeFiles/trunk_recorder_library.dir/flags.make
read -ra defs <<<"$(sed -n 's/^CXX_DEFINES = //p' "$flags" | tr ' ' '\n' | grep -v MANUAL_GITINFO | tr '\n' ' ')"
read -ra incs <<<"$(sed -n 's/^CXX_INCLUDES = //p' "$flags")"
# shellcheck disable=SC2046  # (pkg-config's words are separate flags)
c++ -std=c++17 -O1 -pthread "${defs[@]}" -I"$src" -I"$src/trunk-recorder" "${incs[@]}" ${CXXFLAGS:-} \
  "$here/tr_split_test.cc" -o "$out/tr_split_test" \
  "$build/libtrunk-recorder.a" \
  $(pkg-config --libs gnuradio-runtime gnuradio-blocks 2>/dev/null || echo -lgnuradio-runtime -lgnuradio-blocks -lgnuradio-pmt) \
  $(pkg-config --libs spdlog fmt 2>/dev/null || echo -lfmt) \
  ${LDFLAGS:-} -lboost_log -lboost_log_setup -lboost_thread -lboost_filesystem -lpthread
"$out/tr_split_test"
