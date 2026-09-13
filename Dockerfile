FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY autobooker_browser.py .
CMD ["python", "autobooker_browser.py"]
