FROM python:3.12-slim

# Capture the version at build time
ARG BUILD_VERSION=dev
ENV KOSERVER_VERSION=${BUILD_VERSION}

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

RUN mkdir -p /data/portraits /data/db

EXPOSE 8099

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8099"]
