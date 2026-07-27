#!/usr/bin/env bash
# Install BrainCell from this checkout (or GitHub) into an isolated virtualenv,
# then provision the default local embedding model when Ollama is available.
set -euo pipefail

REPOSITORY_URL="https://github.com/kt2saint-sec/braincell/archive/refs/heads/main.zip"
DEFAULT_MODEL="qwen3-embedding:4b"
VENV_DIR="${BRAINCELL_VENV_DIR:-$PWD/.braincell-venv}"
INSTALL_SOURCE=""
SKIP_MODEL=0

usage() {
    cat <<'EOF'
Usage: scripts/install.sh [--venv PATH] [--source PATH_OR_URL] [--skip-model]

Installs BrainCell into an isolated virtual environment. From a source checkout,
the checkout is installed; when the script is downloaded on its own, the public
GitHub main branch archive is installed instead.

Environment:
  BRAINCELL_VENV_DIR       virtualenv location (default: ./.braincell-venv)
  BRAINCELL_EMBED_PROVIDER embedding provider (default: ollama)
  BRAINCELL_EMBED_MODEL    Ollama model to pull (default: qwen3-embedding:4b)
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --venv)
            [ "$#" -ge 2 ] || { echo "--venv needs a path" >&2; exit 2; }
            VENV_DIR="$2"
            shift 2
            ;;
        --source)
            [ "$#" -ge 2 ] || { echo "--source needs a path or URL" >&2; exit 2; }
            INSTALL_SOURCE="$2"
            shift 2
            ;;
        --skip-model)
            SKIP_MODEL=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3.11 or newer is required, but python3 was not found." >&2
    exit 1
fi

if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
    PYTHON_VERSION="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    echo "BrainCell requires Python 3.11 or newer (found $PYTHON_VERSION)." >&2
    exit 1
fi

if [ -z "$INSTALL_SOURCE" ]; then
    SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
    REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
    if [ -f "$REPO_ROOT/pyproject.toml" ]; then
        INSTALL_SOURCE="$REPO_ROOT"
    else
        INSTALL_SOURCE="$REPOSITORY_URL"
    fi
fi

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install "$INSTALL_SOURCE"

echo
echo "BrainCell installed: $VENV_DIR/bin/braincell"

if [ "$SKIP_MODEL" -eq 1 ] || [ "${BRAINCELL_EMBED_PROVIDER:-ollama}" != "ollama" ]; then
    echo "Skipping local model provisioning."
elif command -v ollama >/dev/null 2>&1; then
    MODEL="${BRAINCELL_EMBED_MODEL:-$DEFAULT_MODEL}"
    echo "Pulling local embedding model: $MODEL"
    if ! ollama pull "$MODEL"; then
        cat <<EOF >&2
BrainCell is installed, but the embedding model could not be downloaded.
Ensure the Ollama service is running, then retry:
  ollama serve
  ollama pull $MODEL
EOF
        exit 1
    fi
else
    cat <<EOF
Ollama is not installed, so the embedding model was not downloaded.
Install Ollama from https://ollama.com/download, then run:
  ollama pull ${BRAINCELL_EMBED_MODEL:-$DEFAULT_MODEL}
EOF
fi

cat <<EOF

Next:
  cd /path/to/your/project
  $VENV_DIR/bin/braincell setup . --dry-run --client <client>
  $VENV_DIR/bin/braincell setup . --client <client> --yes
EOF
