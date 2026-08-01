#!/usr/bin/env bash

set -euo pipefail

readonly DEFAULT_VERSION="v1.0.0"
readonly DEFAULT_INSTALL_DIR="${HOME}/.local/share/hsync-client"
readonly DEFAULT_SYMLINK_DIR="${HOME}/.local/bin"
readonly REPO="albertobasso/hsync-client"

INSTALL_DIR="${DEFAULT_INSTALL_DIR}"
SYMLINK_DIR="${DEFAULT_SYMLINK_DIR}"
VERSION="${DEFAULT_VERSION}"
CREATE_SYMLINK=true
DRY_RUN=false

usage() {
    cat <<EOF
Install the HSYNC client binary.

Usage: ./install.sh [options]

Options:
  --version VERSION       Release version to install, or 'latest' to resolve the
                          latest stable release automatically (default: ${DEFAULT_VERSION})
  --install-dir PATH      Directory where the binary is placed
                          (default: ${DEFAULT_INSTALL_DIR})
  --symlink-dir PATH      Directory for the hsync symlink
                          (default: ${DEFAULT_SYMLINK_DIR})
  --no-symlink            Do not create a symlink
  --dry-run               Show the planned installation without changing files
  -h, --help              Show this help

The installer downloads a prebuilt binary from GitHub Releases, verifies its
SHA-256 checksum, extracts it, and makes it executable. It never invokes sudo;
choose a user-writable install directory. Re-running it updates the binary to
the selected version while preserving the install directory.
EOF
}

fail() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

validate_version() {
    [[ "$1" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([+-][0-9A-Za-z.-]+)?$ ]] ||
        fail "invalid version '$1'; expected vX.Y.Z or 'latest'"
}

resolve_latest_version() {
    local url effective tag
    url="https://github.com/${REPO}/releases/latest"
    effective="$(curl -fsSIL -o /dev/null -w '%{url_effective}' "${url}")" ||
        fail "could not resolve the latest release from ${url}"
    tag="${effective##*/}"
    [[ "${tag}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([+-][0-9A-Za-z.-]+)?$ ]] ||
        fail "unexpected latest release tag '${tag}' from ${url}"
    printf '%s' "${tag}"
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --version)
                [[ -n "${2:-}" ]] || fail "$1 requires a value"
                VERSION="$2"
                shift 2
                ;;
            --install-dir)
                [[ -n "${2:-}" ]] || fail "$1 requires a value"
                INSTALL_DIR="$2"
                shift 2
                ;;
            --symlink-dir)
                [[ -n "${2:-}" ]] || fail "$1 requires a value"
                SYMLINK_DIR="$2"
                shift 2
                ;;
            --no-symlink)
                CREATE_SYMLINK=false
                shift
                ;;
            --dry-run)
                DRY_RUN=true
                shift
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                fail "unknown option: $1"
                ;;
        esac
    done
}

detect_platform() {
    local os arch
    case "$(uname -s)" in
        Linux*)  os="linux" ;;
        Darwin*) os="darwin" ;;
        *) fail "unsupported OS: $(uname -s). Use the .tar.gz / .zip archive manually." ;;
    esac
    case "$(uname -m)" in
        x86_64|amd64)   arch="amd64" ;;
        aarch64|arm64)  arch="arm64" ;;
        armv7l|armv7)
            fail "no prebuilt binary for ARMv7; install the .deb package (armhf) instead."
            ;;
        *) fail "unsupported architecture: $(uname -m)" ;;
    esac
    printf '%s-%s' "${os}" "${arch}"
}

sha256_verify() {
    local sums_file="$1"
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum -c "${sums_file}" --ignore-missing
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 -c "${sums_file}" --ignore-missing
    else
        fail "neither sha256sum nor shasum is available"
    fi
}

main() {
    parse_args "$@"

    if [[ "${INSTALL_DIR}" != /* ]]; then
        INSTALL_DIR="$(pwd)/${INSTALL_DIR}"
    fi
    if [[ "${CREATE_SYMLINK}" == true && "${SYMLINK_DIR}" != /* ]]; then
        SYMLINK_DIR="$(pwd)/${SYMLINK_DIR}"
    fi

    if [[ "${VERSION}" == "latest" ]]; then
        require_command curl
        VERSION="$(resolve_latest_version)"
    fi
    validate_version "${VERSION}"

    local target
    target="$(detect_platform)"

    local asset="hsync-client-${VERSION}-${target}.tar.gz"
    local base_url="https://github.com/${REPO}/releases/download/${VERSION}"

    printf 'Version:    %s\n' "${VERSION}"
    printf 'Platform:   %s\n' "${target}"
    printf 'Install dir: %s\n' "${INSTALL_DIR}"
    if [[ "${CREATE_SYMLINK}" == true ]]; then
        printf 'Symlink:    %s/hsync -> %s/hsync\n' "${SYMLINK_DIR}" "${INSTALL_DIR}"
    fi
    printf 'Asset:      %s\n' "${asset}"

    if [[ "${DRY_RUN}" == true ]]; then
        printf 'Dry run: no files were changed.\n'
        exit 0
    fi

    require_command curl
    require_command tar

    local tmp_dir
    tmp_dir="$(mktemp -d)"
    trap 'rm -rf "${tmp_dir}"' EXIT

    printf 'Downloading %s ...\n' "${asset}"
    curl -fsSL "${base_url}/${asset}" -o "${tmp_dir}/${asset}"

    printf 'Downloading SHA256SUMS ...\n'
    curl -fsSL "${base_url}/SHA256SUMS" -o "${tmp_dir}/SHA256SUMS" || \
        fail "SHA256SUMS not found for ${VERSION}"

    printf 'Verifying checksum ...\n'
    (cd "${tmp_dir}" && sha256_verify "SHA256SUMS")

    mkdir -p "${INSTALL_DIR}"
    tar xzf "${tmp_dir}/${asset}" -C "${tmp_dir}"

    install -m 0755 "${tmp_dir}/hsync" "${INSTALL_DIR}/hsync"

    if [[ "$(uname -s)" == "Darwin" ]]; then
        xattr -d com.apple.quarantine "${INSTALL_DIR}/hsync" 2>/dev/null || true
    fi

    if [[ "${CREATE_SYMLINK}" == true ]]; then
        mkdir -p "${SYMLINK_DIR}"
        ln -sf "${INSTALL_DIR}/hsync" "${SYMLINK_DIR}/hsync"
    fi

    printf '\nHSYNC client %s installed to %s/hsync\n' "${VERSION}" "${INSTALL_DIR}"

    if [[ "${CREATE_SYMLINK}" == true ]]; then
        if ! echo ":${PATH}:" | grep -q ":${SYMLINK_DIR}:"; then
            printf '\nAdd %s to your PATH:\n  export PATH="%s:$PATH"\n' \
                "${SYMLINK_DIR}" "${SYMLINK_DIR}"
        fi
    else
        printf '\nRun it with: %s/hsync --help\n' "${INSTALL_DIR}"
    fi
}

main "$@"
