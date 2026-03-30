# Onion Shell — Web UI container
# NOTE: The watcher (screen capture) must run on the host.
#       This container handles the web interface + AI queries only.
#
# Usage:
#   docker build -t onion-shell .
#   docker run -p 7070:7070 \
#     -v ~/.onion_shell:/root/.onion_shell \
#     -e ONION_WEB_ONLY=1 \
#     onion-shell

FROM python:3.11-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    xclip xsel \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps (no ocrmac/mss in container — web-only mode)
COPY requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements-web.txt

# Copy project
COPY . .

ENV ONION_WEB_ONLY=1
ENV ONION_PORT=7070

EXPOSE 7070

CMD ["python3", "web_app.py"]
