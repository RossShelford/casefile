FROM python:3.12-slim

WORKDIR /app
# Tesseract reads scanned PDFs (English data; add tesseract-ocr-<lang> packages for others).
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY casefile ./casefile
RUN pip install --no-cache-dir . && useradd --create-home casefile && mkdir /data && chown casefile /data

USER casefile
ENV PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8765

# The model runs on the host (Ollama). Publish the port to the host only, then open the
# link printed in `docker logs` (it carries the secret casefile needs):
#   docker run -p 127.0.0.1:8765:8765 -v casefile:/data casefile
CMD ["casefile", "serve", "--host", "0.0.0.0", "--url", "http://host.docker.internal:11434", \
     "--data", "/data/jobs", "--cache", "/data/parts"]
