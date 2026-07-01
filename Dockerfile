FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tg_lead_skill.py .

CMD ["python", "tg_lead_skill.py"]
