# Telegram yt-dlp Bot

Telegram-бот для скачивания видео и аудио с YouTube и других сайтов. Показывает все доступные форматы через inline-кнопки, скачивает выбранный и отправляет файл прямо в чат.

Поддерживает файлы до 2 ГБ благодаря локальному Telegram Bot API серверу.

## Возможности

- Скачивание с YouTube, VK, RuTube и [1000+ других сайтов](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md)
- Выбор формата: видео+аудио, только видео, только аудио
- Автоматическое объединение видео и аудио дорожек для video-only форматов
- Прогресс-бар при скачивании
- Пагинация форматов по 8 на страницу
- Лимит 2 параллельных скачивания на пользователя
- Автоочистка временных файлов

## Требования

- VDS с Linux (Ubuntu 22.04+ / Debian 12+ рекомендуется)
- Минимум 1 ГБ RAM, 2 ГБ свободного места на диске (+ место под загрузки)
- Docker и Docker Compose

## Подготовка

### 1. Создать бота в Telegram

1. Открыть [@BotFather](https://t.me/BotFather) в Telegram
2. Отправить `/newbot`, задать имя и username
3. Скопировать токен вида `123456:ABC-DEF...`

### 2. Получить API ID и API Hash

Эти данные нужны для локального Telegram Bot API сервера.

1. Перейти на [my.telegram.org/apps](https://my.telegram.org/apps)
2. Войти по номеру телефона
3. Создать приложение (название и описание — любые)
4. Скопировать **API ID** (число) и **API Hash** (строка)

## Установка на VDS

### 1. Подключиться к серверу

```bash
ssh root@ваш-ip-адрес
```

### 2. Установить Docker

```bash
curl -fsSL https://get.docker.com | sh
```

Docker Compose устанавливается вместе с Docker (plugin `docker compose`).

Проверить:

```bash
docker --version
docker compose version
```

### 3. Склонировать проект

```bash
git clone https://github.com/ваш-username/telegram_yt-dlp.git
cd telegram_yt-dlp
```

Или создать директорию и скопировать файлы вручную:

```bash
mkdir -p /opt/telegram_yt-dlp
cd /opt/telegram_yt-dlp
# скопировать файлы: bot.py, Dockerfile, docker-compose.yml, requirements.txt
```

### 4. Создать файл .env

```bash
cp .env.example .env
nano .env
```

Заполнить реальными значениями:

```
BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=abcdef1234567890abcdef1234567890
```

Сохранить: `Ctrl+O`, `Enter`, `Ctrl+X`.

### 5. Запустить

```bash
docker compose up -d --build
```

Первый запуск займёт несколько минут — скачиваются образы и собирается контейнер.

### 6. Проверить что всё работает

```bash
docker compose ps
```

Оба сервиса (`telegram-bot-api` и `bot`) должны быть в статусе `Up` / `running`.

Посмотреть логи:

```bash
docker compose logs -f bot
```

Если всё ок — отправьте боту ссылку на видео в Telegram.

## Управление

### Остановить бота

```bash
docker compose down
```

### Перезапустить

```bash
docker compose restart
```

### Обновить (после изменений в коде)

```bash
docker compose up -d --build
```

### Обновить yt-dlp до последней версии

```bash
docker compose build --no-cache bot
docker compose up -d bot
```

### Посмотреть логи

```bash
# все логи
docker compose logs -f

# только бот
docker compose logs -f bot

# последние 100 строк
docker compose logs --tail=100 bot
```

### Очистить загрузки вручную

```bash
docker volume rm telegram_yt-dlp_shared
```

## Структура проекта

```
telegram_yt-dlp/
├── bot.py               # Логика бота
├── Dockerfile           # Сборка контейнера (Python + ffmpeg)
├── docker-compose.yml   # Оркестрация: bot + telegram-bot-api
├── requirements.txt     # Python-зависимости
├── .env.example         # Шаблон переменных окружения
└── .dockerignore        # Исключения для Docker
```

## Архитектура

Запускаются 2 Docker-контейнера:

1. **telegram-bot-api** — локальный сервер Telegram Bot API. Позволяет отправлять файлы до 2 ГБ (вместо стандартных 50 МБ)
2. **bot** — Python-бот на aiogram v3 + yt-dlp + ffmpeg

Контейнеры связаны общим Docker volume (`shared`). Бот скачивает файлы в `/shared/downloads/`, а Bot API сервер читает их оттуда напрямую — без передачи файла по сети.

## Решение проблем

### Бот не отвечает

```bash
docker compose logs bot
```

Частые причины:
- Неверный `BOT_TOKEN` — проверить в `.env`
- Telegram Bot API не запустился — проверить `docker compose logs telegram-bot-api`
- Неверные `API_ID` / `API_HASH` — проверить на [my.telegram.org/apps](https://my.telegram.org/apps)

### telegram-bot-api падает с ошибкой

Убедитесь, что `TELEGRAM_API_ID` — число, а `TELEGRAM_API_HASH` — строка из 32 hex-символов.

### Ошибка "file too large"

Файл превышает лимит 2 ГБ. Выберите формат с меньшим разрешением или битрейтом.

### Скачивание не работает / ошибка yt-dlp

Пересоберите контейнер для обновления yt-dlp:

```bash
docker compose build --no-cache bot && docker compose up -d bot
```

### Заканчивается место на диске

Бот автоматически удаляет файлы после отправки. Если место всё равно заканчивается:

```bash
# Проверить использование диска
df -h

# Очистить Docker-кеш
docker system prune -f

# Очистить volume с загрузками
docker compose down
docker volume rm telegram_yt-dlp_shared
docker compose up -d
```

## Рекомендации по серверу

| Нагрузка | CPU | RAM | Диск |
|---|---|---|---|
| 1–5 пользователей | 1 vCPU | 1 ГБ | 20 ГБ |
| 5–20 пользователей | 2 vCPU | 2 ГБ | 40 ГБ |
| 20+ пользователей | 4 vCPU | 4 ГБ | 80+ ГБ |

Основная нагрузка — на диск (скачивание и мерджинг видео) и сеть. CPU нагружается только при объединении video+audio через ffmpeg.
