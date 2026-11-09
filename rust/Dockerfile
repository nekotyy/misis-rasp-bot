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

WORKDIR /app
COPY --from=builder /app/target/release/misis-rasp-core /usr/local/bin/misis-rasp-bot
RUN mkdir -p /app/runtime

ENV DATABASE_PATH=/app/runtime/bot.db \
    TZ=Europe/Moscow
EXPOSE 8000 8080

CMD ["/usr/local/bin/misis-rasp-bot"]
