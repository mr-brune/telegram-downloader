# Use an official Python runtime as a parent image (Alpine version)
FROM python:3.11-alpine

# Install necessary packages
RUN apk update && apk add --no-cache curl ca-certificates

# Add and run the UV installer script, then move uv to a global path
ADD https://astral.sh/uv/0.5.9/install.sh /uv-installer.sh
RUN sh /uv-installer.sh && \
    mv /root/.local/bin/uv /usr/local/bin/uv && \
    mv /root/.local/bin/uvx /usr/local/bin/uvx && \
    rm /uv-installer.sh

# Set working directory in the container to /app
WORKDIR /app

# Enable bytecode compilation
ENV UV_COMPILE_BYTECODE=1

# Install the project's dependencies using the lockfile and settings
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev

# Add the rest of the application code
ADD . /app

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Crea la cartella (se non esiste), assegna 1000:1000, poi avvia Python come root
CMD mkdir -p /app/downloads && \
    chown -R 1000:1000 /app/downloads && \
    uv run python run.py