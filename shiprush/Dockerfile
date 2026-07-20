FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY client.py .
COPY config.py .
COPY resources.py .
COPY writer.py .
COPY main.py .

CMD ["python", "main.py"]
