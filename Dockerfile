# MCP server as a long-running service: streamable HTTP on port 8765 at /mcp.
# Settings come from the environment (HA_URL, HA_TOKEN, MCP_TOKEN), never the image.
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir '.[mcp]' && rm -rf /app/src /app/build

ENV PYTHONUNBUFFERED=1
USER nobody
EXPOSE 8765
ENTRYPOINT ["ha-llm-context", "mcp", "--http", "--host", "0.0.0.0", "--port", "8765"]
# Extra options go here or in compose's `command:`, e.g. ["--tz", "America/New_York"]
CMD []
