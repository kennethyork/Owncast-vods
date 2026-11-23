FROM python:3.12-alpine
RUN apk add --no-cache ffmpeg tzdata
WORKDIR /app
COPY recorder.py .
VOLUME /vods
EXPOSE 8090
CMD ["python3", "-u", "recorder.py"]
