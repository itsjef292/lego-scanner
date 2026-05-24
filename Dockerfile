# Use Python 3.9 slim image
FROM python:3.9-slim

# Set working directory
WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app files
COPY app.py .
COPY templates/ templates/
COPY static/ static/

# Set environment variables
ENV FLASK_APP=app.py
ENV PYTHONUNBUFFERED=1

# Expose port (Cloud Run uses 8080)
EXPOSE 8080

# Run with gunicorn
CMD exec gunicorn --bind :8080 --workers 4 --threads 2 --worker-class gthread --timeout 60 app:app
