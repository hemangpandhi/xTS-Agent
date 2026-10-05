FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-17-jdk-headless \
    python3 \
    python3-pip \
    python3-venv \
    wget \
    unzip \
    adb \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV ANDROID_HOME=/opt/android-sdk
ENV ANDROID_SDK_ROOT=/opt/android-sdk
ENV PATH=${PATH}:${ANDROID_HOME}/cmdline-tools/latest/bin:${ANDROID_HOME}/platform-tools:${ANDROID_HOME}/build-tools/34.0.0

RUN mkdir -p ${ANDROID_HOME}/cmdline-tools && \
    cd ${ANDROID_HOME}/cmdline-tools && \
    wget -q https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip -O cmdline-tools.zip && \
    unzip -q cmdline-tools.zip && \
    rm cmdline-tools.zip && \
    mv cmdline-tools latest && \
    yes | latest/bin/sdkmanager --licenses >/dev/null && \
    latest/bin/sdkmanager "build-tools;34.0.0" "platform-tools"

WORKDIR /app

# Build context MUST be the repository root:
#   docker build -f docker/Dockerfile .
COPY pyproject.toml requirements.txt README.md ./
COPY xts_agent/ ./xts_agent/
COPY config/ ./config/
COPY scripts/ ./scripts/

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir -U pip && pip install --no-cache-dir -e .

# Non-root friendly defaults; devices still require host ADB via network_mode=host
ENV XTS_PACKAGES_DIR=/opt/xts
VOLUME ["/opt/xts", "/app/results"]

# Default to hardware-oriented CTS plan; override for virtual (full_cts.yaml).
CMD ["python3", "-m", "xts_agent.cli", "run", \
     "--plan", "config/test_plans/cts_only.yaml", \
     "--config", "config/default_config.yaml"]
