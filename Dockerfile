# Agent Jo - web app container.
# Build:  docker build -t agent-jo .
# Run:    docker run -p 8000:8000 -e ANTHROPIC_API_KEY=sk-... -v agentjo:/data agent-jo
#
# Data (memory DB, engines.json, settings) lives in /data so it survives
# restarts. Set ANTHROPIC_API_KEY (and/or AGENT_DEEPSEEK_KEY, etc.) at run time.
FROM python:3.12-slim

WORKDIR /app

# System deps kept minimal; add build-essential only if a wheel needs compiling.
COPY requirements.txt requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements-web.txt

COPY agent ./agent
COPY web ./web
COPY agent_avatar.png ./agent_avatar.png

ENV AGENT_HOME=/data \
    AGENT_BACKEND=anthropic \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8000

# One worker by design: conversation history is in-process for now (see the
# README "Production web app" notes before scaling to multiple workers).
CMD ["uvicorn", "web.server:app", "--host", "0.0.0.0", "--port", "8000"]
