#!/usr/bin/env bash
set -euo pipefail

EE_DIR="$HOME/.config/earthengine"
EE_CREDS="$EE_DIR/credentials"

if [[ -f "$EE_CREDS" ]]; then
    echo "Earth Engine credentials already present at ~/.config/earthengine/"
    echo "Delete that file and re-run if you want to re-authenticate."
    exit 0
fi

# Ensure the directory exists and is owned by the current user so the
# container (which shares the same UID after setup.sh) can write into it.
mkdir -p "$EE_DIR"
if [[ "$(stat -c '%u' "$EE_DIR")" != "$(id -u)" ]]; then
    echo "ERROR: $EE_DIR is not owned by you. Fix it with:"
    echo "  sudo chown -R \$USER $EE_DIR"
    exit 1
fi

echo "Earth Engine authentication (notebook mode):"
echo "  1. A URL will appear below — open it in any browser."
echo "  2. Sign in with the Google account registered at https://code.earthengine.google.com/"
echo "  3. Copy the authorization code and paste it here when prompted."
echo ""

docker compose run --rm -it processor earthengine authenticate --auth_mode=notebook

echo ""
if [[ -f "$EE_CREDS" ]]; then
    echo "Credentials saved to ~/.config/earthengine/credentials"
else
    echo "WARNING: credentials file not found — authentication may have failed."
    exit 1
fi
