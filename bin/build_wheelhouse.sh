#!/bin/bash
# Build an offline install bundle ("wheelhouse") of host-check-tool for hosts on
# a closed network.  Run this on an Internet-connected machine, copy the
# resulting tar.gz to the target host, and install there without network:
#
#   tar xzf procdiag-wheelhouse-py3.10-x86_64.tar.gz
#   ./procdiag-wheelhouse-py3.10-x86_64/install.sh
#
# Usage: bin/build_wheelhouse.sh [PYTHON_VERSION] [ARCH]
#   PYTHON_VERSION  the TARGET host's Python (python3 --version), default 3.10
#   ARCH            the TARGET host's CPU (uname -m): x86_64 | aarch64, default x86_64
# Environment:
#   PYTHON          interpreter used to run pip on THIS machine (default python3)
# See docs/offline-install.md for the full procedure.
set -euo pipefail

PYVER="${1:-3.10}"
ARCH="${2:-x86_64}"
PY="${PYTHON:-python3}"
OUT="procdiag-wheelhouse-py${PYVER}-${ARCH}"

cd "$(dirname "$0")/.."
if [ -e "$OUT" ] || [ -e "$OUT.tar.gz" ]; then
    echo "error: $OUT already exists; remove it first" >&2
    exit 1
fi
mkdir "$OUT"

# 1. This package (pure Python) from the current checkout.
"$PY" -m pip wheel --no-deps --wheel-dir "$OUT" .

# 2. Its dependencies as binary wheels for the target interpreter / CPU.
#    manylinux2014 (glibc >= 2.17) covers RHEL/CentOS 7+, Ubuntu 18.04+, Debian 9+.
"$PY" -m pip download --dest "$OUT" --only-binary=:all: \
    --python-version "$PYVER" --implementation cp \
    --platform "manylinux2014_${ARCH}" --platform "manylinux_2_17_${ARCH}" \
    "$OUT"/host_check_tool-*.whl

# 3. Installer for the target host (no network access).
cat > "$OUT/install.sh" <<'INSTALL'
#!/bin/bash
# Install procdiag from this directory without network access.
#   ./install.sh            -> into a venv at ~/procdiag-venv (recommended)
#   ./install.sh --system   -> into the current python3 environment
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ "${1:-}" = "--system" ]; then
    python3 -m pip install --no-index --find-links "$HERE" host-check-tool
else
    python3 -m venv "$HOME/procdiag-venv"
    "$HOME/procdiag-venv/bin/pip" install --no-index --find-links "$HERE" host-check-tool
    echo "installed: $HOME/procdiag-venv/bin/procdiag"
fi
INSTALL
chmod +x "$OUT/install.sh"

tar czf "$OUT.tar.gz" "$OUT"
echo "wrote $OUT.tar.gz ($(du -h "$OUT.tar.gz" | cut -f1)) — copy it to the target host"
