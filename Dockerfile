FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY casefile ./casefile
RUN pip install --no-cache-dir . && useradd --create-home casefile && mkdir /data && chown casefile /data

USER casefile
ENV PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8765

# The model runs on the host (Ollama). Publish the port to the host only:
#   docker run -p 127.0.0.1:8765:8765 -v casefile:/data casefile
CMD ["casefile", "serve", "--host", "0.0.0.0", "--url", "http://host.docker.internal:11434", \
     "--data", "/data/jobs", "--cache", "/data/parts"]
