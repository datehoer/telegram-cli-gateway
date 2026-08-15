# syntax=docker/dockerfile:1.7

FROM node:22-bookworm-slim AS gateway-base

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git python3 tmux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/gateway
COPY . /opt/gateway
COPY docker/experiments/entrypoint.sh /usr/local/bin/experiment-entrypoint

RUN chmod 0755 /usr/local/bin/experiment-entrypoint \
    && mkdir -p /opt/gateway/.runtime /home/agent /workspace \
    && chown -R node:node /opt/gateway/.runtime /home/agent /workspace

ENV HOME=/home/agent \
    PYTHONPATH=/opt/gateway/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["/usr/local/bin/experiment-entrypoint"]
CMD ["python3", "-m", "cli_telegram_gateway"]

FROM gateway-base AS codex-agent
COPY --from=codex_cli . /opt/codex
RUN ln -s /opt/codex/bin/codex.js /usr/local/bin/codex
USER node

FROM gateway-base AS claude-agent
COPY --from=claude_cli /2.1.223 /usr/local/bin/claude
RUN chmod 0755 /usr/local/bin/claude
USER node

FROM gateway-base AS grok-agent
COPY --from=grok_cli /grok-1.0.4-linux-x86_64 /usr/local/bin/grok
RUN chmod 0755 /usr/local/bin/grok
USER node
