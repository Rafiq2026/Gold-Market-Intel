# Gold Market Intelligence - cloud web dashboard (pure Python stdlib).
FROM python:3.12-slim
WORKDIR /app
COPY . /app
ENV GBAI_HOST=0.0.0.0
# The host (Render/Railway/Fly) injects $PORT; web_server.py reads it.
EXPOSE 8008
CMD ["python", "web_server.py"]
