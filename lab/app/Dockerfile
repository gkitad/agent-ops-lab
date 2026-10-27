FROM python:3.12-slim
RUN pip install --no-cache-dir "psycopg[binary]==3.2.3"
COPY app.py /app/app.py
EXPOSE 8080
CMD ["python", "-u", "/app/app.py"]
