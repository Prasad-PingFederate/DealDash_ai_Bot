# 🚀 DealDash AI - Autonomous Affiliate Telegram Deal Bot

An enterprise-grade, fully autonomous Python deal agent that discovers high-discount e-commerce deals across India (Amazon, Reddit, RSS feeds), performs **two-stage live page verification**, crafts viral AI captions via **Google Gemini 2.5 Flash / Groq / OpenRouter**, tracks historical price sightings, and publishes them with affiliate tags to Telegram with **real-time Out-of-Stock and price-spike monitoring**.

---

## 🌟 Key Capabilities (Phases 1–5)

1. **Two-Stage Live Amazon Enrichment**:
   - **Stage 1 (Pre-scoring)**: Fast, non-blocking scoring of all feed candidates (Reddit `r/dealsforindia`, `r/IndianGaming`, RSS feeds).
   - **Stage 2 (Live Verification)**: Fetches the live Amazon product page to scrape real-time price, basis MRP, customer ratings, review volume, and on-page coupons before final selection.
   - **Stage 3 (DealAgent Scoring)**: Re-scores verified survivors with actual live prices and social proof metrics.
2. **Effective Coupon & Bank Offer Pricing**:
   - Automatically detects and parses on-page Amazon coupons (flat `Apply ₹500 coupon` and percentage `Save 10% coupon`).
   - Calculates `effective_price = live_price - coupon_discount` and `effective_discount_pct`.
   - Post captions feature the exact deal price breakdown with strikethrough MRP and coupon instructions.
3. **Historical Price Memory (`price_sightings`)**:
   - Stores sightings across product runs in SQLite to identify genuine price drops and all-time lows.
   - Detects and penalizes chronic fake MRP inflations on unbranded or generic accessories.
4. **Composite Buyer Value Score**:
   - **Brand Tiers**: Tier-1 (+20 pts for Apple, Samsung, Sony, LG, etc.), Tier-2 (+14 pts for boAt, Noise, Realme, Xiaomi).
   - **Social Proof Boost**: +10 pts for verified ratings $\ge 4.2★$ with $\ge 500$ customer reviews.
   - **Accessory Anti-Spoofing**: Prevents cheap ₹149 generic phone cases from hijacking Tier-1 brand boosts via preposition filtering (`for `, `case for `).
5. **Auto Out-of-Stock (OOS) & In-Stock Price Spike Watcher**:
   - Post-cycle background watcher verifies active Telegram posts (capped at 6 deals per cycle with 1.5s throttling to protect runner IP).
   - Uses **consecutive 2-check confirmation** to prevent false edits caused by transient page glitches.
   - Automatically edits Telegram posts to prepend:
     - `❌ [OFFER EXPIRED / OUT OF STOCK]` when items sell out.
     - `⚠️ [DEAL ENDED — PRICE INCREASED TO ₹X]` when the effective price jumps $\ge 25\%$ while still in stock.
6. **Guaranteed Hourly Posting & Anti-Drift Clock**:
   - **45-Minute Pacing Gate**: New hourly cron jobs starting 57–59 minutes after the previous run pass the cooldown gate and post immediately on startup.
   - **65-Minute In-Runner Backup Sleep**: The active runner sleeps for 65 minutes after posting, ensuring clean replacement by the next hourly cron (:37), or auto-recovering dropped crons at :42.
   - **Immediate SQLite Sync**: Checkpoints WAL mode and synchronizes `data/*.db` to `origin/main` immediately after every cycle.
   - **Adaptive Soft-Floor & Deterministic Backfill**: Guarantees targeted posts per cycle even under strict LLM constraints.
7. **AI Copywriter with Strict Critic & Telegram HTML Escaping**:
   - Multi-provider support: **Google Gemini 2.5 Flash**, **Groq Llama 3.3 70B**, and **OpenRouter Gemma 26B**.
   - **Strict Critic Validation**: Validates AI captions against deal price, effective price, and coupons to reject hallucinations.
   - Full HTML entity escaping (`quote=False`) protects against Telegram `400 Bad Request: can't parse entities`.

---

## 🏗️ Architecture

```
┌─────────────────────────────────┐
│        Scout Engine             │
│   RSS Feeds, Reddit, Amazon     │
└─────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────┐
│ Stage 1: Heuristic Pre-Scoring  │
│  Fast, in-memory candidate rank │
└─────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────┐
│ Stage 2: Live Amazon Enrichment │
│  Scrape live price, MRP, rating,│
│  coupons, and hi-res image      │
└─────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────┐
│ Stage 3: Autonomous DealAgent   │
│  Composite buyer score, brand   │
│  tiers, soft-floor & backfill   │
└─────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────┐      ┌─────────────────────────────┐
│    AI Multi-Provider Engine     │ ───> │  Publisher & Telegram Post  │
│  Gemini 2.5 / Groq / OpenRouter │      │  Affiliate link + HTML safe │
└─────────────────────────────────┘      └─────────────────────────────┘
                                                        │
                                                        ▼
                                         ┌─────────────────────────────┐
                                         │ OOS & Price Spike Watcher   │
                                         │ Edits expired/spiked posts  │
                                         └─────────────────────────────┘
```

---

## ⚙️ Configuration (`.env`)

Copy `.env.example` to `.env` and configure your credentials:

```dotenv
# Telegram Bot & Channel
TELEGRAM_BOT_TOKEN=your_bot_token_from_botfather
TELEGRAM_CHAT_ID=@YourDealChannel
ADMIN_CHAT_ID=your_private_admin_chat_id

# Bot Controls
DRY_RUN=false
CONTINUOUS=false
MAX_POSTS_PER_CYCLE=3
POST_DELAY_SECONDS=2
MIN_DISCOUNT_PERCENT=30
ALLOWED_RETAILERS=Amazon

# Affiliate Tracking
AMAZON_PARTNER_TAG=dammaiprasad-21

# Primary AI Copywriter (Google Gemini)
AI_PROVIDER=gemini
GEMINI_API_KEY=your_gemini_api_key
GEMINI_MODEL=gemini-2.5-flash
AI_TIMEOUT_SECONDS=12

# Secondary High-Speed AI (Groq)
GROQ_API_KEY=your_groq_api_key
GROQ_MODEL=llama-3.3-70b-versatile

# Fallback AI (OpenRouter / Gemma)
HERMES_API_KEY=your_openrouter_key
HERMES_MODEL=google/gemma-4-26b-a4b-it:free
HERMES_ENDPOINT=https://openrouter.ai/api/v1/chat/completions

# Autonomous DealAgent
AGENT_MODE=true
AGENT_LLM=true
AGENT_MIN_SCORE=28
CHECK_STOCK_BEFORE_POST=true
LISTEN_FOR_ENGAGEMENT=true
```

---

## 🧪 Testing & Verification

Run the complete test suite across all phases:

```bash
pytest -v
```

Run a single local cycle in dry-run mode:

```bash
python run_once.py
```

---

## ☁️ Autonomous Daytime Runner (GitHub Actions)

The bot runs on GitHub Actions throughout Indian daytime hours (09:07 IST to 23:07 IST):

1. Hourly cron triggers at minute `:37` (`37 3-17 * * *` UTC).
2. Runner checks out repository and pulls the latest SQLite database.
3. 45-minute startup gate ensures immediate posting without waiting.
4. Database state is checkpointed and synced to `origin/main` after each cycle.
5. In-runner 65-minute sleep provides seamless handoff to the next hourly job or fallback execution.
