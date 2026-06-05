# App image for the overnight fleet. Includes Node so npx-based MCP servers (Jira/Atlassian,
# Reddit-style stdio servers) and the Claude Agent SDK's tooling are available at runtime.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl git ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml README.md ./
COPY overnight_eng ./overnight_eng
RUN uv pip install --system --no-cache .

EXPOSE 8080
CMD ["overnight", "serve"]
