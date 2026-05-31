# Публикация на GitHub (один раз)

Репозиторий ещё не создан. Выполните на своём компьютере или в GitHub UI:

1. Откройте https://github.com/new
2. Имя: `cursor-bot`
3. Public → Create repository **без** README
4. В терминале:

```bash
cd cursor-telegram-bot
git remote add origin https://github.com/Nikita34196/cursor-bot.git
git push -u origin main
```

5. Railway → Deploy from GitHub → `Nikita34196/cursor-bot`
6. Variables: `BOT_TOKEN`, `CURSOR_API_KEY`, `DEFAULT_REPO_URL`

Готово — бот работает в облаке 24/7.
