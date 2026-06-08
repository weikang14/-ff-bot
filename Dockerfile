FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ff_bot.py .

CMD ["python", "-u", "ff_bot.py"]
