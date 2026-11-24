# MTRobo classroom hub (issue #1): Python standard library only, no pip install
FROM python:3.12-slim
WORKDIR /app
COPY wsproto.py hub.py ./
USER nobody
EXPOSE 9000
CMD ["python", "-u", "hub.py"]
