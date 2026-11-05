# The base image is set right here: since Supervisor 2026.04 no BUILD_FROM is
# passed any more and build.yaml is only read for backwards compatibility.
# Multi-arch image (amd64, aarch64), Alpine 3.22 with Python 3.12.14 - Python
# stays on 3.12, so the pinned wheels below keep working. Alpine 3.21 is no
# longer rebuilt by Home Assistant and reaches end of life on 2026-11-01.
FROM ghcr.io/home-assistant/base:3.22

ARG BUILD_ARCH
ARG BUILD_VERSION
LABEL io.hass.type="app" \
      io.hass.version="${BUILD_VERSION}" \
      io.hass.arch="${BUILD_ARCH}" \
      org.opencontainers.image.title="MyHome SCS Bridge"

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apk add --no-cache python3 py3-pip

# Pinned wheels only: paho-mqtt is pure Python, PyYAML ships musllinux wheels
# for aarch64 and amd64 - nothing is compiled on the device.
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages --root-user-action=ignore \
        --only-binary=:all: -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

WORKDIR /opt/bridge
COPY app/ /opt/bridge/app/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
