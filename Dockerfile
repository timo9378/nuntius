FROM python:3.13-slim

# Dependencies before the source, so that editing a cog does not reinstall
# discord.py.
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The mapping files live here. Mount a volume over it — without one, every
# thread-to-issue mapping is lost when the container is replaced, and the
# GitHub side of every open thread goes quiet.
VOLUME ["/app/data"]

# Not root. There is nothing in here that needs it, and this process is
# reachable from the internet by design (GitHub has to be able to POST to it).
RUN useradd --create-home --uid 10001 nuntius \
    && mkdir -p /app/data \
    && chown -R nuntius:nuntius /app
USER nuntius

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).status == 200 else 1)"

CMD ["python", "bot.py"]
