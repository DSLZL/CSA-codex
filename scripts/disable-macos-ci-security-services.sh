#!/usr/bin/env bash
# Disposable GitHub-hosted macOS 26 runners only. Protected/absent services are best effort.
set -euo pipefail

if [[ "${GITHUB_ACTIONS:-}" != true || "${RUNNER_ENVIRONMENT:-}" != github-hosted || "${RUNNER_OS:-}" != macOS ]]; then
  echo 'Security shutdown requires a disposable GitHub-hosted macOS runner.' >&2
  exit 2
fi
macos_version="$(sw_vers -productVersion)"
if [[ "$macos_version" != 26 && "$macos_version" != 26.* ]]; then
  echo "Security shutdown requires macOS 26, got: $macos_version" >&2
  exit 2
fi

# Resolve every existing root before any mutation. Never clear metadata outside RUNNER_TEMP.
runner_temp="$(cd "${RUNNER_TEMP:?}" && pwd -P)"
[[ "$runner_temp" != / ]] || exit 2
paths=()
for path in "${SOURCE_ROOT:?}" "${CARGO_HOME:?}" "${CARGO_TARGET_DIR:?}" \
  "${SCCACHE_DIR:?}" "${TARGET_BUNDLE:?}" "$runner_temp/rusty_v8"; do
  [[ -e "$path" ]] || continue
  resolved="$(cd "$path" && pwd -P)"
  if [[ "$resolved" != "$runner_temp/"* ]]; then
    echo "Metadata path must be below RUNNER_TEMP: $resolved" >&2
    exit 2
  fi
  paths+=("$resolved")
done

# Use the native vendored binary that upstream's build.rs selects, not PATH/PROTOC.
case "${RUNNER_ARCH:?}" in
  ARM64) protoc_arch=aarch_64 ;;
  X64) protoc_arch=x86_64 ;;
  *) echo "Unsupported macOS runner architecture: $RUNNER_ARCH" >&2; exit 2 ;;
esac
protoc_candidates=("$CARGO_HOME"/registry/src/*/protoc-bin-vendored-macos-"$protoc_arch"-*/bin/protoc)
if [[ "${#protoc_candidates[@]}" != 1 || ! -x "${protoc_candidates[0]}" ]]; then
  echo 'Expected one executable native vendored protoc after Cargo prefetch.' >&2
  printf '%s\n' "${protoc_candidates[@]}" >&2
  exit 2
fi
verify_protoc() {
  echo "protoc preflight ($1): ${protoc_candidates[0]} --version"
  if "${protoc_candidates[0]}" --version; then
    echo "protoc preflight ($1) passed."
  else
    local status=$?
    echo "protoc preflight ($1) failed with exit status $status." >&2
    return "$status"
  fi
}
verify_protoc before

attempt() {
  printf '+ '; printf '%q ' "$@"; printf '\n'
  if "$@"; then
    return 0
  else
    local status=$?
    echo "Best-effort command returned $status; continuing."
  fi
}

uid_value="$(id -u)"
echo "::group::macOS $macos_version scanning/indexing shutdown"
attempt csrutil status
attempt defaults write com.apple.LaunchServices LSQuarantine -bool false
attempt sudo -n mdutil -a -i off

# Gatekeeper remains enabled on these images. Keep syspolicyd, trustd and the
# on-demand XProtect plugin available; only stop background scanning jobs.
for service in \
  com.apple.XProtect.daemon.scan \
  com.apple.XProtect.daemon.scan.startup; do
  attempt sudo -n launchctl disable "system/$service"
  attempt sudo -n launchctl bootout "system/$service"
done
for service in com.apple.XProtect.agent.scan com.apple.XProtect.agent.scan.startup; do
  attempt launchctl disable "gui/$uid_value/$service"
  attempt launchctl bootout "gui/$uid_value/$service"
done
for path in "${paths[@]}"; do
  # -s changes symlink metadata itself, rather than the referenced file.
  attempt sudo -n xattr -drs com.apple.quarantine "$path"
  attempt sudo -n xattr -drs com.apple.provenance "$path"
done

echo 'Background scanning/indexing shutdown requested; execution-assessment services are not stopped.'
attempt spctl --status
attempt launchctl print-disabled system
attempt launchctl print-disabled "gui/$uid_value"
attempt mdutil -a -s
attempt pgrep -lf 'XProtect|syspolicyd|trustd|mds|mdworker'
verify_protoc after
echo '::endgroup::'
