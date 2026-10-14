#!/bin/sh
# One-line installer for quotabar:
#   curl -fsSL https://raw.githubusercontent.com/c-xiong/codex-claude-quotabar/main/install.sh | sh
set -eu

REPO="c-xiong/codex-claude-quotabar"
CODEXBAR_VERSION="${CODEXBAR_VERSION:-0.71.1}"
APP_DIR="${QUOTABAR_HOME:-$HOME/.local/share/quotabar}"
BIN_DIR="${QUOTABAR_BIN:-$HOME/.local/bin}"

say() { printf '\033[1;33m==>\033[0m %s\n' "$1"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$1" >&2; exit 1; }

command -v curl >/dev/null 2>&1 || die "curl is required"
PYTHON="$(command -v python3 || true)"
[ -n "$PYTHON" ] || die "python3 is required (macOS: xcode-select --install)"

case "$(uname -s)" in
  Darwin) os=macos ;;
  Linux)  os=linux ;;
  *) die "unsupported OS: $(uname -s) (macOS and Linux only)" ;;
esac
case "$(uname -m)" in
  arm64|aarch64) arch=$([ "$os" = macos ] && echo arm64 || echo aarch64) ;;
  x86_64|amd64)  arch=x86_64 ;;
  *) die "unsupported CPU: $(uname -m)" ;;
esac

if command -v shasum >/dev/null 2>&1; then sha() { shasum -a 256 "$1" | cut -d' ' -f1; }
elif command -v sha256sum >/dev/null 2>&1; then sha() { sha256sum "$1" | cut -d' ' -f1; }
else die "shasum or sha256sum is required"; fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT INT TERM

asset="CodexBarCLI-v${CODEXBAR_VERSION}-${os}-${arch}.tar.gz"
base="https://github.com/steipete/CodexBar/releases/download/v${CODEXBAR_VERSION}"
say "Downloading CodexBar CLI ${CODEXBAR_VERSION} (${os}-${arch})"
curl -fsSL -o "$tmp/$asset" "$base/$asset" || die "download failed: $base/$asset"
curl -fsSL -o "$tmp/$asset.sha256" "$base/$asset.sha256" || die "checksum download failed"
[ "$(sha "$tmp/$asset")" = "$(cut -d' ' -f1 < "$tmp/$asset.sha256")" ] || die "checksum mismatch for $asset"

say "Downloading quotabar"
curl -fsSL -o "$tmp/quotabar.py" "https://raw.githubusercontent.com/$REPO/main/quotabar.py" \
  || die "could not download quotabar.py"

say "Installing to $APP_DIR"
mkdir -p "$APP_DIR" "$BIN_DIR"
rm -rf "$APP_DIR/backend"
mkdir -p "$APP_DIR/backend"
tar xzf "$tmp/$asset" -C "$APP_DIR/backend"
mv "$tmp/quotabar.py" "$APP_DIR/quotabar.py"
cat > "$BIN_DIR/quotabar" <<EOF
#!/bin/sh
exec python3 "$APP_DIR/quotabar.py" "\$@"
EOF
chmod +x "$BIN_DIR/quotabar"

say "Installed. Run: quotabar"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) printf '\n%s is not on your PATH. Add this to your shell profile:\n  export PATH="%s:$PATH"\n' "$BIN_DIR" "$BIN_DIR" ;;
esac
