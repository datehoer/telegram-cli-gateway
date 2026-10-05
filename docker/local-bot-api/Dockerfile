FROM debian:bookworm-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git cmake g++ make gperf libssl-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
ARG BOT_API_REVISION=e3e9dd8e5b3d7ab8537cd5a10dc31d5ffa8f82d1
RUN git clone https://github.com/tdlib/telegram-bot-api.git /src \
    && cd /src && git checkout "$BOT_API_REVISION" \
    && git submodule update --init --recursive
RUN cmake -S /src -B /build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/install \
    && cmake --build /build --target install --parallel 2

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates libssl3 zlib1g libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /install/bin/telegram-bot-api /usr/local/bin/telegram-bot-api
USER 1000:1000
ENTRYPOINT ["telegram-bot-api"]
