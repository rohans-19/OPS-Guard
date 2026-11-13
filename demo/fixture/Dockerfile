FROM python:3.12-alpine
ARG APP_VERSION=v1
WORKDIR /app
COPY app.py /app/app.py
RUN printf '%s' "$APP_VERSION" > /app/version
USER 10001
CMD ["python", "-u", "/app/app.py"]
