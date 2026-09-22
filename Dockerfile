FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Indexed repos are bind-mounted and owned by another uid; without this git
# refuses them as "dubious ownership" and we silently lose branch/HEAD.
RUN git config --global --add safe.directory "*"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY forge ./forge
ENV PYTHONUNBUFFERED=1 FORGE_DATA=/data
VOLUME /data
EXPOSE 8910
CMD ["uvicorn", "forge.api.app:app", "--host", "0.0.0.0", "--port", "8910"]
