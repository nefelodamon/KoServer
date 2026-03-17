ARG BUILD_FROM
FROM ${BUILD_FROM}

# Install dependencies
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY app/ ./app/

# Ensure data directory exists
RUN mkdir -p /data/portraits /data/db

EXPOSE 8099

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8099"]
