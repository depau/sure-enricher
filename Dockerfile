FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra postgres --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --extra postgres
RUN useradd -u 1000 -m spe && mkdir /data && chown spe /data
USER spe
ENV DATABASE_URL=sqlite:////data/spe.db SPE_LISTEN=0.0.0.0:8790 PATH=/app/.venv/bin:$PATH
VOLUME /data
EXPOSE 8790
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8790/readyz')"
CMD ["spe", "serve"]
