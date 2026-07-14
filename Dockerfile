FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# introspect_schema.py intentionally excluded — development-only tool
COPY sync_meetups.py .
RUN useradd --create-home meetup
USER meetup
ENTRYPOINT ["python", "sync_meetups.py"]
CMD ["--config", "/config/config.yml"]
