FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY dealbot ./dealbot
COPY data ./data
COPY run_once.py ./run_once.py
CMD ["python", "-m", "dealbot.main"]
