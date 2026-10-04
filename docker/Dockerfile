FROM ubuntu:24.04

# Prevent interactive prompts
ENV DEBIAN_FRONTEND=noninteractive

# Install core dependencies: Java 17, Python 3, ADB, and build tools
RUN apt-get update && apt-get install -y \
    openjdk-17-jdk-headless \
    python3 \
    python3-pip \
    python3-venv \
    wget \
    unzip \
    adb \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set up Android SDK environment variables
ENV ANDROID_HOME=/opt/android-sdk
ENV PATH=${PATH}:${ANDROID_HOME}/cmdline-tools/latest/bin:${ANDROID_HOME}/platform-tools:${ANDROID_HOME}/build-tools/34.0.0

# Download and install exact Android SDK Command Line Tools & build-tools (for aapt2)
RUN mkdir -p ${ANDROID_HOME}/cmdline-tools && \
    cd ${ANDROID_HOME}/cmdline-tools && \
    wget -q https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip -O cmdline-tools.zip && \
    unzip -q cmdline-tools.zip && \
    rm cmdline-tools.zip && \
    mv cmdline-tools latest && \
    yes | latest/bin/sdkmanager --licenses && \
    latest/bin/sdkmanager "build-tools;34.0.0" "platform-tools"

# Set up xTS Agent Python Environment
WORKDIR /app
COPY pyproject.toml requirements.txt ./
COPY xts_agent/ ./xts_agent/
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir -r requirements.txt

# Copy TradeFed execution scripts
COPY start_massive_nightly.sh /app/
RUN chmod +x /app/start_massive_nightly.sh

# Start the xTS Agent
CMD ["python3", "-m", "xts_agent.cli", "run", "--plan", "full_cts.yaml"]
