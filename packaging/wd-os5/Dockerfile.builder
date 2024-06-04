# syntax=docker/dockerfile:1.6
#
# HSYNC — WD My Cloud OS5 builder image (ARMv7 / amd64)
#
# Multi-stage build that produces a debian:buster (glibc 2.28) environment
# containing Python 3.12 and Poetry, ready to invoke PyInstaller on the
# project sources mounted at /repo.
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
# The tarball is verified against PYTHON_SHA256; this is what guarantees
# integrity, not the TLS chain. We additionally force-refresh the CA bundle
# (``update-ca-certificates --fresh``) so curl's TLS handshake succeeds even
# on the archived Buster image, where the postinst trigger may have left an
# incomplete trust store.
RUN set -eux; \
    update-ca-certificates --fresh; \
    curl -fsSL "https://www.python.org/ftp/python/${PYTHON_VERSION}/Python-${PYTHON_VERSION}.tar.xz" \
        -o /tmp/python.tar.xz; \
    echo "${PYTHON_SHA256} /tmp/python.tar.xz" | sha256sum -c -; \
    mkdir -p /tmp/python-src; \
    tar -xJf /tmp/python.tar.xz -C /tmp/python-src --strip-components=1; \
    cd /tmp/python-src; \
    ./configure \
        --prefix=/opt/python3.12 \
        --enable-shared \
        --with-system-ffi \
        --with-ensurepip=install \
        LDFLAGS=-Wl,-rpath=/opt/python3.12/lib; \
    make -j"$(nproc)"; \
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
    POETRY_VIRTUALENVS_IN_PROJECT=0 \
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
# Why the *-dev packages: on ARMv7, PyPI does not publish ``armv7l`` wheels
# for ``cryptography`` (a transitive dependency of ``poetry`` via
# keyring → SecretStorage), so it must be compiled from source — which
# requires ``gcc`` + ``libffi-dev`` + ``libssl-dev``.  On amd64 the binary
# wheels are available (manylinux2014, glibc ≥ 2.17 — satisfied by buster)
# so the dev packages are unused but harmless.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        binutils \
        upx-ucl \
        libsqlite3-0 \
        libbz2-1.0 \
        liblzma5 \
        libncursesw6 \
        libreadline7 \
        git \
        build-essential \
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

# Poetry — pinned to a known-good 1.x release for reproducibility.
# ``--prefer-binary`` lets pip pick wheels when available (on amd64 this
# covers most dependencies; on ARMv7 it covers msgpack, rapidfuzz and others
# that publish armv7l wheels), falling back to source builds only when
# strictly needed.
RUN /opt/python3.12/bin/python3 -m pip install --no-cache-dir --prefer-binary \
        "poetry==1.8.3"

# Workdir is mounted by the caller (``-v "$PWD:/src"``).
WORKDIR /src

# Default command is a shell so the entrypoint can be overridden from the
# host. ``compile.sh`` invokes a one-shot ``bash -lc '…'`` to build.
CMD ["bash"]
