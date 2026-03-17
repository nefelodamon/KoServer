ARG BUILD_FROM
FROM ${BUILD_FROM}

# Capture the version at build time
ARG BUILD_VERSION=dev
ENV KOSERVER_VERSION=${BUILD_VERSION}

# Install Python and pip (base image is Alpine)
RUN apk add --no-cache python3 py3-pip

# Install dependencies
WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

# Copy application
COPY app/ ./app/

# Ensure data directory exists
RUN mkdir -p /data/portraits /data/db

EXPOSE 8099

CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8099"]
