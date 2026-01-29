# Инструкция: разместить изменения в ветке main

Ниже краткий и безопасный сценарий публикации изменений на GitHub.

## 1) Проверьте удалённый репозиторий

```bash
git remote -v
```

Если `origin` не указывает на `https://github.com/goodwill-v/weather_app`, замените:

```bash
git remote set-url origin https://github.com/goodwill-v/weather_app
```

## 2) Проверьте статус и подготовьте изменения

```bash
git status
```

Убедитесь, что в списке нет временных файлов вроде `User_Data.json` и `weather_cache.json`
(они добавлены в `.gitignore`).

Добавьте изменения:

```bash
git add -A
```

## 3) Зафиксируйте изменения

```bash
git commit -m "Update Telegram bot and project docs"
```

## 4) Синхронизируйтесь с main и отправьте изменения

```bash
git pull --rebase origin main
git push origin main
```

Если возникнут конфликты — исправьте их, затем повторите `git add -A` и `git rebase --continue`.
