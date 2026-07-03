# syntax=docker/dockerfile:1.6
#
# HSYNC — WD My Cloud OS5 builder image (ARMv7 / amd64)
#
# Multi-stage build that produces a debian:buster (glibc 2.28) environment
# containing Python 3.12, the project's third-party dependencies (compiled
# once and cached as a BuildKit layer) and the application sources, ready to
# invoke PyInstaller. The expensive ARMv7 source builds therefore happen at
# `docker build` time instead of on every `docker run`.
#
# The base is Debian 10 buster on purpose: its glibc (2.28) is below the
# 2.30 ceiling exposed by the stock WD OS5 firmware, so the binary that
# PyInstaller bundles will load on the NAS without ``GLIBC_X.XX not found``
# errors.  This applies to both CPU families supported by OS5.
#
# Built and consumed locally by:
#   compile.sh --target wd-ex2-ultra-os5   (ARMv7, MyCloudEX2Ultra only)
#   compile.sh --target wd-os5-all         (ARMv7 + amd64, all 10 models)
# Never published to any registry.

# PYTHON_VERSION and PYTHON_SHA256 are kept in sync with the upstream
# docker-library/python image (https://github.com/docker-library/python).
# Bumping the version requires updating both values.
ARG PYTHON_VERSION=3.12.13
ARG PYTHON_SHA256=c08bc65a81971c1dd5783182826503369466c7e67374d1646519adf05207b684

# Base image and target platform are parametrised so the same Dockerfile
# produces a builder for every CPU family supported by WD OS5:
#   ARMv7  (My Cloud / Mirror / EX2 Ultra / WDCloud)  → arm32v7/debian:buster
#   amd64  (EX2100 / EX4100 / DL2100 / DL4100 / PR2100 / PR4100) → amd64/debian:buster
# Both stay on Debian 10 buster on purpose: its glibc (2.28) is below the
# 2.30 ceiling exposed by stock WD OS5 firmware.
ARG BASE_IMAGE=arm32v7/debian:buster
ARG TARGET_PLATFORM=linux/arm/v7

# OpenSSL 3 source-build parameters (consumed in the builder stage below).
# cryptography >= 43 refuses to compile against OpenSSL < 3.0.0, but Debian
# Buster only ships 1.1.1; we therefore build a static OpenSSL 3 from source.
# Pinned + integrity-checked via SHA256 (mirrors the CPython pinning above).
ARG OPENSSL_VERSION=3.0.16
ARG OPENSSL_SHA256=57e03c50feab5d31b152af2b764f10379aecd8ee92f16c985983ce4a99f7ef86

# -----------------------------------------------------------------------------
# Stage 1 — compile Python 3.12 from source (cached forever)
# -----------------------------------------------------------------------------
FROM --platform=${TARGET_PLATFORM} ${BASE_IMAGE} AS python-builder

ARG PYTHON_VERSION
ARG PYTHON_SHA256

ENV DEBIAN_FRONTEND=noninteractive \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8

# Buster reached LTS end-of-life in 2024 and the regular Debian mirrors no
# longer serve it. Repoint apt to archive.debian.org, drop the no-longer
# published ``buster-updates`` suite, and disable the Valid-Until check
# since archived Release files are intentionally expired.
RUN set -eux; \
    printf '%s\n' \
        'deb http://archive.debian.org/debian buster main' \
        'deb http://archive.debian.org/debian-security buster/updates main' \
        > /etc/apt/sources.list; \
    echo 'Acquire::Check-Valid-Until "false";' \
        > /etc/apt/apt.conf.d/10no-check-valid-until

# Build dependencies required by the CPython source distribution.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        libssl-dev \
        zlib1g-dev \
        libbz2-dev \
        libreadline-dev \
        libsqlite3-dev \
        libncursesw5-dev \
        libffi-dev \
        liblzma-dev \
        uuid-dev \
        xz-utils \
        tk-dev \
    && rm -rf /var/lib/apt/lists/*

# Build & install CPython into /opt/python3.12. ``--enable-optimizations`` is
# intentionally omitted — PGO under qemu emulation is extremely slow and the
# gains are marginal for our use case (one-shot PyInstaller build).
#
# ``--with-system-ffi`` was removed in Python 3.12 (the bundled copy is now the
# only option and the flag produces a configure warning); it is omitted here.
#
# The tarball is verified against PYTHON_SHA256; this is what guarantees
# integrity, not the TLS chain. We additionally force-refresh the CA bundle
# (``update-ca-certificates --fresh``) so curl's TLS handshake succeeds even
# on the archived Buster image, where the postinst trigger may have left an
# incomplete trust store.
#
# The download is retried up to 3 times to survive transient python.org / CDN
# failures seen on GitHub Actions runners. ``make -j`` is capped at 4 jobs
# instead of ``$(nproc)``: under qemu-user emulation (ARMv7) each gcc process
# has significant memory overhead, and the 16 GB Actions runner can OOM with
# the full ``$(nproc)`` parallelism, killing the build with exit code 1.
#
# ``make`` is additionally wrapped in a retry loop (``-j4``, ``-j4``, then a
# final ``-j1``): under qemu-user emulation individual gcc invocations can crash
# with a SIGSEGV (exit code 139) on otherwise valid translation units (e.g.
# ``Python/_warnings.c``). These crashes are non-deterministic and depend on
# emulated thread scheduling / memory pressure, so simply re-running ``make``
# (which resumes from the already-produced ``.o`` files) succeeds; the ``-j1``
# fallback maximises the odds on the last attempt.
#
# ``ac_cv_sizeof_time_t`` is seeded explicitly: under qemu-user emulation the
# CPython ``configure`` runtime probe for ``sizeof(time_t)`` is unreliable and
# can leave ``SIZEOF_TIME_T`` undefined in ``pyconfig.h``. That makes both the
# ``#if SIZEOF_TIME_T == SIZEOF_LONG_LONG`` / ``== SIZEOF_LONG`` branches in
# ``Python/pytime.c`` evaluate ``SIZEOF_TIME_T`` as empty, so ``PY_TIME_T_MIN``
# is never ``#define``-d and compilation fails with
# ``error: 'PY_TIME_T_MIN' undeclared``. We instead measure the size with a
# tiny C program compiled by the (target-native under qemu) gcc and feed the
# result into configure's cache. The size is returned via the program's exit
# code, so ``|| TT_SIZE=$?`` is used to capture it without tripping ``set -e``.
RUN set -eux; \
    update-ca-certificates --fresh; \
    for attempt in 1 2 3; do \
        curl -fsSL "https://www.python.org/ftp/python/${PYTHON_VERSION}/Python-${PYTHON_VERSION}.tar.xz" \
            -o /tmp/python.tar.xz && break; \
        echo "curl attempt ${attempt}/3 failed, retrying in 5s..." >&2; \
        sleep 5; \
    done; \
    test -s /tmp/python.tar.xz; \
    echo "${PYTHON_SHA256} /tmp/python.tar.xz" | sha256sum -c -; \
    mkdir -p /tmp/python-src; \
    tar -xJf /tmp/python.tar.xz -C /tmp/python-src --strip-components=1; \
    cd /tmp/python-src; \
    printf '#include <time.h>\nint main(void){return (int)sizeof(time_t);}\n' > /tmp/tt.c; \
    gcc /tmp/tt.c -o /tmp/tt_bin; \
    TT_SIZE=0; /tmp/tt_bin || TT_SIZE=$?; \
    echo "detected sizeof(time_t) = ${TT_SIZE} bytes"; \
    test "${TT_SIZE}" = 4 -o "${TT_SIZE}" = 8; \
    ./configure \
        --prefix=/opt/python3.12 \
        --enable-shared \
        --with-ensurepip=install \
        ac_cv_sizeof_time_t="${TT_SIZE}" \
        LDFLAGS=-Wl,-rpath=/opt/python3.12/lib; \
    echo "=== pyconfig.h SIZEOF_TIME_T / SIZEOF_LONG ==="; \
    grep -E 'define SIZEOF_TIME_T|define SIZEOF_LONG' pyconfig.h || true; \
    for jobs in 4 4 1; do \
        echo "make -j${jobs} (attempt with ${jobs} job(s))"; \
        rc=0; make -j"${jobs}" || rc=$?; \
        if [ "${rc}" = 0 ]; then break; fi; \
        echo "make -j${jobs} failed with exit code ${rc}; retrying (qemu gcc segfaults are non-deterministic)..." >&2; \
        if [ "${jobs}" = 1 ]; then echo "make failed even with -j1, giving up" >&2; exit "${rc}"; fi; \
    done; \
    make install; \
    /opt/python3.12/bin/python3 -m pip install --no-cache-dir --upgrade pip setuptools wheel; \
    rm -rf /tmp/python-src /tmp/python.tar.xz

# -----------------------------------------------------------------------------
# Stage 2 — runtime builder image
# -----------------------------------------------------------------------------
FROM --platform=${TARGET_PLATFORM} ${BASE_IMAGE} AS builder

ENV DEBIAN_FRONTEND=noninteractive \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8 \
    PATH=/opt/python3.12/bin:/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=true \
    POETRY_VIRTUALENVS_CREATE=true \
    POETRY_NO_INTERACTION=1

# Same archive-mirror pivot as the python-builder stage (Buster is EOL).
RUN set -eux; \
    printf '%s\n' \
        'deb http://archive.debian.org/debian buster main' \
        'deb http://archive.debian.org/debian-security buster/updates main' \
        > /etc/apt/sources.list; \
    echo 'Acquire::Check-Valid-Until "false";' \
        > /etc/apt/apt.conf.d/10no-check-valid-until

# Runtime dependencies for the produced binary + tooling for PyInstaller +
# build dependencies needed to compile native Python extensions from source.
#
# Why the *-dev packages and perl: on ARMv7, PyPI does not publish ``armv7l``
# wheels for ``cryptography`` (a direct project dependency, pinned >=48,<50),
# so it is compiled from source — which needs ``gcc`` + ``libffi-dev`` plus a
# Rust toolchain and OpenSSL 3 (built from source below, since Buster ships
# only 1.1.1). ``perl`` is required to configure the OpenSSL source tree. On
# amd64 the binary wheels are available (manylinux2014, glibc ≥ 2.17 —
# satisfied by buster) so these build packages are unused but harmless.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        binutils \
        libsqlite3-0 \
        libbz2-1.0 \
        liblzma5 \
        libncursesw6 \
        libreadline7 \
        build-essential \
        perl \
        pkg-config \
        libffi-dev \
        libssl-dev \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Bring in the Python 3.12 install from the previous stage.
COPY --from=python-builder /opt/python3.12 /opt/python3.12

# Rust toolchain — ``cryptography`` >= 35 uses pyo3 for its native modules.
# Debian Buster ships rustc 1.41 in apt, which is too old; ``cryptography``
# 48 in particular requires rustc >= 1.83. Use rustup with the ``minimal``
# profile to keep the layer as small as possible (no docs, no rust-src, no
# rustfmt). Pinned to a known version so the cached layer is reproducible.
ENV RUSTUP_HOME=/opt/rust/rustup \
    CARGO_HOME=/opt/rust/cargo \
    PATH=/opt/rust/cargo/bin:/opt/python3.12/bin:/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUN set -eux; \
    update-ca-certificates --fresh; \
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
        | sh -s -- -y --no-modify-path --profile minimal --default-toolchain 1.84.0; \
    rustc --version; \
    cargo --version

# -----------------------------------------------------------------------------
# OpenSSL 3 — required by cryptography (>= 43 drops OpenSSL 1.1.x)
# -----------------------------------------------------------------------------
# Build a *static* OpenSSL 3 into /opt/openssl3 and point the Rust
# ``openssl-sys`` build (used by ``cryptography``) at it via OPENSSL_DIR +
# OPENSSL_STATIC=1. The compiled ``cryptography`` extension then embeds
# OpenSSL 3 statically, so the PyInstaller bundle is self-contained on the NAS
# and the CPython ``ssl`` module (linked against the system libssl 1.1.1) is
# left untouched. ``-fPIC`` is mandatory so the static archive can be linked
# into the ``_rust`` shared object.
#
# ``make -j`` is capped at 4 (same rationale as the CPython build above) to
# avoid OOM kills under qemu-user emulation on GitHub Actions runners, and is
# wrapped in the same ``-j4 / -j4 / -j1`` retry loop to survive non-deterministic
# qemu gcc SIGSEGV (exit code 139) crashes.
ARG OPENSSL_VERSION
ARG OPENSSL_SHA256
RUN set -eux; \
    update-ca-certificates --fresh; \
    curl -fsSL "https://github.com/openssl/openssl/releases/download/openssl-${OPENSSL_VERSION}/openssl-${OPENSSL_VERSION}.tar.gz" \
        -o /tmp/openssl.tar.gz; \
    echo "${OPENSSL_SHA256}  /tmp/openssl.tar.gz" | sha256sum -c -; \
    mkdir -p /tmp/openssl-src; \
    tar -xzf /tmp/openssl.tar.gz -C /tmp/openssl-src --strip-components=1; \
    cd /tmp/openssl-src; \
    ./config --prefix=/opt/openssl3 --openssldir=/opt/openssl3 \
        no-shared no-tests -fPIC; \
    for jobs in 4 4 1; do \
        echo "make -j${jobs} (attempt with ${jobs} job(s))"; \
        rc=0; make -j"${jobs}" || rc=$?; \
        if [ "${rc}" = 0 ]; then break; fi; \
        echo "make -j${jobs} failed with exit code ${rc}; retrying (qemu gcc segfaults are non-deterministic)..." >&2; \
        if [ "${jobs}" = 1 ]; then echo "make failed even with -j1, giving up" >&2; exit "${rc}"; fi; \
    done; \
    make install_sw; \
    rm -rf /tmp/openssl-src /tmp/openssl.tar.gz

# Make the static OpenSSL 3 the one cryptography's openssl-sys build resolves,
# overriding the system 1.1.1 discovered via pkg-config / /usr/include.
ENV OPENSSL_DIR=/opt/openssl3 \
    OPENSSL_STATIC=1

# Poetry — pinned to a known-good 2.x release for reproducibility. Version
# >= 2.0 is mandatory: the project declares its metadata via the PEP 621
# ``[project]`` table and the lock file is generated by Poetry 2.x; older
# 1.x releases ignore ``[project]`` and abort with "fields ... are required
# in package mode" while also rejecting the newer lock-file format.
# ``--prefer-binary`` lets pip pick wheels when available (on amd64 this
# covers most dependencies; on ARMv7 it covers msgpack, rapidfuzz and others
# that publish armv7l wheels), falling back to source builds only when
# strictly needed.
RUN /opt/python3.12/bin/python3 -m pip install --no-cache-dir --prefer-binary \
        "poetry==2.3.2"

# -----------------------------------------------------------------------------
# Project dependency + source layers
# -----------------------------------------------------------------------------
# Install all third-party dependencies into an in-project virtualenv and bake
# the application sources into the image. Doing this here (rather than on every
# `docker run`) means the slow ARMv7 native builds — cryptography (Rust),
# zstandard / cffi (C) and the PyInstaller bootloader — are compiled once and
# persisted as a cached layer.
#
# Layer ordering is deliberate: only the dependency manifests feed the heavy
# `poetry install` step, so editing application sources rebuilds just the cheap
# COPY layers below it and reuses the cached install.

# The in-project venv (/build/hsync-client/.venv) is selected via
# POETRY_VIRTUALENVS_IN_PROJECT above — env vars outrank `poetry config`, so it
# is the single source of truth — and is what the runtime `pyinstaller`
# invocation resolves on PATH. Here we only raise the installer worker count.
RUN poetry config installer.max-workers 4

# 1) Dependency manifests only (rarely change → install layer stays cached).
#    The two projects keep their relative layout so the
#    ``hsync-common = {path = "../hsync-common"}`` path dependency resolves.
COPY hsync-client/pyproject.toml hsync-client/poetry.lock hsync-client/README.md /build/hsync-client/
COPY hsync-common/pyproject.toml hsync-common/README.md /build/hsync-common/

# 2) Stub package so Poetry can build the editable (develop=true) wheel for the
#    ``hsync-common`` path dependency without busting the install cache on every
#    source edit. The real sources are layered in at the same path afterwards;
#    Poetry's editable .pth keeps resolving to whatever lives there.
RUN mkdir -p /build/hsync-common/hsync_common \
 && : > /build/hsync-common/hsync_common/__init__.py

# 3) Heavy layer: compile + install every non-dev dependency. Retries make the
#    PEP-517 source builds resilient to the slow PyPI transfers seen under qemu.
WORKDIR /build/hsync-client
ENV PIP_DEFAULT_TIMEOUT=300 \
    PIP_RETRIES=10 \
    POETRY_REQUESTS_TIMEOUT=300
RUN set -eux; \
    install_ok=0; \
    for attempt in 1 2 3; do \
        if poetry install --no-root --without dev; then install_ok=1; break; fi; \
        echo "poetry install attempt ${attempt}/3 failed, retrying in 10s..." >&2; \
        sleep 10; \
    done; \
    [ "${install_ok}" -eq 1 ]

# 4) Real application sources — cheap layers that rebuild without touching the
#    cached dependency install above.
COPY hsync-common/hsync_common /build/hsync-common/hsync_common
COPY hsync-client/hsync_client /build/hsync-client/hsync_client

# Put the in-project venv on PATH so the runtime entrypoint calls `pyinstaller`
# directly, then fail fast at build time if the editable path dependency or any
# of its submodules fail to resolve.
ENV PATH=/build/hsync-client/.venv/bin:/opt/rust/cargo/bin:/opt/python3.12/bin:/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUN python -c "import hsync_client, hsync_common, hsync_common.models, hsync_common.constants"

# The caller mounts a host directory at /out and invokes PyInstaller; the
# default command is a shell for ad-hoc inspection.
CMD ["bash"]
