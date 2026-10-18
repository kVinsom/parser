FROM python:3.12.14-slim AS contracts
WORKDIR /build
RUN pip install --no-cache-dir grpcio-tools==1.84.0 protobuf==7.36.1
COPY services/access-gateway/bypass/proto/ proto/
RUN python -m grpc_tools.protoc -Iproto --python_out=. --grpc_python_out=. proto/vinted_rpc/access.proto

FROM python:3.12.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    PYTHONPATH=/app/services/telegram-bot:/app/services/worker:/app/services/parser:/app/services/notifier:/app/shared
WORKDIR /app
COPY shared/ shared/
COPY services/telegram-bot/ services/telegram-bot/
COPY services/parser/ services/parser/
RUN pip install --no-cache-dir ./shared './services/telegram-bot[autobuy]' -c services/telegram-bot/requirements-autobuy.lock \
    && cd services/parser && pip install --no-cache-dir -r requirements.txt
RUN python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home vpars
COPY services/worker/ services/worker/
COPY services/notifier/ services/notifier/
COPY --from=contracts /build/vinted_rpc/ services/parser/.generated/vinted_rpc/
COPY deploy/service.py /app/deploy/service.py
COPY deploy/state_archive.py /app/deploy/state_archive.py
RUN mkdir -p services/worker/.artifacts services/telegram-bot/.wallet-profiles services/telegram-bot/.wallet-contracts \
    && chown -R 10001:10001 services/worker/.artifacts services/telegram-bot/.wallet-profiles services/telegram-bot/.wallet-contracts
USER 10001:10001
WORKDIR /app/services/telegram-bot
ENTRYPOINT ["python", "/app/deploy/service.py"]
CMD ["bot"]
