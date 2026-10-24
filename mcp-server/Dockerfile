# MCP server for the Proxmox Dashboard (Streamable HTTP by default; stdio also works).
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 MCP_TRANSPORT=http MCP_PORT=8765
WORKDIR /app
COPY pyproject.toml ./
COPY pve_dashboard_mcp ./pve_dashboard_mcp
RUN pip install . && useradd --system --uid 10002 mcp
USER mcp
EXPOSE 8765
CMD ["pve-dashboard-mcp"]
