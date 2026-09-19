FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sheet_to_drive.py .

# Polls the sheet every 5 minutes. Credentials come from env vars.
CMD ["python", "-u", "sheet_to_drive.py", "--watch", "5"]
