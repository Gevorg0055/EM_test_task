#!/usr/bin/env bash

set -euo pipefail

# Install k8sgpt and ensure it's on PATH (Linux/macOS)
# Usage:
#   DRY_RUN=1 ./scripts/install-k8sgpt.sh   # dry run
#   ./scripts/install-k8sgpt.sh              # real install

log() { echo "[install-k8sgpt] $*"; }

DRY_RUN="${DRY_RUN:-0}"
run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '+ %s\n' "$*"
  else
    eval "$@"
  fi
}

require_cmd() {
  if [[ "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  if ! command -v "$1" >/dev/null 2>&1; then
    log "Missing required command: $1"
    exit 1
  fi
}

detect_os() {
  case "$(uname -s)" in
    Linux)   OS_TOKEN="Linux" ;;
    Darwin)  OS_TOKEN="Darwin" ;;
    *)       log "Unsupported OS: $(uname -s)"; exit 1 ;;
  esac
}

detect_arch() {
  case "$(uname -m)" in
    x86_64|amd64) ARCH_TOKENS='amd64|x86_64' ;;
    aarch64|arm64) ARCH_TOKENS='arm64|aarch64' ;;
    *) log "Unsupported architecture: $(uname -m)"; exit 1 ;;
  esac
}

choose_bin_dir() {
  if [[ -w "/usr/local/bin" ]]; then
    BIN_DIR="/usr/local/bin"
  else
    BIN_DIR="${HOME}/.local/bin"
    run mkdir -p "$BIN_DIR"
  fi
}

ensure_on_path() {
  case ":$PATH:" in
    *":$BIN_DIR:"*) ;; # already on PATH
    *)
      log "Adding $BIN_DIR to PATH in shell profiles"
      # Add to common shell profiles if not already present
      for profile in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile"; do
        if [[ -f "$profile" ]]; then
          if ! grep -qs "$BIN_DIR" "$profile"; then
            run printf '%s\n' "export PATH=\"$BIN_DIR:\$PATH\"" >> "$profile"
          fi
        fi
      done
      log "Open a new shell or 'source' your profile to load the updated PATH."
      ;;
  esac
}

already_installed() {
  if command -v k8sgpt >/dev/null 2>&1; then
    log "k8sgpt already installed at $(command -v k8sgpt)"
    return 0
  fi
  return 1
}

# Fetch latest release asset URL matching OS/arch
get_latest_download_url() {
  require_cmd curl
  local api_url="https://api.github.com/repos/k8sgpt-ai/k8sgpt/releases/latest"
  local assets_json
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '%s\n' "https://example.com/k8sgpt_${OS_TOKEN}_dummy.${OS_TOKEN:+tar.gz}"
    return 0
  fi
  assets_json=$(curl -sL "$api_url")
  # Extract candidate URLs and match OS and ARCH tokens
  # Avoid requiring jq; use grep/sed for portability
  local url
  url=$(printf '%s\n' "$assets_json" \
    | grep -Eo '"browser_download_url"\s*:\s*"[^"]+"' \
    | sed -E 's/.*"(https:[^"]+)"/\1/' \
    | grep -Ei "$OS_TOKEN" \
    | grep -Ei "$ARCH_TOKENS" \
    | grep -E '\.(tar\.gz|zip)$' \
    | head -n1)
  if [[ -z "${url:-}" ]]; then
    log "Could not determine a suitable download URL from latest release assets"
    exit 1
  fi
  printf '%s\n' "$url"
}

install_from_url() {
  local url="$1"
  local tmpdir
  tmpdir=$(mktemp -d)
  trap "rm -rf \"$tmpdir\"" EXIT
  local filename="$tmpdir/$(basename "$url")"

  log "Downloading $url"
  run curl -fL "$url" -o "$filename"

  local extracted_dir="$tmpdir/extracted"
  run mkdir -p "$extracted_dir"

  if echo "$filename" | grep -qi '\.zip$'; then
    if ! command -v unzip >/dev/null 2>&1; then
      log "unzip is required to extract .zip archive"
      exit 1
    fi
    run unzip -q -o "$filename" -d "$extracted_dir"
  else
    require_cmd tar
    run tar -xzf "$filename" -f "$filename" -C "$extracted_dir" 2>/dev/null || run tar -xzf "$filename" -C "$extracted_dir"
  fi

  # Find the binary inside the extracted directory
  local bin_path
  if [[ "$DRY_RUN" == "1" ]]; then
    bin_path="$extracted_dir/k8sgpt"
  else
    bin_path=$(find "$extracted_dir" -type f \( -name 'k8sgpt' -o -name 'k8sgpt.exe' \) | head -n1 || true)
    if [[ -z "${bin_path:-}" ]]; then
      log "Failed to locate k8sgpt binary in the downloaded archive"
      exit 1
    fi
  fi

  local target="$BIN_DIR/$(basename "$bin_path")"
  log "Installing to $target"
  if command -v install >/dev/null 2>&1; then
    run install -m 0755 "$bin_path" "$target"
  else
    run cp "$bin_path" "$target"
    run chmod 0755 "$target"
  fi
}

main() {
  detect_os
  detect_arch
  choose_bin_dir

  if already_installed; then
    ensure_on_path
    exit 0
  fi

  local download_url
  download_url=$(get_latest_download_url)
  install_from_url "$download_url"
  ensure_on_path
  log "k8sgpt installation complete."
}

main "$@"
