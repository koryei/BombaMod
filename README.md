# BombaMod

**Free, open-source Discord moderation powered by OpenAI Omni Moderation and NVIDIA Nemotron.**

BombaMod is an MIT-licensed self-hosted bot. You can use, modify, and redistribute the software without paying a BombaMod license fee. It runs on a small CPU worker and starts with SQLite; no GPU, paid database, or paid inference model is needed for the default setup. You do need a Discord application/token and API keys. Third-party free quotas and hosting reliability are not guaranteed by BombaMod.

> **AI is fallible.** A moderation model is not a human moderator or a substitute for server safety policies. Keep actions off until your moderators have tested the system, use a review channel, and make an appeals/contact path available. BombaMod never automatically punishes on provider failure.

## What it does

- Classifies real-time server text with OpenAI's `omni-moderation-latest` model.
- Can optionally scan eligible images through the same OpenAI Moderation API. Image scanning is off by default.
- Can ask NVIDIA Nemotron 3 Super through OpenRouter to interpret moderation categories and your server rules, and (only after explicit server opt-in) best-effort-redacted flagged text.
- Lets administrators manage rules and action/privacy limits with the `/bm` slash-command group.
- Supports moderator correction labels for offline evaluation. It does **not** silently train model weights.
- Stores guild settings and short-retention metadata in SQLite by default or PostgreSQL via `DATABASE_URL`. Message text, images, and prompts are not persisted.

### How policy setup relates to model training

`/bm rules` supplies moderator-authored server policy to Nemotron on each decision. This is in-context policy, not fine-tuning: it does not change the model's parameters and does not make the model learn automatically from member actions. `/bm feedback` stores the message ID, moderator ID, predicted/corrected action and category labels for a case held in memory; only cases still available in that bot process can be labeled. Use labels to measure a separate offline test set and tune rules/thresholds. Labels are **not** used to retrain a model.

A responsible improvement loop is: start with clear written rules and automatic actions off; create synthetic examples for allowed/prohibited/ambiguous cases; have moderators label representative, de-identified cases; measure false positives and false negatives by category; revise the rules or confidence threshold; replay the test set; then pilot in review-only mode before enabling actions. Fine-tuning a 120B model requires separate compute, data rights/consent, dataset governance, evaluation, and deployment. BombaMod does not do this in the bot process, and OpenRouter's free inference endpoint is not a training service.

## Prerequisites

- Python 3.11 or newer (Docker uses Python 3.12).
- A Discord application with a bot token.
- An OpenAI API key for the Moderation API. OpenAI currently documents `/v1/moderations` as free to use, but account access/rate limits and provider terms still apply.
- An OpenRouter API key. `nvidia/nemotron-3-super-120b-a12b:free` is a third-party free model endpoint with quotas, capacity changes, and possible outages; it is not unlimited or guaranteed. If it becomes unavailable, BombaMod escalates for moderator review instead of pretending content passed.

## Discord setup

1. Create an application and bot in the [Discord Developer Portal](https://discord.com/developers/applications).
2. Under **Bot → Privileged Gateway Intents**, enable **Message Content Intent**. BombaMod needs message text/attachments to moderate; Discord may require additional review/approval for verified applications.
3. Invite the bot to your server with **View Channels**, **Send Messages**, **Use Application Commands**, and **Read Message History**. For autonomous deletion also grant **Manage Messages**; for timeouts grant **Moderate Members**; for optional bans grant **Ban Members**. Avoid Administrator permission. Put the bot role above members it may timeout/ban; Discord role hierarchy still blocks actions it cannot perform.
4. Run `/bm setup` in the server. Optionally pass comma-separated channel mentions/IDs. BombaMod remains inert until setup enables the guild.
5. Use `/bm rules` to provide short, explicit server rules. Use `/bm review-channel` to send review and audit notices to a moderator-only channel.
6. Start with `/bm enforcement enabled:false`, `/bm privacy share_text:false scan_images:false`, then `/bm status`. Use review-only operation to validate category signals before enabling actions.

## Local install

```bash
python -m venv .venv
. .venv/bin/activate                 # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e '.[dev]'
cp .env.example .env                 # Windows: copy .env.example .env
# Add DISCORD_TOKEN, OPENAI_API_KEY, and OPENROUTER_API_KEY to .env
bombamod
```

Keep `.env` private. Never paste tokens into public channels or commit credentials. If a token is exposed, revoke and rotate it immediately. On startup the bot refuses to run when required credentials are missing. It creates `./data/bombamod.db` when `DATABASE_URL` is empty.

Global slash-command changes may take a while to appear. Set `DISCORD_GUILD_ID` in `.env` to sync commands to one development guild immediately.

## Slash commands

Commands under `/bm` are restricted to moderators (Manage Messages/Moderate Members) or server managers. `/bm setup`, `/bm enforcement`, `/bm privacy`, `/bm review-channel`, and `/bm pause` require **Manage Server** or **Administrator**; only a server manager may enable external content sharing or high-impact actions:

| Command | Purpose |
| --- | --- |
| `/bm setup [channels]` | Enable moderation and optionally limit monitored channels. Empty means every channel, so configure private/opt-out channels as needed. Uses `IMAGE_SCAN_ENABLED_BY_DEFAULT` for the guild's initial image setting. |
| `/bm rules text` | Set moderator-authored policy (up to 6000 chars); this is decision context, not model training. |
| `/bm enforcement enabled max_timeout_minutes min_confidence allow_bans` | Configure actions. Bans require a separate explicit opt-in. |
| `/bm privacy share_text scan_images` | Independently opt into best-effort-redacted flagged text sent to OpenRouter and image attachments sent to OpenAI. Both are off by default. |
| `/bm review-channel [channel]` | Choose where moderator review and action notices go. |
| `/bm feedback message_id corrected_action` | Label a recent in-memory case without storing content. Only scanned cases still in process memory are labelable. |
| `/bm export-feedback` | Export retained labels without Discord IDs or raw content; treat exports as private. |
| `/bm retention strike_days feedback_days` | Set metadata retention windows (1–365 days). |
| `/bm pause` | Immediately disable new moderation work for the server. |
| `/bm status` | Show current guild settings and recent feedback-label count. |

## Privacy and safety

- **OpenAI:** Message text is sent to OpenAI for classification whenever moderation is enabled, unless the operator sets `DISABLE_OPENAI_TEXT=true` (in which case every eligible message is escalated rather than treated as cleared). If opted in, enabled images are fetched from Discord's HTTPS CDN and sent with the text to OpenAI. The official model accepts text/images (not audio) and supports images up to 20 MB; BombaMod uses a configurable lower download cap by default. OpenAI documents API content as not used for model training by default, but says abuse-monitoring logs may retain content up to 30 days. See [OpenAI data controls](https://developers.openai.com/api/docs/guides/your-data).
- **Child safety:** OpenAI says **do not send known or suspected child sexual abuse material (CSAM) to its Moderation API**. Omni Moderation is not a CSAM detector; its `sexual/minors` category is text-only and will not identify sexual/minor content in images. Never enable image scanning for reported/suspected CSAM. Follow Discord/platform reporting and applicable law; preserve evidence only through your approved safety process, not by sending it to this service.
- **OpenRouter:** By default BombaMod sends Nemotron the rules, category flags/scores, and bounded strike count, but **not** message text. A server admin must explicitly set `/bm privacy share_text:true` to also send best-effort-redacted flagged text. Redaction removes common mentions, identifiers, contact details and links, but is not a guarantee against personal or confidential information. OpenRouter's free model listing specifically cautions against sending personal/confidential data. Review current provider retention and privacy settings before opting in; do not use free routing for sensitive/private communities unless you accept its terms and risks.
- **Local data:** Raw message text, images, model prompts, and message contents are never written to the BombaMod database or application logs. Stored Discord IDs and action/category labels are still personal data and need an appropriate privacy notice and retention policy. Strike metadata expires on use after its configured 1–365 day window; feedback metadata is pruned on writes after its configured 1–365 day retention window. Retention is configurable with `/bm retention`. SQLite is local and is lost if your host's filesystem is ephemeral. Backups are the operator's responsibility.
- **Actions:** Automatic actions are disabled by default. Decisions are schema-validated and bounded by server settings; bot permissions and Discord's role hierarchy are checked. Ban opt-in is separate and disabled by default. No system can guarantee correct AI actions; have moderators monitor and provide an appeal process.
- **Prompt injection:** Member content is untrusted input. The decision prompt instructs Nemotron to treat it as data, but prompt instructions do not guarantee model behavior. Code, not the model, enforces action limits. Do not rely on AI for emergencies, threat response, self-harm support, or legal determinations.

## Configuration reference

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `DISCORD_TOKEN` | Yes | — | Discord bot token. |
| `OPENAI_API_KEY` | Yes | — | OpenAI Moderation API key. |
| `OPENROUTER_API_KEY` | Yes | — | OpenRouter API key for Nemotron decisions. |
| `OPENROUTER_MODEL` | No | `nvidia/nemotron-3-super-120b-a12b:free` | Model id can be changed by the operator. |
| `DATABASE_URL` | No | local SQLite | `postgresql://...` is normalized to async PostgreSQL. Use secret-manager variables in hosting. |
| `DISCORD_GUILD_ID` | No | global commands | Optional development guild for fast command registration. |
| `MAX_CONCURRENT_MODERATION` | No | `4` | Bound concurrent message processing (1–64). |
| `HTTP_TIMEOUT_SECONDS` | No | `12` | Per-request network timeout (3–60). |
| `MAX_IMAGE_BYTES` | No | `8000000` | Total image bytes per case (100 KB–20 MB), subject to provider support. |
| `MAX_MESSAGE_CHARS` | No | `6000` | Maximum text sent for classification; longer messages are escalated (1,000–20,000). |
| `IMAGE_SCAN_ENABLED_BY_DEFAULT` | No | `false` | Initial per-guild image-scan setting; command can toggle per server. |
| `ALLOW_OPENROUTER_TEXT` | No | `false` | Global hard gate for OpenRouter text; even true requires per-server `/bm privacy` opt-in. |
| `DISABLE_OPENAI_TEXT` | No | `false` | If true, do not transmit message text to OpenAI; all cases are escalated for moderator review. |
| `LOG_LEVEL` | No | `INFO` | Standard Python logging level. |

## Deployment

Run BombaMod as a **persistent worker/container**, not a short-lived serverless function: Discord's Gateway is a persistent WebSocket. The included Dockerfile runs as a non-root user, needs no inbound port and no GPU:

```bash
cp .env.example .env
# Edit .env and add the Discord/OpenAI/OpenRouter credentials.
docker compose up -d --build
docker compose logs -f bombamod
```

The included [Compose file](compose.yaml) persists SQLite data in a named volume, runs read-only with dropped Linux capabilities, restarts after failures, and bounds Docker log rotation. It uses the volume-backed SQLite URL unless `DATABASE_URL` is set in `.env`, allowing an external PostgreSQL deployment. Docker secrets and host hardening remain the operator's responsibility. To shut down, run `docker compose down`; this preserves the named data volume. Back it up before removing volumes or changing database schemas.

For PostgreSQL, use a managed PostgreSQL URL in `DATABASE_URL` and put credentials in your host's secret manager. Make backups and test restoration. There is no automatic database migration framework yet; this initial release creates tables on startup, so back up data before future schema upgrades. SQLite is appropriate for a single bot instance; use PostgreSQL for multi-replica or higher-availability deployments. Run only one active bot against a SQLite database.

Free hosting is **best effort, not 24/7**. Providers can sleep/restart free instances, erase ephemeral filesystems, cap worker hours/outbound requests, and rate-limit APIs. The Gateway disconnect means messages sent while offline will not be scanned. Do not bypass provider sleep policies with artificial keep-alive traffic. Use an always-on plan if continuous moderation is a production requirement, and maintain native Discord AutoMod/human coverage as backup.

## Development and checks

```bash
python -m pip install -e '.[dev]'
ruff check .
ruff format --check .
mypy src/bombamod
pytest
```

Tests mock external services; running them does not contact Discord, OpenAI, or OpenRouter. For a live pilot, use a private test server and clearly notify participants which services receive message data.

## License

BombaMod is distributed under the [MIT License](LICENSE). The software is free to use; Discord, OpenAI, OpenRouter, and hosting services are separate services with their own terms, eligibility, quotas, and availability.
