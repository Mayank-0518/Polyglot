FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /usr/local/bin/uv
WORKDIR /app
ENV PYTHONUNBUFFERED=1
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY . .
# Keep the Linux environment outside /app so the Windows bind mount cannot hide it.
RUN uv sync --locked --extra test --no-cache
EXPOSE 8000
# Same image runs the API (default) or the agent: `python -m app.agent start`
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
