#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

command_exists() { command -v "$1" >/dev/null 2>&1; }

update_path() {
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
}

install_uv() {
    if command_exists uv; then
        echo "uv already installed: $(uv --version)"
        return
    fi
    echo "uv not found; installing it..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    update_path
    if ! command_exists uv; then
        echo "ERROR: uv was installed but is not on PATH. Open a new terminal and run setup.sh again." >&2
        exit 1
    fi
}

install_python() {
    if uv python find 3.12 >/dev/null 2>&1; then
        echo "Python 3.12 available: $(uv python find 3.12)"
    else
        echo "Python 3.12 not found; installing it via uv..."
        uv python install 3.12
    fi

    local venv_python="$PROJECT_DIR/.venv/bin/python"
    if [ ! -x "$venv_python" ]; then
        echo "Creating virtual environment in .venv..."
        uv venv --python 3.12 .venv
    fi
    uv pip install --python "$venv_python" -e .

    update_path
    if ! command_exists python; then
        echo "ERROR: Python was installed but is not on PATH. Open a new terminal and run setup.sh again." >&2
        exit 1
    fi
}

install_clang() {
    if command_exists cc || command_exists clang || command_exists gcc; then
        echo "C compiler already installed."
        return
    fi
    echo "C compiler not found; installing LLVM/Clang..."

    if command_exists apt-get; then
        sudo apt-get update && sudo apt-get install -y clang
    elif command_exists dnf; then
        sudo dnf install -y clang
    elif command_exists pacman; then
        sudo pacman -S --noconfirm clang
    elif command_exists brew; then
        brew install llvm
        update_path
        if ! command_exists clang; then
            echo "WARNING: LLVM was installed but clang is not on PATH. Open a new terminal and run setup.sh again."
            return
        fi
    else
        echo "WARNING: no supported package manager found; install LLVM/Clang or GCC manually."
        return
    fi

    update_path
    if ! command_exists clang; then
        echo "WARNING: LLVM was installed but clang is not on PATH. Open a new terminal and run setup.sh again."
    fi
}

install_app() {
    echo "Installing agentic command to PATH via uv tool (editable)..."
    uv tool install --python 3.12 --editable --force .
    uv tool update-shell || true
    export PATH="$(uv tool dir --bin):$PATH"
    if ! command_exists agentic; then
        echo "WARNING: agentic was installed but is not on PATH. Open a new terminal and run setup.sh again."
        return
    fi
    echo "agentic ready: $(command -v agentic)"
}

NVM_VERSION="v0.40.3"

load_nvm() {
    export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
    [ -s "$NVM_DIR/nvm.sh" ] || return 1
    # nvm.sh is not safe under `set -eu`
    set +eu
    . "$NVM_DIR/nvm.sh"
    set -eu
}

install_node() {
    if load_nvm; then
        echo "nvm already installed: $(nvm --version)"
    else
        echo "nvm not found; installing $NVM_VERSION..."
        curl -o- "https://raw.githubusercontent.com/nvm-sh/nvm/$NVM_VERSION/install.sh" | bash
        if ! load_nvm; then
            echo "ERROR: nvm was installed but could not be loaded. Open a new terminal and run setup.sh again." >&2
            exit 1
        fi
    fi

    echo "Installing Node.js LTS via nvm..."
    set +eu
    nvm install --lts && nvm alias default 'lts/*' && nvm use default
    local status=$?
    set -eu
    if [ "$status" -ne 0 ] || ! command_exists node; then
        echo "ERROR: Node.js LTS install via nvm failed." >&2
        exit 1
    fi
    echo "Node.js ready: $(node --version)"
}

install_uv
install_python
install_app
install_clang
install_node

echo "Setup complete."
echo "Activate the environment with: source .venv/bin/activate"
echo "Run the app from anywhere with: agentic"
