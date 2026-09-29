FROM python:3.12-slim

# Зеркало Debian: с deb.debian.org сборка из Китая занимает часы (см. docs/deployment-china.md)
RUN sed -i s@deb.debian.org@mirrors.aliyun.com@g /etc/apt/sources.list.d/debian.sources || true; \
    apt-get update && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

# JS runtime для yt-dlp: без него извлечение YouTube объявлено устаревшим
# и часть форматов не отдаётся (см. https://github.com/yt-dlp/yt-dlp/wiki/EJS)
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot/ bot/

CMD ["python", "-m", "bot"]
