# phase-sim: rules-accurate four-player Commander games with phase-rs's headless AI (github.com/phase-rs/phase), behind a small HTTP service.
#
# phase is a Rust rules engine (stack, priority, combat, layers, triggers, replacement effects), dual licensed Apache-2.0 / MIT, with card
# data from MTGJSON (MIT). Pinned to release v0.118.0 (commit ad0de6a) and to the card database that release was published with
# (`card-data-5e541a38c99b97f0.json`, named in data.phase-rs.dev/card-data-meta.json for that commit); the file's SHA-256 is checked, so a
# changed file fails the build instead of silently changing results. Nothing is fetched at run time, and the pod has no network.
#
# Built: the `ai-commander` binary (4-player Commander, batch mode) and `ai-duel` with the repo's `server-release` profile (unwinding
# panics, so one game's panic cannot kill a batch). The toolchain is whatever rust-toolchain.toml pins (a dated nightly).
FROM rust:slim-bookworm AS build

ARG PHASE_REPO=https://github.com/phase-rs/phase.git
ARG PHASE_TAG=v0.118.0
ARG PHASE_SHA=ad0de6adf00153a6e6eb89604f6670be5b1d5168
ARG CARD_DATA_URL=https://data.phase-rs.dev/card-data-5e541a38c99b97f0.json
ARG CARD_DATA_SHA256=5e541a38c99b97f0ef12c81f7b249e7cd253f38b32bd5c156cf19608cb6f1218

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates curl build-essential pkg-config python3 \
 && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    mkdir /phase; cd /phase; \
    git init -q; \
    git remote add origin "$PHASE_REPO"; \
    git fetch -q --depth 1 origin "$PHASE_SHA"; \
    git checkout -q FETCH_HEAD; \
    test "$(git rev-parse HEAD)" = "$PHASE_SHA"

WORKDIR /phase
# phase's server-release profile uses thin LTO and 16 code-generation units; compiling its big engine crate that way ran a 7 GB CI machine
# out of memory ("cannot allocate memory", 2026-10-27). LTO off and two parallel jobs trade some build time (and a few percent of run
# speed) for a build that fits; the optimisation level and panic=unwind (which batch isolation needs) are unchanged.
ENV CARGO_BUILD_JOBS=2 \
    CARGO_PROFILE_SERVER_RELEASE_LTO=off \
    CARGO_PROFILE_SERVER_RELEASE_CODEGEN_UNITS=8
RUN cargo build --profile server-release -p phase-ai --bin ai-commander --bin ai-duel

RUN set -eux; \
    curl -fsSL --retry 3 -o /card-data.json "$CARD_DATA_URL"; \
    echo "$CARD_DATA_SHA256  /card-data.json" | sha256sum -c -; \
    cp /phase/client/public/feeds/mtggoldfish-commander.json /sample-feed.json

FROM python:3.12-slim-bookworm
ARG PHASE_TAG=v0.118.0

RUN groupadd -g 10001 phase && useradd -u 10001 -g 10001 -d /tmp -s /usr/sbin/nologin phase
COPY --from=build /phase/target/server-release/ai-commander /opt/phase/ai-commander
COPY --from=build /phase/target/server-release/ai-duel /opt/phase/ai-duel
COPY --from=build /card-data.json /opt/phase/data/card-data.json
COPY --from=build /sample-feed.json /opt/phase/sample-feed.json
COPY phase_sim.py phase_smoke.py /opt/phase/

ENV PHASE_ENGINE_VERSION="phase-rs ${PHASE_TAG}" \
    PHASE_AI_COMMANDER=/opt/phase/ai-commander \
    PHASE_CARD_DATA=/opt/phase/data/card-data.json \
    PYTHONUNBUFFERED=1 PORT=8090
USER 10001
WORKDIR /tmp
EXPOSE 8090
CMD ["python", "/opt/phase/phase_sim.py"]
