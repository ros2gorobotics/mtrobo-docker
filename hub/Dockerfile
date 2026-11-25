# MTRobo classroom hub (issue #1): Python standard library only, no pip install
FROM python:3.12-slim
WORKDIR /app
COPY wsproto.py hub.py db.py schema.sql ./
# classroom database (SQLite) and its daily backups; a named volume keeps them across image updates
RUN mkdir -p /data/backup && chown -R nobody /data
USER nobody
EXPOSE 9000
CMD ["python", "-u", "hub.py"]
