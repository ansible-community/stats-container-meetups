FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sync_meetups.py .
ENTRYPOINT ["python", "sync_meetups.py"]
CMD ["--config", "/config/config.yml"]
