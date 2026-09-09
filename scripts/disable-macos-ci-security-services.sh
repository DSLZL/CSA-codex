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
attempt sudo -n spctl --global-disable
attempt defaults write com.apple.LaunchServices LSQuarantine -bool false
attempt sudo -n mdutil -a -i off

for service in \
  com.apple.XProtect.daemon.scan \
  com.apple.XProtect.daemon.scan.startup \
  com.apple.XprotectFramework.PluginService \
  com.apple.security.syspolicy \
  com.apple.trustd \
  com.apple.metadata.mds; do
  attempt sudo -n launchctl disable "system/$service"
  attempt sudo -n launchctl bootout "system/$service"
done
for service in com.apple.XProtect.agent.scan com.apple.XProtect.agent.scan.startup com.apple.trustd.agent; do
  attempt launchctl disable "gui/$uid_value/$service"
  attempt launchctl bootout "gui/$uid_value/$service"
done
attempt sudo -n pkill -9 -f XProtect
for process in syspolicyd trustd mds mds_stores mdworker mdworker_shared; do
  attempt sudo -n pkill -9 -x "$process"
done

for path in "${paths[@]}"; do
  # -s changes symlink metadata itself, rather than the referenced file.
  attempt sudo -n xattr -drs com.apple.quarantine "$path"
  attempt sudo -n xattr -drs com.apple.provenance "$path"
done

attempt sudo -n pkill -9 -f XProtect
for process in syspolicyd trustd mds mds_stores mdworker mdworker_shared; do
  attempt sudo -n pkill -9 -x "$process"
done
echo 'Shutdown requested. Protected/on-demand services may remain active or restart.'
attempt spctl --status
attempt launchctl print-disabled system
attempt launchctl print-disabled "gui/$uid_value"
attempt mdutil -a -s
attempt pgrep -lf 'XProtect|syspolicyd|trustd|mds|mdworker'
echo '::endgroup::'
