FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY scripts ./scripts
ENV DB_PATH=/data/attendance.db SELFIE_DIR=/data/selfies RAW_LOG=/data/fkweb_raw.log
EXPOSE 8000 80
# One worker on purpose: live feed and login throttling are in-process.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
