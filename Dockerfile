FROM python:3.13-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir ".[postgres]"
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "materials_pipeline"]
