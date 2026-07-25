#!/usr/bin/env bash
# build-deb.sh — build the braincell-mcp .deb from the repo, reproducibly-ish.
#
# APPROACH (why hand-rolled dpkg-deb, chosen 2026-07-23):
#   The runtime deps (mcp, numpy, pydantic-core, ...) are not in the Debian/
#   Ubuntu archives, so a classic policy-compliant python3-* Depends chain is
#   impossible. Options considered:
#     * dh-virtualenv — effectively unmaintained, and it bakes the BUILD
#       host's absolute venv paths into the artifact (venvs are not
#       relocatable), which is exactly the class of bug we can't ship.
#     * fpm — a Ruby-gem toolchain; adding a whole new unvetted ecosystem to
#       build one .deb fails the supply-chain gate for no gain.
#     * hand-rolled dpkg-deb (CHOSEN) — dpkg-deb ships on every Debian/Ubuntu
#       host. We bundle the project wheel + all dependency wheels under
#       /usr/share/braincell-mcp/wheels, and postinst creates a private venv
#       at /opt/braincell-mcp/venv with `pip install --no-index` (offline —
#       no network at install time), then symlinks the three console scripts
#       into /usr/bin. This sidesteps venv relocation entirely: the venv is
#       always created at its final path, on the target machine.
#
#   CONSEQUENCE: binary wheels (numpy, pydantic-core, ...) are CPython-minor-
#   version specific, so the .deb targets the Python minor it was built with
#   (encoded in Depends: python3 (>= X.Y), python3 (<< X.Y+1)). Build on the
#   distro release you target (e.g. Ubuntu 24.04 -> Python 3.12).
#
# Usage:  packaging/linux/build-deb.sh
# Output: dist/braincell-mcp_<version>-<rev>_amd64.deb  (under the repo root)
# Env:
#   BRAINCELL_DEB_REVISION     Debian revision suffix (default: 1)
#   BRAINCELL_DEB_BUILD_DIR    scratch dir (default: mktemp under /tmp)
#   BRAINCELL_DEB_REQUIREMENTS optional hash-pinned requirements file; when
#                              set, dependency wheels are downloaded with
#                              --require-hashes from it instead of resolving
#                              from the project metadata (supply-chain
#                              hardening; generate with
#                              `uv pip compile --generate-hashes`).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PKG=braincell-mcp
ARCH=amd64
DEB_REVISION="${BRAINCELL_DEB_REVISION:-1}"

VERSION="$(python3 -c 'import tomllib,sys; print(tomllib.load(open(sys.argv[1],"rb"))["project"]["version"])' "$REPO_ROOT/pyproject.toml")"
PY_MINOR="$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
PY_NEXT="$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]+1}")')"
DEB_VERSION="${VERSION}-${DEB_REVISION}"

BUILD_DIR="${BRAINCELL_DEB_BUILD_DIR:-$(mktemp -d /tmp/braincell-deb.XXXXXX)}"
STAGE="$BUILD_DIR/stage"
WHEELHOUSE="$STAGE/usr/share/$PKG/wheels"
DOCDIR="$STAGE/usr/share/doc/$PKG"
TOOLVENV="$BUILD_DIR/toolvenv"
OUT_DIR="$REPO_ROOT/dist"

echo "==> braincell-mcp $DEB_VERSION for Python $PY_MINOR ($ARCH)"
echo "==> build dir: $BUILD_DIR"
rm -rf "$STAGE"
mkdir -p "$WHEELHOUSE" "$DOCDIR" "$STAGE/DEBIAN" "$OUT_DIR"

# --- 1. tooling venv (isolated pip; never touches the system or repo venv) --
python3 -m venv "$TOOLVENV"
PIP="$TOOLVENV/bin/pip"

# --- 2. project wheel (PEP 517 via pip; setuptools per pyproject) -----------
"$PIP" wheel --quiet --no-deps --wheel-dir "$WHEELHOUSE" "$REPO_ROOT"
PROJECT_WHEEL="$(ls "$WHEELHOUSE"/braincell_mcp-*.whl)"

# --- 3. dependency wheels (binary-only; no sdists ever get built) ------------
if [ -n "${BRAINCELL_DEB_REQUIREMENTS:-}" ]; then
    "$PIP" download --quiet --only-binary=:all: --require-hashes \
        --dest "$WHEELHOUSE" -r "$BRAINCELL_DEB_REQUIREMENTS"
else
    "$PIP" download --quiet --only-binary=:all: \
        --dest "$WHEELHOUSE" "$PROJECT_WHEEL"
fi

# --- 4. docs: copyright, licenses/notices, changelog.Debian.gz ---------------
install -m 0644 "$REPO_ROOT/packaging/linux/deb/copyright" "$DOCDIR/copyright"
install -m 0644 "$REPO_ROOT/LICENSE" "$DOCDIR/LICENSE"
install -m 0644 "$REPO_ROOT/NOTICE" "$DOCDIR/NOTICE"
DATE_RFC="$(date -R -u)"
cat > "$BUILD_DIR/changelog.Debian" <<EOF
braincell-mcp ($DEB_VERSION) unstable; urgency=low

  * Packaged from source tree (version $VERSION).

 -- Karl Toussaint (kt2saint) <kt2saint.create@gmail.com>  $DATE_RFC
EOF
gzip -9 -n -c "$BUILD_DIR/changelog.Debian" > "$DOCDIR/changelog.Debian.gz"

# --- 5. maintainer scripts + control -----------------------------------------
for script in postinst prerm postrm; do
    install -m 0755 "$REPO_ROOT/packaging/linux/deb/$script" "$STAGE/DEBIAN/$script"
done

INSTALLED_SIZE="$(du -sk --exclude=DEBIAN "$STAGE" | cut -f1)"
sed -e "s/@VERSION@/$DEB_VERSION/" \
    -e "s/@ARCH@/$ARCH/" \
    -e "s/@PYMIN@/$PY_MINOR/" \
    -e "s/@PYMAX@/$PY_NEXT/" \
    -e "s/@INSTALLED_SIZE@/$INSTALLED_SIZE/" \
    "$REPO_ROOT/packaging/linux/deb/control.in" > "$STAGE/DEBIAN/control"

# --- 6. md5sums ---------------------------------------------------------------
( cd "$STAGE" && find . -type f -not -path './DEBIAN/*' -printf '%P\n' \
    | sort | xargs md5sum > DEBIAN/md5sums )
chmod 0644 "$STAGE/DEBIAN/md5sums"

# --- 7. build ------------------------------------------------------------------
# Normalize permissions regardless of the build host's umask (dpkg-deb
# requires 0755..0775 directories; wheels should be world-readable).
find "$STAGE" -type d -exec chmod 0755 {} +
find "$STAGE" -type f -not -path '*/DEBIAN/*' -exec chmod 0644 {} +

DEB_OUT="$OUT_DIR/${PKG}_${DEB_VERSION}_${ARCH}.deb"
dpkg-deb --build --root-owner-group "$STAGE" "$DEB_OUT"

echo "==> built: $DEB_OUT"
echo "==> wheelhouse contents:"
ls -1 "$WHEELHOUSE" | sed 's/^/    /'
echo "==> install with: sudo apt install $DEB_OUT"
