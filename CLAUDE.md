# BombaMod

Discord moderation bot (discord.py + SQLite/Postgres), run with Docker on a VM.

## Pipeline
1. `bot.on_message` queues messages (guild must have run `/bm setup`, needs a private review channel).
2. OpenAI `omni-moderation-latest` classifies the text -> category scores.
3. Roast guard (`enforcement.SEVERE_CATEGORIES`): only severe categories (hate, threats, self-harm,
   violence, sexual/minors) can trigger action. Plain harassment/insults are allowed silently.
4. Decision model via OpenRouter (`providers.OpenRouterProvider`). Default
   `mercury-decide` on Inception (`DECISION_BASE_URL=https://api.inceptionlabs.ai/v1`, `/v1/decisions`) or
   `inception/mercury-decide:free` on OpenRouter (`/api/alpha/decisions`). Other models use chat completions
   with reasoning disabled. Env: `DECISION_API_KEY/BASE_URL/MODEL` (legacy `OPENROUTER_*` still read). The model only sees scores unless text sharing is on.
5. `DiscordActionSink` applies delete/warn/timeout/ban, then DMs the user once per 60s.

## Spam handling
- Identical messages from one user reuse the first decision for 120s (`service._recent`).
- Deletes within 0.4s are one bulk delete (`DiscordActionSink._delete`).
- Review alerts are batched for 20s into one counted digest (`bot._format_alert_digest`).

- Opt-in `/bm logging` (policy `log_content`) adds author + 200-char excerpt (`privacy.case_excerpt`) to
  review alerts and action audit lines; off by default.

## Deploy
First time: `./scripts/setup-env.sh` (writes `.env`). Updates: push to `main`, then `./scripts/update.sh` on the VM.
`.env` and `data/` are gitignored. Settings live in the `bombamod-data` Docker volume.

## Checks
`PYTHONPATH=src .venv/bin/python -m pytest -q`, `ruff check src tests tools`, `mypy src`.

## Gotchas
- Actions fail silently-ish ("Discord action failed" alert) if the target's top role >= the bot's role.
- Autonomous actions default off: `/bm enforcement enabled:true`.
