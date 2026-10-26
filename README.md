# 🚀 DealDash AI - Autonomous Affiliate Telegram Deal Bot

An enterprise-grade, fully autonomous Python bot that discovers high-discount e-commerce deals across India (Amazon, Flipkart, etc.), crafts viral AI-written captions using **Google Gemini 2.5 Flash**, embeds high-resolution product photos, auto-injects your affiliate tracking tags, and publishes them to your Telegram channel.

It features **real-time Out-of-Stock (OOS) auto-detection**, smart channel pacing, Telegram HTML injection defenses, SQLite deduplication, and a **GitHub Actions runner that keeps posting through the day**.

---

## 🌟 Key Capabilities

1. **Autonomous Live Ingestion**:
   - Ingests 30+ human-verified price drops (30% to 80% OFF) automatically every cycle.
   - Zero manual deal entry required.
2. **AI Copywriter (Google Gemini 2.5 Flash)**:
   - Transforms raw product titles into punchy, high-CTR Telegram posts with emojis, specs bullets, and strikethrough MRP.
   - Built with strict Telegram HTML sanitization to eliminate `400 Bad Request: can't parse entities` errors.
   - Graceful fallback: If AI quota is exhausted, immediately falls back to clean templates without crashing.
3. **Smart Affiliate Link Engine**:
   - Automatically cleans third-party affiliate tags and injects your tag (e.g. `tag=dammaiprasad-21` for Amazon, `affid=` for Flipkart).
4. **Auto Out-of-Stock (OOS) Detection**:
   - Background worker sweeps posted deals and checks availability.
   - Automatically edits the Telegram message to prepend `❌ [OFFER EXPIRED / OUT OF STOCK]` when items sell out.
5. **Smart Pacing & Operating Hours**:
   - Paces posts to prevent channel notification spam.
   - Respects Indian Standard Time (IST): active posting between 8:00 AM – 11:30 PM, quiet throttled sleep at night.
6. **Deal Editor Agent**:
   - Does not post the first rows from the feed. Each cycle it scores fresh deals (discount, price, photo, title, category mix, IST time slot).
   - If Gemini or Hermes is configured, the model may pick the slate. Unknown ids are ignored, and a bad reply falls back to the heuristic.
   - Near-duplicate titles are dropped so the channel does not get two posts of the same headphones.
   - A critic throws away AI captions that hide the real price and uses the template instead.
   - The reason for every post and skip is stored in SQLite and, if `ADMIN_CHAT_ID` is set, sent only to you.
7. **Daytime cloud runner**:
   - GitHub Actions starts a job at 09:37, 12:37, 15:37, 18:37, and 21:37 IST. Each job stays up and posts about once an hour for up to five hours, and stops after 23:20 IST.
   - GitHub can still fail to create a start. A job that is already running keeps posting, and the next start covers what is left. A manual run posts once immediately, then continues on the same hourly loop if the window is open.
   - Also includes Docker Compose for an always-on VM. That loop is separate and is not what the Actions job uses.

---

## 🏗️ Architecture

```
┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐     ┌───────────────────┐
│   Scout Engine   │ ──> │   AI Copywriter  │ ──> │ Publisher Engine │ ──> │ Auto-OOS Watcher  │
│ Live Feeds & APIs│     │ Gemini 2.5 Flash │     │Telegram Photo/Btn│     │Edits Expired Posts│
└──────────────────┘     └──────────────────┘     └──────────────────┘     └───────────────────┘
         │                         ▲
         ▼                         │
┌────────────────────────────────┘
│ Deal editor agent                 │
│ price first, coupon only if known │
│ check stock, then post            │
└─────────────────────────────────┘
```

---

## ⚙️ Configuration (`.env`)

Copy `.env.example` to `.env` and fill in your values:

```dotenv
# Telegram Bot & Channel
TELEGRAM_BOT_TOKEN=your_bot_token_from_botfather
TELEGRAM_CHAT_ID=@YourDealChannel

# Bot Controls
DRY_RUN=false
CONTINUOUS=true
POLL_INTERVAL_MINUTES=30
MAX_POSTS_PER_CYCLE=3
POST_DELAY_SECONDS=3

# Affiliate Tags
AMAZON_PARTNER_TAG=dammaiprasad-21
FLIPKART_AFFILIATE_ID=yourflipkartid

# AI Copywriter (Google Gemini)
GEMINI_API_KEY=your_gemini_api_key
GEMINI_MODEL=gemini-2.5-flash
AI_TIMEOUT_SECONDS=8

# Deal Filtering
FEEDS_ENABLED=true
MIN_DISCOUNT_PERCENT=30

# Editor agent (set AGENT_MODE=false for old first-come posting)
AGENT_MODE=true
AGENT_LLM=true
AGENT_MIN_SCORE=32
```

---

## 🧪 Testing & Verification

Run the entire suite of unit, negative, pacing, and agent tests:

```bash
pytest -v
```

Run a single-cycle dry run without sending live Telegram messages:

```bash
python run_once.py
```

---

## ☁️ Daytime runner on GitHub Actions

You don't even need to keep your laptop on. The Actions workflow posts through the day. GitHub's own scheduler is not guaranteed, so each start stays on the runner instead of depending on a fresh tick every hour.

1. In your GitHub repository, go to **Settings → Secrets and variables → Actions**.
2. Add the following repository secrets:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `GEMINI_API_KEY`
   - `AMAZON_PARTNER_TAG`
   - `ADMIN_CHAT_ID` (optional — private agent briefing, not the public channel)
3. Go to the **Actions** tab and enable workflows. A job posts once an hour from the morning start until 23:20 IST. It does not run overnight.

---

## 🐳 Docker Deployment (For VPS / Oracle Cloud Free Tier)

```bash
docker compose up -d --build
docker compose logs -f dealbot
```

---

## ⚖️ Compliance & Disclosures

- Automatically appends legal affiliate disclosures (`#ad Affiliate link. As an Amazon Associate I earn from qualifying purchases.`) to every post.
- Preserves accurate pricing and strikethrough MRP without manufacturing discounts.
