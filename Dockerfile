# syntax=docker/dockerfile:1
FROM python:3.13-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ZTB_DATA_DIR=/data \
    ZTB_PORT=8765

WORKDIR /app
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements.lock ./
RUN python -m pip install --no-cache-dir -r requirements.lock \
    && python -c "import ensurepip, pathlib, shutil; shutil.rmtree(pathlib.Path(ensurepip.__file__).parent)" \
    && python -m pip uninstall --yes pip \
    && groupadd --gid 10001 ztb \
    && useradd --uid 10001 --gid 10001 --no-create-home ztb \
    && mkdir -p /data \
    && chown 10001:10001 /data

# .dockerignore permits only reviewed runtime files into the build context.
COPY . /app/
USER 10001:10001
EXPOSE 8765
STOPSIGNAL SIGINT
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "/app/docker/healthcheck.py"]
ENTRYPOINT ["python", "/app/app.py"]
CMD ["--bind", "0.0.0.0", "--no-browser"]
