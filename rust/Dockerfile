FROM rust:1-slim AS builder

WORKDIR /app
COPY Cargo.toml Cargo.lock ./
COPY schema.sql ./
COPY src ./src

RUN cargo build --release

FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -d /app -s /usr/sbin/nologin appuser

WORKDIR /app
COPY --from=builder /app/target/release/misis-rasp-core /usr/local/bin/misis-rasp-bot
RUN mkdir -p /app/runtime && chown -R appuser:appgroup /app

USER appuser

ENV DATABASE_PATH=/app/runtime/bot.db \
    TZ=Europe/Moscow
EXPOSE 8000 8080

CMD ["/usr/local/bin/misis-rasp-bot"]
