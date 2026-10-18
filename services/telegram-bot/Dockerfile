FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml requirements.lock ./
COPY vpars_bot ./vpars_bot
RUN pip install --no-cache-dir . -c requirements.lock && useradd --uid 10001 --create-home bot
COPY alembic.ini ./
COPY migrations ./migrations
COPY config ./config
USER bot
EXPOSE 8085
CMD ["python", "-m", "vpars_bot", "run"]
