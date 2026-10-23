# Affiliate Telegram Deal Bot

An always-on Python bot that discovers retailer offers through authorized sources and posts them to a Telegram group/channel. It supports:

- Flipkart's official affiliate **offers API** (active offers, end time, LIVE/OOS status, affiliate offer URL).
- Amazon India's **Creators API SearchItems** (only when your account has approved API access).
- JSON feeds provided by approved affiliate networks (use this for Myntra only when your particular program/feed authorizes Telegram and deal/coupon promotion).
- `data/deals.json` for a small set of manually curated offers, each with its already-generated affiliate link.
- SQLite duplicate protection, post rate limiting, dry-run mode, expiry checks, link-level affiliate disclosures, and persistent state.

It intentionally does not scrape retailer webpages or manufacture discounts. API/feed access, social-channel permissions, and link tracking are subject to each affiliate program's rules.

## 1. Requirements

- Python 3.11+ or Docker + Docker Compose.
- A Telegram bot token from [@BotFather](https://t.me/BotFather).
- Your Telegram group/channel's chat ID. Add the bot to the group and grant permission to post. For a channel, add it as an administrator with posting permission.
- Affiliate program credentials and approval for the relevant retailer/network.

## 2. Install locally on Windows PowerShell

```powershell
cd affiliate-telegram-dealbot
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

Fill `.env`. Never commit `.env` or expose API tokens in a public repository.

## 3. Find the Telegram chat ID

1. Create the bot with `@BotFather` and copy its token into `TELEGRAM_BOT_TOKEN`.
2. Add the bot to your group. Send a fresh message in the group.
3. Open `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates` in a browser, replacing the placeholder. Find `message.chat.id` in the returned JSON (supergroups usually have a negative ID beginning `-100`).
4. Put the value in `TELEGRAM_CHAT_ID`. Do not share the bot token or the `getUpdates` URL publicly.
5. Grant the bot permission to send messages. Do not disable group privacy unless your bot needs to read group messages; this bot only sends messages.

If `getUpdates` is empty, send another new group message after adding the bot, and check that a webhook is not already configured.

## 4. Affiliate source configuration

### Flipkart

Set `FLIPKART_AFFILIATE_ID` and `FLIPKART_AFFILIATE_TOKEN` from the approved Flipkart Affiliate account. Keep `FLIPKART_API_ENABLED=true`. The adapter calls the documented offers endpoint and preserves its returned URL, which already contains affiliate tracking. API access depends on your account status.

Official API docs: https://affiliate.flipkart.com/api-docs/af_offer_ref.html

### Amazon India

Amazon's former PA-API 5.0 is deprecated. Use the Amazon India Creators API if your account is eligible and has API credentials. Amazon's documentation currently lists 10 qualifying sales in the preceding 30 days as a prerequisite to Creators API access; verify that your account qualifies before enabling it.

1. Join Amazon Associates India and register for Creators API in Associates Central.
2. Set `AMAZON_API_ENABLED=true` and fill `AMAZON_CREATOR_CLIENT_ID`, `AMAZON_CREATOR_CLIENT_SECRET`, and `AMAZON_PARTNER_TAG`.
3. Set semicolon-separated `AMAZON_SEARCH_KEYWORDS`; optionally adjust `AMAZON_MIN_SAVING_PERCENT`.
4. The adapter uses Amazon's `SearchItems` endpoint and preserves the API-returned `detailPageURL` unchanged. Verify API permissions and marketplace settings first.

Official docs: https://affiliate-program.amazon.in/creatorsapi/docs/en-us/introduction

### Myntra / approved affiliate networks

Do not assume every Myntra campaign allows Telegram. Some campaigns explicitly restrict Telegram even if another campaign allows social media. Check the **exact campaign's Media Allowed/Disallowed terms** and obtain written approval where unclear.

If your network provides a JSON feed/API and approves the Telegram channel, put it in `.env` as:

```dotenv
JSON_FEEDS=Myntra=https://YOUR_AUTHORIZED_FEED_URL
```

A feed can be a list of deal objects or an object with a `deals` list. Required fields are `title` and `url`; the URL must be a pre-generated affiliate/deep link from the network. Optional fields: `id`, `retailer`, `description`, `current_price`, `original_price`, `discount_percent`, `category`, `image_url`, `expires_at`.

If you don't have an API/feed, use the local file below. Add only links generated inside your affiliate network's dashboard; the bot will not convert ordinary product URLs into affiliate URLs for Myntra.

### Manual/local deals fallback

Edit `data/deals.json`:

```json
[
  {
    "id": "my-campaign-001",
    "retailer": "Myntra",
    "title": "Example product title — replace this example",
    "url": "https://YOUR-AFFILIATE-NETWORK-TRACKING-LINK",
    "description": "Add only a verified offer description.",
    "current_price": "₹999",
    "original_price": "₹1,499",
    "discount_percent": 33,
    "category": "Fashion",
    "expires_at": "2026-12-31T18:30:00+05:30"
  }
]
```

This is a schema example, not a real deal. Replace it with a real active offer and tracking link, or leave the starter `[]` empty. Price and discount values are posted exactly as supplied; ensure you validate them before publishing.

## 5. Test without posting anything

Keep `DRY_RUN=true`. Run once:

```powershell
python run_once.py
```

Run automated tests:

```powershell
pytest -q
```

The console displays the posts the bot *would* send. No Telegram messages are sent in dry-run mode.

## 6. Start the bot

Set `DRY_RUN=false` only after confirming that test posts are correct and all source/channel permissions are in place.

Run continuously:

```powershell
python -m dealbot.main
```

The bot checks sources every `POLL_INTERVAL_MINUTES`, posts up to `MAX_POSTS_PER_CYCLE` new deals, spaces posts by `POST_DELAY_SECONDS`, and stores posted IDs in `data/state.sqlite3`. Keep the terminal/session running, or run it on an always-on host.

### Docker (recommended for a server)

```bash
cp .env.example .env
# Edit .env and set credentials; initially keep DRY_RUN=true
docker compose up -d --build
docker compose logs -f dealbot
```

After verifying logs, change `DRY_RUN=false` in `.env` and restart:

```bash
docker compose up -d
```

The compose file restarts the container unless stopped and persists SQLite state through the `./data` mount. Do not run multiple replicas against the same database; keep one bot process to avoid duplicate races.

## 7. Useful operating controls

| Variable | Meaning | Default |
|---|---|---|
| `DRY_RUN` | Log but don't send posts | `true` |
| `CONTINUOUS` | Keep polling | `true` |
| `POLL_INTERVAL_MINUTES` | Time between source checks | `60` |
| `MAX_POSTS_PER_CYCLE` | Upper posting limit per poll | `5` |
| `POST_DELAY_SECONDS` | Delay between posts | `2` |
| `DATABASE_PATH` | Duplicate history database | `data/state.sqlite3` |
| `JSON_FEEDS` | Approved external JSON feeds, separated by `;` | empty |

## Compliance and reliability notes

- Affiliate links must be disclosed clearly near the link. This bot includes a general disclosure, and the Amazon Associates statement on Amazon posts. Also place any account-level disclosures required by the program in your channel description/profile.
- Make sure your Telegram channel/group is listed or approved as a promotional property wherever required by the retailer/network.
- Don't invent discounts or state a price from stale data as current. The local/manual feed fields are only as accurate as the person supplying them.
- A product search is not a promise of price/stock. Confirm that the API result represents an actual deal before you enable regular posting.
- The bot's API/feed sources can fail, be rate limited, or change. Errors are logged and the process continues; this isn't a guarantee of uninterrupted service or income.
- Telegram bot API documentation: https://core.telegram.org/bots/api#sendmessage
