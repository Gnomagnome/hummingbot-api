# syntax=docker/dockerfile:1.7

# Stage 1: install the architecture-specific Conda and Python lock without
# consulting a package solver. The pinned base already contains git; compilers,
# headers, make, and libusb are supplied by the lock, so APT is not involved.
FROM continuumio/miniconda3:26.5.3-1@sha256:1808b31ef43e9c521cde5884ba4df9ec26d8d503a314cea787590f8550358a63 AS builder

ARG TARGETARCH
ARG FORK_SOURCE_SHA
ARG DEPENDENCY_LOCK_SHA256
ARG HUMMINGBOT_CORE_SHA=2bfaccc48dd49e71a5b6d9b3011808e127dd00cd

WORKDIR /build
COPY locks ./locks

RUN test "${FORK_SOURCE_SHA}" != "" \
    && test "${DEPENDENCY_LOCK_SHA256}" != "" \
    && echo "${FORK_SOURCE_SHA}" | grep -Eq '^[0-9a-f]{40}$' \
    && echo "${DEPENDENCY_LOCK_SHA256}" | grep -Eq '^[0-9a-f]{64}$' \
    && test "${HUMMINGBOT_CORE_SHA}" = "2bfaccc48dd49e71a5b6d9b3011808e127dd00cd" \
    && case "${TARGETARCH}" in \
        amd64) LOCK_PLATFORM="linux-64" ;; \
        arm64) LOCK_PLATFORM="linux-aarch64" ;; \
        *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && LOCK_FILE="locks/conda-${LOCK_PLATFORM}.lock" \
    && conda create --yes --copy --prefix /opt/conda/envs/hummingbot-api --file "${LOCK_FILE}" \
    && sed -n 's/^# pip //p' "${LOCK_FILE}" > /tmp/pip.lock \
    && CORE_REQUIREMENT="hummingbot @ git+https://github.com/hummingbot/hummingbot.git@${HUMMINGBOT_CORE_SHA}" \
    && test "$(grep -Fxc "${CORE_REQUIREMENT}" /tmp/pip.lock)" = "1" \
    && grep -Fvx "${CORE_REQUIREMENT}" /tmp/pip.lock > /tmp/pip-dependencies.lock \
    && grep -Fx "${CORE_REQUIREMENT}" /tmp/pip.lock > /tmp/hummingbot-core.lock \
    && conda run --prefix /opt/conda/envs/hummingbot-api \
        python -m pip install --disable-pip-version-check --no-cache-dir \
        --no-deps --no-build-isolation --requirement /tmp/pip-dependencies.lock \
    && conda run --prefix /opt/conda/envs/hummingbot-api \
        python -m pip install --disable-pip-version-check --no-cache-dir \
        --no-deps --no-build-isolation --requirement /tmp/hummingbot-core.lock \
    && conda run --prefix /opt/conda/envs/hummingbot-api python -m pip check \
    && conda clean --all --yes \
    && rm -f /tmp/pip.lock /tmp/pip-dependencies.lock /tmp/hummingbot-core.lock

# Stage 2: immutable runtime base plus the already-resolved environment.
FROM continuumio/miniconda3:26.5.3-1@sha256:1808b31ef43e9c521cde5884ba4df9ec26d8d503a314cea787590f8550358a63

ARG FORK_SOURCE_SHA
ARG DEPENDENCY_LOCK_SHA256
ARG HUMMINGBOT_CORE_SHA=2bfaccc48dd49e71a5b6d9b3011808e127dd00cd

LABEL org.opencontainers.image.source="https://github.com/Gnomagnome/hummingbot-api" \
      org.opencontainers.image.revision="${FORK_SOURCE_SHA}" \
      org.opencontainers.image.base.name="docker.io/continuumio/miniconda3:26.5.3-1" \
      org.opencontainers.image.base.digest="sha256:1808b31ef43e9c521cde5884ba4df9ec26d8d503a314cea787590f8550358a63" \
      io.condor.hummingbot-core.revision="${HUMMINGBOT_CORE_SHA}" \
      io.condor.dependency-lock.sha256="${DEPENDENCY_LOCK_SHA256}"

COPY --from=builder /opt/conda/envs/hummingbot-api /opt/conda/envs/hummingbot-api

WORKDIR /hummingbot-api

COPY main.py config.py deps.py ./
COPY models ./models
COPY routers ./routers
COPY services ./services
COPY utils ./utils
COPY database ./database
COPY bots/controllers ./bots/controllers
COPY bots/scripts ./bots/scripts

RUN mkdir -p bots/instances bots/conf bots/credentials bots/data bots/archived

EXPOSE 8000

ENV PATH="/opt/conda/envs/hummingbot-api/bin:$PATH" \
    CONDA_DEFAULT_ENV="hummingbot-api"

ENTRYPOINT ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
