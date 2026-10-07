FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# No distro `adb`: its version rarely matches the host adb server, and a
# version-mismatched client restarts the host server mid-run.
RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-17-jdk-headless \
    python3 \
    python3-pip \
    python3-venv \
    wget \
    unzip \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV ANDROID_HOME=/opt/android-sdk
ENV ANDROID_SDK_ROOT=/opt/android-sdk

# Pinned and checksum-verified Android command-line tools
ARG CMDLINE_TOOLS_VERSION=11076708
ARG CMDLINE_TOOLS_SHA256=2d2d50857e4eb553af5a6dc3ad507a17adf43d115264b1afc116f95c92e5e258
ARG BUILD_TOOLS_VERSION=34.0.0
RUN mkdir -p ${ANDROID_HOME}/cmdline-tools && \
    cd ${ANDROID_HOME}/cmdline-tools && \
    wget -q "https://dl.google.com/android/repository/commandlinetools-linux-${CMDLINE_TOOLS_VERSION}_latest.zip" -O cmdline-tools.zip && \
    echo "${CMDLINE_TOOLS_SHA256}  cmdline-tools.zip" | sha256sum -c - && \
    unzip -q cmdline-tools.zip && \
    rm cmdline-tools.zip && \
    mv cmdline-tools latest && \
    yes | latest/bin/sdkmanager --licenses >/dev/null && \
    latest/bin/sdkmanager "build-tools;${BUILD_TOOLS_VERSION}" "platform-tools"

# /host-platform-tools (mounted from the host) comes first so the container's
# adb client is the same binary that runs the host adb server; the SDK copy
# is only a fallback.
ENV PATH=/host-platform-tools:/opt/venv/bin:${PATH}:${ANDROID_HOME}/build-tools/${BUILD_TOOLS_VERSION}:${ANDROID_HOME}/platform-tools

WORKDIR /app

# Build context MUST be the repository root:
#   docker build -f docker/Dockerfile .
# Dependencies first (exact, hash-checked versions from the lock) so code
# changes do not invalidate this layer
COPY requirements.lock ./
RUN python3 -m venv /opt/venv && \
    pip install --no-cache-dir --require-hashes -r requirements.lock

COPY pyproject.toml README.md ./
COPY xts_agent/ ./xts_agent/
COPY config/ ./config/
COPY scripts/ ./scripts/
RUN pip install --no-cache-dir --no-deps .

# Run as an unprivileged user (UID/GID overridable to match host file ownership)
ARG XTS_UID=1000
ARG XTS_GID=1000
# ubuntu:24.04 ships user "ubuntu" at 1000; drop any user holding the UID first
RUN if getent passwd ${XTS_UID} >/dev/null; then userdel -r "$(getent passwd ${XTS_UID} | cut -d: -f1)" 2>/dev/null || true; fi && \
    (getent group ${XTS_GID} >/dev/null || groupadd -g ${XTS_GID} xts) && \
    useradd -m -u ${XTS_UID} -g ${XTS_GID} -s /usr/sbin/nologin xts && \
    mkdir -p /app/results /app/logs && \
    chown -R ${XTS_UID}:${XTS_GID} /app/results /app/logs
USER xts

ENV XTS_PACKAGES_DIR=/opt/xts
VOLUME ["/opt/xts", "/app/results"]

# Default to hardware-oriented CTS plan; override for virtual (full_cts.yaml).
CMD ["python3", "-m", "xts_agent.cli", "run", \
     "--plan", "config/test_plans/cts_only.yaml", \
     "--config", "config/default_config.yaml"]
