BAZAR_WIDGET_HTML = '''
  <!-- ⚡ BazarAI Floating Launcher & Chat Window -->
  <div id="bazaroAiWidget" class="bazaro-ai-widget">
    <!-- Welcome Tooltip Callout -->
    <div id="bazaroAiTooltip" class="bazaro-ai-tooltip hidden">
      <div class="tooltip-content">
        <span>👋 Looking for hidden coupons, price history, or the best deal under your budget? Ask me!</span>
        <button id="bazaroAiTooltipClose" class="tooltip-close" aria-label="Close tooltip">&times;</button>
      </div>
    </div>

    <!-- Floating Launcher Button -->
    <button id="bazaroAiLauncher" class="bazaro-ai-launcher" aria-label="Ask BazarAI Discounts Finder">
      <div class="launcher-avatar-wrap">
        <span class="launcher-icon">⚡</span>
        <span class="launcher-status-dot"></span>
      </div>
      <span class="launcher-label">⚡ Ask BazarAI • Discounts Finder</span>
      <span id="bazaroAiUnreadBadge" class="launcher-badge">1</span>
    </button>

    <!-- Chat Drawer -->
    <div id="bazaroAiDrawer" class="bazaro-ai-drawer hidden" role="dialog" aria-labelledby="bazaroAiTitle">
      <div class="drawer-header">
        <div class="drawer-header-left">
          <div class="drawer-avatar">⚡</div>
          <div class="drawer-titles">
            <h3 id="bazaroAiTitle" class="drawer-title">⚡ BazarAI — AI Discounts &amp; Coupon Finder</h3>
            <span class="drawer-subtitle">🟢 Online 24/7 • Finds Lowest Prices, Hidden Coupons &amp; Keepa Charts</span>
          </div>
        </div>
        <div class="drawer-controls">
          <button id="bazaroAiClearBtn" class="drawer-ctrl-btn" title="Clear Chat History">🗑️</button>
          <button id="bazaroAiExpandBtn" class="drawer-ctrl-btn desktop-only" title="Expand / Shrink Window">⛶</button>
          <button id="bazaroAiCloseBtn" class="drawer-ctrl-btn" title="Close Chat">&times;</button>
        </div>
      </div>

      <!-- Quick Action Chips Tray -->
      <div class="drawer-chips-tray" id="bazaroAiChips">
        <button class="ai-chip" data-prompt="🚨 Top Loot Deals">🚨 Top Loot Deals</button>
        <button class="ai-chip" data-prompt="🪙 Under ₹299 Store">🪙 Under ₹299 Store</button>
        <button class="ai-chip" data-prompt="🎟️ Hidden Coupons">🎟️ Hidden Coupons</button>
        <button class="ai-chip" data-prompt="🎧 Audio &amp; Tech Drops">🎧 Audio &amp; Tech Drops</button>
        <button class="ai-chip" data-prompt="🏠 Home &amp; Kitchen Loots">🏠 Home &amp; Kitchen Loots</button>
        <button class="ai-chip" data-prompt="📅 Amazon Sale Calendar">📅 Amazon Sale Calendar</button>
        <button class="ai-chip" data-prompt="📈 Check Amazon Link">📈 Check Amazon Link</button>
      </div>

      <!-- Message Stream Container -->
      <div class="drawer-messages" id="bazaroAiMessages"></div>

      <!-- Typing Indicator -->
      <div id="bazaroAiTyping" class="drawer-typing hidden">
        <span class="typing-dot"></span>
        <span class="typing-dot"></span>
        <span class="typing-dot"></span>
        <span class="typing-text">BazarAI is analyzing live deals...</span>
      </div>

      <!-- Input Bar -->
      <form id="bazaroAiForm" class="drawer-input-bar">
        <input type="text" id="bazaroAiInput" class="drawer-input" placeholder="Ask for any product, budget (e.g. earbuds under 999), sale info, or paste Amazon link..." autocomplete="off">
        <button type="submit" id="bazaroAiSendBtn" class="drawer-send-btn" aria-label="Send message">➤</button>
      </form>
    </div>
  </div>

  <script src="/bazaro_ai.js"></script>
'''

"""
Static Site Generation (SSG) Pre-rendering, Category Landing Pages, Price Stores,
Individual Product Pages, RSS 2.0 Feed & Rich Google Schema for BharatBazaro.com.

Pre-renders live Amazon India deals into static semantic HTML across:
1. Core Category Landing Pages:
   - index.html (Top 30 deals across all categories)
   - deals.html (Top Loot Alerts & steepest discount deals, window.INITIAL_FILTER = "loot")
   - mobiles.html (Mobile & Accessories deals, window.INITIAL_CATEGORY = "mobiles")
   - electronics.html (Electronics, Audio & Laptop deals, window.INITIAL_CATEGORY = "electronics")
   - home.html (Home & Kitchen deals, window.INITIAL_CATEGORY = "home")
   - fashion.html (Fashion & Footwear deals, window.INITIAL_CATEGORY = "fashion")
   - grocery.html (Grocery & Personal Care deals, window.INITIAL_CATEGORY = "grocery")

2. High-Intent Price Stores & Coupon Pages:
   - under-99.html (Amazon ₹99 Store, window.INITIAL_MAX_PRICE = 99)
   - under-299.html (Amazon Deals Under ₹299, window.INITIAL_MAX_PRICE = 299)
   - under-499.html (Amazon Deals Under ₹499, window.INITIAL_MAX_PRICE = 499)
   - under-999.html (Earbuds, Watches & Gadgets Under ₹999, window.INITIAL_MAX_PRICE = 999)
   - coupons.html (Hidden On-Page Coupons & Bank Offers, window.INITIAL_FILTER = "coupons")

3. Programmatic Individual Product Pages (/deal/{asin}.html):
   - Fast, SEO-optimized page per live deal with Keepa chart, price waterfall, how-to-claim, and Product schema.

4. Multi-Channel Syndication Feed:
   - feed.xml (RSS 2.0 + Yahoo Media Feed for Pinterest, X, IFTTT, Telegram, Google Discover)

5. Comprehensive XML Sitemap:
   - sitemap.xml (All 60+ canonical URLs with lastmod, priorities, and changefreq)
"""

import html as html_lib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

PAGE_CONFIGS = [
    {
        "filename": "index.html",
        "title": "BharatBazaro — Live Amazon India Loot Deals, Price Drop Radar & Hidden Coupon Finder (2026)",
        "canonical": "https://bharatbazaro.com/",
        "meta_description": "Discover screaming Amazon India price drops, 80% off lightning loot deals, and stackable hidden on-page coupons verified live 24/7 with BharatBazaro's AI Deal Radar.",
        "hero_title": 'India\'s 24/7 AI Deal & <span class="gradient-text">Price Drop Radar</span>',
        "hero_subtitle": "Real-time price error alerts, stackable on-page coupons, Keepa charts & BuyHatke-inspired confidence ratings verified autonomously every cycle.",
        "category_slug": "all",
        "initial_filter": None,
        "initial_max_price": None,
        "breadcrumb_name": None,
    },
    {
        "filename": "deals.html",
        "title": "Today's Top Amazon Loot Deals, Price Errors & 80% Off Lightning Offers | BharatBazaro",
        "canonical": "https://bharatbazaro.com/deals.html",
        "meta_description": "Today's verified Amazon India loot deals, 80% off lightning price errors, and screaming discounts verified live 24/7 by BharatBazaro AI Deal Radar.",
        "hero_title": 'Today\'s Top Amazon <span class="gradient-text">Loot Deals & 80% Off Offers</span>',
        "hero_subtitle": "Autonomous radar tracking price errors, 70%–90% off lightning deals, and screaming Amazon discounts verified live.",
        "category_slug": None,
        "initial_filter": "loot",
        "initial_max_price": None,
        "breadcrumb_name": "Loot Deals",
    },
    {
        "filename": "mobiles.html",
        "title": "Best Mobile Phone Deals Today in India — iPhone, Samsung, OnePlus Price Drops | BharatBazaro",
        "canonical": "https://bharatbazaro.com/mobiles.html",
        "meta_description": "Best smartphone and mobile phone deals today in India. Live price drops on iPhones, Samsung Galaxy, OnePlus, chargers, cases, and mobile accessories.",
        "hero_title": 'Best Mobile Phone Deals & <span class="gradient-text">Smartphone Price Drops</span>',
        "hero_subtitle": "Live automated price drops on iPhones, Samsung Galaxy, OnePlus, chargers, and mobile accessories.",
        "category_slug": "mobiles",
        "initial_filter": None,
        "initial_max_price": None,
        "breadcrumb_name": "Mobiles & Accessories",
    },
    {
        "filename": "electronics.html",
        "title": "Top Electronics, Headphones, Laptops & Smartwatch Price Drops Today | BharatBazaro",
        "canonical": "https://bharatbazaro.com/electronics.html",
        "meta_description": "Top electronics, noise-cancelling headphones, gaming laptops, smartwatches, and computing gear price drops on Amazon India.",
        "hero_title": 'Top Electronics, Audio & <span class="gradient-text">Laptop Price Drops Today</span>',
        "hero_subtitle": "Discover screaming discounts on noise-cancelling headphones, laptops, PC accessories, and smart electronics.",
        "category_slug": "electronics",
        "initial_filter": None,
        "initial_max_price": None,
        "breadcrumb_name": "Electronics, Audio & Computing",
    },
    {
        "filename": "home.html",
        "title": "Home & Kitchen Appliances Loot Deals — Air Fryers, Mixer Grinders & Daily Essentials | BharatBazaro",
        "canonical": "https://bharatbazaro.com/home.html",
        "meta_description": "Home and kitchen appliances loot deals in India. Air fryers, mixer grinders, cookware, water purifiers, and home essentials with verified price drops.",
        "hero_title": 'Home & Kitchen Appliances <span class="gradient-text">Loot Deals & Offers</span>',
        "hero_subtitle": "Unbeatable price drops on air fryers, mixer grinders, cookware, water purifiers, and home essentials.",
        "category_slug": "home",
        "initial_filter": None,
        "initial_max_price": None,
        "breadcrumb_name": "Home & Kitchen",
    },
    {
        "filename": "fashion.html",
        "title": "Branded Fashion, Footwear & Watch Loot Deals Today (50%–85% Off) | BharatBazaro",
        "canonical": "https://bharatbazaro.com/fashion.html",
        "meta_description": "Branded fashion, footwear, sneakers, and luxury watch deals today (50%–85% off) on Amazon India verified by BharatBazaro.",
        "hero_title": 'Branded Fashion, Footwear & <span class="gradient-text">Watch Loot Deals (50%–85% Off)</span>',
        "hero_subtitle": "Steep discounts on Puma, Adidas, Casio, Levi's, sneakers, formal wear, and designer accessories.",
        "category_slug": "fashion",
        "initial_filter": None,
        "initial_max_price": None,
        "breadcrumb_name": "Fashion & Footwear",
    },
    {
        "filename": "grocery.html",
        "title": "Online Grocery, Skincare & Amazon Subscribe & Save Coupon Deals India | BharatBazaro",
        "canonical": "https://bharatbazaro.com/grocery.html",
        "meta_description": "Online grocery, skincare, and daily essentials with extra stackable on-page coupons and Amazon Subscribe & Save deals in India.",
        "hero_title": 'Online Grocery, Skincare & <span class="gradient-text">Subscribe & Save Coupon Deals</span>',
        "hero_subtitle": "Extra stackable on-page coupons and discounts on daily groceries, personal care, dry fruits, and skincare.",
        "category_slug": "grocery",
        "initial_filter": None,
        "initial_max_price": None,
        "breadcrumb_name": "Personal Care & Grocery",
    },
    {
        "filename": "beauty.html",
        "title": "Best Beauty, Skincare & Grooming Deals on Amazon India (Dove, Minimalist, Cetaphil, L'Oreal, Nivea, Colgate) | BharatBazaro",
        "canonical": "https://bharatbazaro.com/beauty.html",
        "meta_description": "Best beauty, skincare, and grooming deals on Amazon India. Live verified price drops and value pack steals on Dove, Minimalist, Cetaphil, L'Oreal, Nivea, and Colgate.",
        "hero_title": 'Best Beauty, Skincare & <span class="gradient-text">Grooming Deals on Amazon India</span>',
        "hero_subtitle": "Autonomous radar tracking verified discounts on Dove, Minimalist, Cetaphil, L'Oreal, Nivea, Colgate & top personal care brands.",
        "category_slug": "beauty",
        "initial_filter": None,
        "initial_max_price": None,
        "breadcrumb_name": "Beauty & Grooming",
        "subtag": "bb-seo-beauty",
    },
    # ── High-Intent Price Stores & Coupon Landing Pages ──
    {
        "filename": "under-99.html",
        "title": "Amazon ₹99 Store India — Best Daily Essentials & Loot Deals Under ₹99 Today | BharatBazaro",
        "canonical": "https://bharatbazaro.com/under-99.html",
        "meta_description": "Amazon India ₹99 store loot deals today. Discover verified daily essentials, mobile cables, kitchen accessories, and budget loots under ₹99 with BharatBazaro.",
        "hero_title": 'Amazon ₹99 Store India — <span class="gradient-text">Loot Deals Under ₹99</span>',
        "hero_subtitle": "Verified daily essentials, mobile accessories, kitchen loots, and budget deals under ₹99 updated live 24/7.",
        "category_slug": None,
        "initial_filter": None,
        "initial_max_price": 99,
        "breadcrumb_name": "₹99 Store",
    },
    {
        "filename": "under-299.html",
        "title": "Viral Daily Essentials & Value Pack Steals Under ₹299 on Amazon India | BharatBazaro",
        "canonical": "https://bharatbazaro.com/under-299.html",
        "meta_description": "Viral daily essentials, FMCG value packs, skincare loots, and steals under ₹299 on Amazon India verified live 24/7 with BharatBazaro AI Deal Radar.",
        "hero_title": 'Viral Daily Essentials & <span class="gradient-text">Steals Under ₹299</span>',
        "hero_subtitle": "Unbeatable price drops and quantity value packs on skincare, personal care, cables, and home loots under ₹299.",
        "category_slug": None,
        "initial_filter": None,
        "initial_max_price": 299,
        "breadcrumb_name": "Deals Under ₹299",
        "subtag": "bb-seo-under299",
    },
    {
        "filename": "under-499.html",
        "title": "Top Amazon Deals Under ₹499 Today in India (50%–85% Off Verified Drops) | BharatBazaro",
        "canonical": "https://bharatbazaro.com/under-499.html",
        "meta_description": "Top Amazon deals under ₹499 in India. 50%–85% off earbuds, t-shirts, power banks, and daily essentials verified live.",
        "hero_title": 'Top Amazon Deals <span class="gradient-text">Under ₹499 (50%–85% Off)</span>',
        "hero_subtitle": "Discover screaming discounts on Bluetooth earbuds, branded apparel, power accessories, and home items under ₹499.",
        "category_slug": None,
        "initial_filter": None,
        "initial_max_price": 499,
        "breadcrumb_name": "Deals Under ₹499",
    },
    {
        "filename": "under-999.html",
        "title": "Best Earbuds, Smartwatches, Shoes & Gadgets Under ₹999 on Amazon India Today | BharatBazaro",
        "canonical": "https://bharatbazaro.com/under-999.html",
        "meta_description": "Best earbuds, smartwatches, running shoes, and tech gadgets under ₹999 on Amazon India today with verified price error alerts.",
        "hero_title": 'Best Gadgets & Shoes <span class="gradient-text">Under ₹999 Today</span>',
        "hero_subtitle": "Verified price drops on wireless earbuds, smartwatches, sneakers, and smart home gadgets under ₹999.",
        "category_slug": None,
        "initial_filter": None,
        "initial_max_price": 999,
        "breadcrumb_name": "Deals Under ₹999",
    },
    {
        "filename": "coupons.html",
        "title": "Amazon India Hidden Checkbox Coupons, Subscribe & Save & Bank Offer Finder Today | BharatBazaro",
        "canonical": "https://bharatbazaro.com/coupons.html",
        "meta_description": "Amazon India hidden checkbox coupons, Subscribe & Save discounts, and instant bank card offers verified live by BharatBazaro.",
        "hero_title": 'Hidden Checkbox Coupons & <span class="gradient-text">Bank Offer Finder</span>',
        "hero_subtitle": "Autonomous scanner tracking clip-on Amazon coupons, extra 10%–20% Subscribe & Save discounts, and promo codes.",
        "category_slug": None,
        "initial_filter": "coupons",
        "initial_max_price": None,
        "breadcrumb_name": "Hidden Coupons",
    },
]


def ensure_og_banner(root_dir: Path) -> Path:
    """Ensure a high-resolution 1200x630 landscape social preview banner exists."""
    banner_path = root_dir / "og-banner.png"
    try:
        from PIL import Image, ImageDraw, ImageFont
        if banner_path.exists():
            try:
                with Image.open(banner_path) as im:
                    if im.size == (1200, 630):
                        return banner_path
            except Exception:
                pass

        W, H = 1200, 630
        img = Image.new("RGBA", (W, H), (11, 17, 32, 255))
        draw = ImageDraw.Draw(img)

        # Gradient background
        for y in range(H):
            t = y / H
            r = int(10 * (1 - t) + 4 * t)
            g = int(22 * (1 - t) + 38 * t)
            b = int(40 * (1 - t) + 30 * t)
            draw.line([(0, y), (W, y)], fill=(r, g, b, 255))

        # Ambient radial glow
        glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        gdraw = ImageDraw.Draw(glow)
        for rad in range(450, 0, -15):
            alpha = int(40 * (1 - rad / 450))
            gdraw.ellipse([980 - rad, 160 - rad, 980 + rad, 160 + rad], fill=(16, 185, 129, alpha))
            gdraw.ellipse([180 - rad, 480 - rad, 180 + rad, 480 + rad], fill=(6, 182, 212, int(alpha * 0.8)))
        img = Image.alpha_composite(img, glow)
        draw = ImageDraw.Draw(img)

        # Border accents
        draw.rounded_rectangle([15, 15, W - 16, H - 16], radius=24, outline=(16, 185, 129, 70), width=2)
        draw.rounded_rectangle([25, 25, W - 26, H - 26], radius=18, outline=(255, 255, 255, 20), width=1)

        # Fonts
        windir = os.environ.get("WINDIR", "C:/Windows")
        f_pill = f_title = f_h2 = f_sub = f_chip = f_url = None
        font_candidates = [
            os.path.join(windir, "Fonts", "segoeuib.ttf"),
            os.path.join(windir, "Fonts", "arialbd.ttf"),
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
        for fc in font_candidates:
            if os.path.exists(fc):
                try:
                    f_pill = ImageFont.truetype(fc, 18)
                    f_title = ImageFont.truetype(fc, 64)
                    f_h2 = ImageFont.truetype(fc, 36)
                    f_chip = ImageFont.truetype(fc, 18)
                    f_url = ImageFont.truetype(fc, 20)
                    break
                except Exception:
                    pass
        reg_candidates = [
            os.path.join(windir, "Fonts", "segoeui.ttf"),
            os.path.join(windir, "Fonts", "arial.ttf"),
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
        for rc in reg_candidates:
            if os.path.exists(rc):
                try:
                    f_sub = ImageFont.truetype(rc, 24)
                    break
                except Exception:
                    pass

        if not f_pill:
            f_pill = f_title = f_h2 = f_sub = f_chip = f_url = ImageFont.load_default()

        # Top Pill Badge
        pill_text = "⚡ AUTONOMOUS DEAL & PRICE DROP RADAR"
        bbox = draw.textbbox((0, 0), pill_text, font=f_pill)
        pw = bbox[2] - bbox[0] + 36
        ph = bbox[3] - bbox[1] + 16
        px, py = 70, 60
        draw.rounded_rectangle([px, py, px + pw, py + ph], radius=14, fill=(16, 185, 129, 35), outline=(16, 185, 129, 120), width=1)
        draw.text((px + 18, py + 7), pill_text, font=f_pill, fill=(52, 211, 153))

        # Icon
        icon_path = root_dir / "icon-512.png"
        if icon_path.exists():
            with Image.open(icon_path) as icon_img:
                icon_res = icon_img.convert("RGBA").resize((130, 130), Image.Resampling.LANCZOS)
                img.paste(icon_res, (70, 130), icon_res)

        # Brand Title
        draw.text((225, 135), "Bharat", font=f_title, fill=(255, 255, 255))
        b_w = draw.textbbox((225, 135), "Bharat", font=f_title)[2]
        draw.text((b_w + 5, 135), "Bazaro", font=f_title, fill=(52, 211, 153))

        # Headline & Subtitle
        draw.text((70, 290), "Live Amazon India Loot Deals, Price Drops & Hidden Coupons", font=f_h2, fill=(241, 245, 249))
        draw.text((70, 350), "Real-time price error alerts, stackable on-page coupons & Keepa charts verified 24/7", font=f_sub, fill=(148, 163, 184))

        # Feature Chips
        chips = ["🔥 10,000+ Items Scanned", "🚨 Verified Loot Alerts", "📉 All-Time Lows", "🎟️ On-Page Coupons"]
        cx = 70
        cy = 430
        for ch in chips:
            c_box = draw.textbbox((0, 0), ch, font=f_chip)
            cw = c_box[2] - c_box[0] + 28
            ch_h = c_box[3] - c_box[1] + 14
            draw.rounded_rectangle([cx, cy, cx + cw, cy + ch_h], radius=10, fill=(30, 41, 59, 200), outline=(51, 65, 85, 255), width=1)
            draw.text((cx + 14, cy + 6), ch, font=f_chip, fill=(226, 232, 240))
            cx += cw + 16

        # Footer Bar
        draw.line([(70, 520), (W - 70, 520)], fill=(30, 41, 59, 255), width=1)
        draw.text((70, 545), "🌐 bharatbazaro.com", font=f_url, fill=(52, 211, 153))

        img.convert("RGB").save(banner_path, "PNG", optimize=True)
        logger.info(f"Generated 1200x630 og-banner at {banner_path}")
    except Exception as e:
        logger.debug(f"ensure_og_banner error: {e}")
    return banner_path


def render_deal_card_html(d: Dict[str, Any], card_index: int = 0, subtag: Optional[str] = None) -> str:
    """Render a semantic HTML article matching client-side createDealCardHtml in app.js."""
    asin = html_lib.escape(str(d.get("asin") or "B0UNKNOWN0"))
    title = html_lib.escape(html_lib.unescape(str(d.get("title") or "Amazon Deal")).strip())
    eff_price = d.get("effective_price") or d.get("list_price") or 0
    if eff_price <= 0:
        return ""
    mrp = d.get("mrp") or 0
    disc_pct = d.get("effective_discount_pct") or d.get("discount_pct") or 0
    savings_inr = d.get("savings_inr") or 0
    buy_conf = d.get("buy_confidence") or 75
    brand_chip = html_lib.escape(str(d.get("brand") or d.get("category") or "Amazon"))

    card_subtag = subtag or "bb-grid"
    raw_aff = f"https://www.amazon.in/dp/{asin}?tag=dammaiprasad-21&linkCode=ll1&ascsubtag={card_subtag}"
    aff_btn_url = html_lib.escape(raw_aff)
    aff_img_url = html_lib.escape(f"https://www.amazon.in/dp/{asin}?tag=dammaiprasad-21&linkCode=ll1&ascsubtag={card_subtag}")
    aff_title_url = html_lib.escape(f"https://www.amazon.in/dp/{asin}?tag=dammaiprasad-21&linkCode=ll1&ascsubtag={card_subtag}")
    aff_price_url = html_lib.escape(f"https://www.amazon.in/dp/{asin}?tag=dammaiprasad-21&linkCode=ll1&ascsubtag={card_subtag}")
    aff_coupon_url = html_lib.escape(f"https://www.amazon.in/dp/{asin}?tag=dammaiprasad-21&linkCode=ll1&ascsubtag={card_subtag}")

    image_url = html_lib.escape(str(d.get("image_url") or f"https://m.media-amazon.com/images/P/{asin}.jpg"))
    is_loot = bool(d.get("is_loot"))
    deal_page_url = f"/deal/{asin}.html"

    badges = []
    if d.get("is_new") or (d.get("age_hours") is not None and d.get("age_hours") <= 2.0):
        badges.append('<span class="badge-tag badge-new">⚡ NEW</span>')
    is_diwali_steal = bool(d.get("is_diwali_steal") or (savings_inr >= 1000 and disc_pct >= 25))
    if is_diwali_steal:
        badges.append('<span class="badge-tag badge-diwali">🔥 DIWALI STEAL</span>')
    if d.get("is_cookie_magnet"):
        badges.append('<span class="badge-tag badge-magnet">⚡ VIRAL DAILY STEAL</span>')
    if is_loot:
        badges.append('<span class="badge-tag badge-loot">🚨 LOOT ALERT</span>')
    if d.get("price_context") == "all_time_low":
        badges.append('<span class="badge-tag badge-drop">📉 ALL-TIME LOW</span>')
    elif d.get("is_further_drop"):
        badges.append('<span class="badge-tag badge-further">🔥 FURTHER DROP</span>')
    if d.get("brand_tier") == "tier1":
        badges.append('<span class="badge-tag badge-tier1">🏆 TIER-1 BRAND</span>')
    if d.get("posted_to_telegram"):
        badges.append('<span class="badge-tag badge-telegram">📲 Featured on Telegram</span>')
    badges_html = "".join(badges)

    updated_at = d.get("updated_at") or int(time.time())
    diff_sec = max(0, int(time.time() - updated_at))
    if diff_sec < 60:
        time_ago = "Just now"
    elif diff_sec < 3600:
        time_ago = f"{diff_sec // 60}m ago"
    elif diff_sec < 86400:
        time_ago = f"{diff_sec // 3600}h ago"
    else:
        time_ago = f"{diff_sec // 86400}d ago"

    # HotUKDeals Degree Meter
    score_val = d.get("score") or 70
    c_disc_val = d.get("coupon_discount") or 0
    base_degrees = round(score_val * 2.4 + (45 if is_loot else 0) + (25 if c_disc_val > 0 else 0))
    hotness_btn = f'''<button class="btn-hotness-degree" data-asin="{asin}" data-base="{base_degrees}" title="Upvote deal hotness">🔥 <span class="degree-count">{base_degrees}°</span></button>'''

    rating = d.get("rating")
    review_count = d.get("review_count")
    rating_html = ""
    if rating:
        rev_str = f'<span class="reviews-count">({review_count:,})</span>' if review_count else ""
        rating_html = f'''
            <div class="rating-chip">
              <span>⭐ {rating}</span>
              {rev_str}
            </div>'''

    mrp_html = f'<span class="original-mrp">₹{mrp:,}</span>' if (mrp and mrp > eff_price) else ""
    disc_html = f'<span class="discount-badge">{disc_pct}% OFF</span>' if disc_pct else ""
    savings_html = f'<div class="savings-line">You save ₹{savings_inr:,}</div>' if savings_inr > 0 else ""

    coupon_claim_banner = ""
    coupon_disc = d.get("coupon_discount")
    coupon_text = d.get("coupon_text")
    promo_code = d.get("promo_code")
    if coupon_disc or coupon_text:
        c_label = coupon_text or f"Apply ₹{coupon_disc:,} coupon"
        c_tag = f'<span class="coupon-discount-tag">(-₹{coupon_disc:,})</span>' if coupon_disc else ""
        coupon_claim_banner = f'''
          <div class="coupon-claim-banner clickable-coupon-pill" data-aff="{aff_coupon_url}" data-promo="{html_lib.escape(str(promo_code or ''))}" title="Click to copy promo code and open Amazon deal">
            <span class="coupon-tick-icon">☑️</span>
            <span class="coupon-claim-text">Tick <b>"{html_lib.escape(str(c_label))}"</b> on Amazon page {c_tag}</span>
          </div>'''

    extra_offers = d.get("extra_offers") or []
    extra_offers_html = ""
    if extra_offers:
        chips = "".join(f'<span class="extra-offer-chip">{html_lib.escape(str(o))}</span>' for o in extra_offers)
        extra_offers_html = f'''
          <div class="extra-offers-row">
            {chips}
          </div>'''

    callouts = []
    if coupon_disc and coupon_disc > 0:
        callouts.append(f'''
          <div class="callout-pill callout-coupon">
            <span>🎟️ Includes ₹{coupon_disc:,} On-Page Coupon</span>
            <span style="font-weight:700;">Applied</span>
          </div>''')
    bank_offer = d.get("bank_offer")
    if bank_offer:
        callouts.append(f'''
          <div class="callout-pill callout-bank" title="{html_lib.escape(str(bank_offer))}">
            <span>💳 {html_lib.escape(str(bank_offer)[:48])}...</span>
          </div>''')
    if promo_code:
        callouts.append(f'''
          <div class="callout-pill callout-promo btn-copy-promo" data-promo="{html_lib.escape(str(promo_code))}" title="Click to copy promo code">
            <span>🎟️ Code: <code style="font-weight:700;">{html_lib.escape(str(promo_code))}</code></span>
            <span class="promo-copy-action">📋 Tap to Copy</span>
          </div>''')
    callouts_html = f'<div class="callouts-list">{"".join(callouts)}</div>' if callouts else ""

    if buy_conf >= 90:
        conf_class = "high"
        conf_text = "Screaming Deal, Buy Now!"
        conf_icon = "🟢"
    elif buy_conf >= 80:
        conf_class = "medium"
        conf_text = "Strong Price Drop"
        conf_icon = "🟢"
    else:
        conf_class = "normal"
        conf_text = "Good Value"
        conf_icon = "🟡"

    hist_avg = d.get("historical_avg")
    trajectory_html = ""
    if hist_avg and hist_avg > eff_price:
        drop_amt = hist_avg - eff_price
        trajectory_html = f'<div class="historical-trajectory-text">📉 <b>₹{drop_amt:,}</b> below tracked average (₹{hist_avg:,})</div>'

    # BuyHatke-Style Price Range Slider Bar
    low_ref = min(eff_price, d.get("historical_min") or eff_price)
    high_ref = max(mrp, d.get("historical_max") or (mrp or int(eff_price * 1.35)))
    if high_ref <= low_ref:
        high_ref = int(low_ref * 1.3)
    spread = max(1, high_ref - low_ref)
    pos_pct = min(92, max(4, round(((eff_price - low_ref) / spread) * 100)))
    price_range_html = f'''
          <div class="buyhatke-range-box" title="Current Price vs Historical Low & MRP">
            <div class="range-labels-row">
              <span class="range-lbl range-low">Low: ₹{low_ref:,}</span>
              <span class="range-lbl range-now">Now: ₹{eff_price:,} ✅</span>
              <span class="range-lbl range-high">MRP: ₹{high_ref:,}</span>
            </div>
            <div class="range-track">
              <div class="range-marker" style="left: {pos_pct}%;" title="Now: ₹{eff_price:,} ({pos_pct}% of range)">●</div>
            </div>
          </div>'''

    # GrabOn-Style Trust Line
    why_buy = "Lowest tracked price + verified seller"
    if c_disc_val > 0 and savings_inr > 0:
        why_buy = f"Save ₹{savings_inr:,} + Extra ₹{c_disc_val:,} Coupon"
    elif savings_inr >= 1000:
        why_buy = f"Save ₹{savings_inr:,} below MRP ({disc_pct}% OFF)"
    elif d.get("brand_tier") == "tier1":
        why_buy = f"Genuine Tier-1 {brand_chip} at verified low"
    elif disc_pct >= 50:
        why_buy = f"Steep {disc_pct}% verified drop"

    is_deal_fresh = bool(d.get("is_fresh", True))
    if d.get("age_hours") is not None and d.get("age_hours") > 24.0:
        is_deal_fresh = False

    trust_check_badge = '<span class="trust-check">✓ Verified Live on Amazon</span>' if is_deal_fresh else '<span class="trust-check trust-warning">⚠️ Price subject to change on Amazon</span>'

    grabon_trust_html = f'''
          <div class="grabon-trust-line">
            {trust_check_badge}
            <span class="trust-sep">•</span>
            <span class="trust-reason">💡 <b>Why Buy:</b> {why_buy}</span>
          </div>'''

    loot_class = "is-loot" if is_loot else ""

    svg_placeholder = "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIzMDAiIGhlaWdodD0iMzAwIiB2aWV3Qm94PSIwIDAgMzAwIDMwMCI+PGRlZnM+PGxpbmVhckdyYWRpZW50IGlkPSJiZyIgeDE9IjAlIiB5MT0iMCUiIHgyPSIxMDAlIiB5Mj0iMTAwJSI+PHN0b3Agb2Zmc2V0PSIwJSIgc3RvcC1jb2xvcj0iI2Y4ZmFmYyIvPjxzdG9wIG9mZnNldD0iMTAwJSIgc3RvcC1jb2xvcj0iI2UyZThmMCIvPjwvbGluZWFyR3JhZGllbnQ+PC9kZWZzPjxyZWN0IHdpZHRoPSIxMDAlIiBoZWlnaHQ9IjEwMCUiIGZpbGw9InVybCgjYmcpIiByeD0iMTYiLz48Y2lyY2xlIGN4PSIxNTAiIGN5PSIxMjAiIHI9IjUwIiBmaWxsPSIjZmVmM2M3Ii8+PHRleHQgeD0iMTUwIiB5PSIxMzIiIGZvbnQtc2l6ZT0iNTIiIHRleHQtYW5jaG9yPSJtaWRkbGUiIGRvbWluYW50LWJhc2VsaW5lPSJtaWRkbGUiPvKaITwvdGV4dD48dGV4dCB4PSIxNTAiIHk9IjIwMCIgZm9udC1zaXplPSIxNSIgZm9udC13ZWlnaHQ9IjcwMCIgZmlsbD0iIzBmMTcyYSIgdGV4dC1hbmNob3I9Im1pZGRsZSIgZm9udC1mYW1pbHk9InNhbnMtc2VyaWYiPkFtYXpvbiBWZXJpZmllZCBEZWFsPC90ZXh0Pjx0ZXh0IHg9IjE1MCIgeT0iMjIyIiBmb250LXNpemU9IjEyIiBmb250LXdlaWdodD0iNjAwIiBmaWxsPSIjNjQ3NDhiIiB0ZXh0LWFuY2hvcj0ibWlkZGxlIiBmb250LWZhbWlseT0ic2Fucy1zZXJpZiI+Q2xpY2sgdG8gZmluZCBvbiBBbWF6b24g4oaXPC90ZXh0Pjwvc3ZnPg=="
    img_fallback = f"if(this.naturalWidth<=1||this.naturalHeight<=1){{this.onload=null;this.src='{svg_placeholder}';}}"

    if card_index < 3:
        img_tag = f'<img src="{image_url}" alt="{title}" class="card-img" fetchpriority="high" decoding="async" width="220" height="220" onload="{img_fallback}" onerror="this.onerror=null;this.src=\'{svg_placeholder}\';">'
    else:
        img_tag = f'<img src="{image_url}" alt="{title}" class="card-img" loading="lazy" decoding="async" width="220" height="220" onload="{img_fallback}" onerror="this.onerror=null;this.src=\'{svg_placeholder}\';">'

    return f'''      <article class="deal-card {loot_class}" data-asin="{asin}">
        <div class="card-top">
          <div class="card-badges">
            {badges_html}
          </div>
          <div class="card-top-actions">
            {hotness_btn}
            <span class="time-ago">{time_ago}</span>
            <button class="btn-watchlist" data-asin="{asin}" title="Save to Watchlist">
              🤍
            </button>
          </div>
        </div>

        <div class="card-media">
          <div class="card-image-wrap">
            <a href="{aff_img_url}" target="_blank" rel="noopener noreferrer" class="card-img-anchor" title="View Deal on Amazon.in">
              {img_tag}
            </a>
          </div>
          <div class="card-meta-bar">
            <span class="brand-chip">{brand_chip}</span>{rating_html}
          </div>
        </div>

        <div class="card-content">
          <a href="{aff_title_url}" target="_blank" rel="noopener noreferrer" class="card-title" title="{title}">
            {title}
          </a>

          <!-- Price Waterfall -->
          <div class="price-waterfall-box">
            <a href="{aff_price_url}" target="_blank" rel="noopener noreferrer" class="price-main-row-link" title="Grab at this price on Amazon" style="text-decoration:none; color:inherit; display:flex; align-items:baseline; gap:8px;">
              <span class="effective-price">₹{eff_price:,}</span>
              {mrp_html}
              {disc_html}
            </a>
            {savings_html}
          </div>

          {price_range_html}
          {coupon_claim_banner}
          {extra_offers_html}
          {callouts_html}

          <!-- Buy Confidence Meter -->
          <div class="confidence-meter-box">
            <div class="confidence-header">
              <span class="confidence-label">
                <span>{conf_icon}</span>
                <span>{conf_text}</span>
              </span>
              <span class="confidence-pct">{buy_conf}% Confidence</span>
            </div>
            <div class="confidence-bar-bg">
              <div class="confidence-bar-fill {conf_class}" style="width: {buy_conf}%"></div>
            </div>
            {trajectory_html}
          </div>

          {grabon_trust_html}
        </div>

        <div class="card-footer">
          <a href="{aff_btn_url}" target="_blank" rel="noopener noreferrer" class="btn-buy-deal">
            <span>{'🛒 Grab Daily Steal on Amazon ↗' if d.get('is_cookie_magnet') else ('🛒 Grab Diwali Deal on Amazon ↗' if (buy_conf >= 80 or d.get('is_diwali_steal') or eff_price >= 2000) else '🛒 Grab Deal on Amazon ↗')}</span>
          </a>
          <button class="btn-card-share" data-asin="{asin}" title="Share Deal via WhatsApp / Copy Link">
            <span>📤 Share</span>
          </button>
          <a href="{deal_page_url}" class="btn-chart-deal" title="View Price History & Keepa Chart">
            <span>📈 Chart</span>
          </a>
        </div>
      </article>'''


def build_structured_data(page_cfg: dict, page_deals: list) -> str:
    """Generate rich Google Schema JSON-LD for WebSite, Organization, BreadcrumbList, and ItemList."""
    canonical = page_cfg["canonical"]
    is_home = page_cfg["filename"] == "index.html"

    graph = [
        {
            "@type": "WebSite",
            "@id": "https://bharatbazaro.com/#website",
            "url": "https://bharatbazaro.com/",
            "name": "BharatBazaro",
            "description": "Live Amazon India Loot Deals, Price Drop Radar & Hidden Coupon Finder",
            "potentialAction": {
                "@type": "SearchAction",
                "target": "https://bharatbazaro.com/?q={search_term_string}",
                "query-input": "required name=search_term_string"
            }
        },
        {
            "@type": "Organization",
            "@id": "https://bharatbazaro.com/#organization",
            "name": "BharatBazaro",
            "url": "https://bharatbazaro.com/",
            "logo": {
                "@type": "ImageObject",
                "url": "https://bharatbazaro.com/icon-512.png",
                "width": 512,
                "height": 512
            },
            "sameAs": [
                "https://t.me/Flash_Deals_Flipkart_Amazon",
                "https://chat.whatsapp.com/GspHsCGTRm29orUszbuvh3?s=cl&p=a&mlu=4&ilr=4&iam=2"
            ]
        }
    ]

    if not is_home and page_cfg.get("breadcrumb_name"):
        graph.append({
            "@type": "BreadcrumbList",
            "@id": f"{canonical}#breadcrumb",
            "itemListElement": [
                {
                    "@type": "ListItem",
                    "position": 1,
                    "name": "Home",
                    "item": "https://bharatbazaro.com/"
                },
                {
                    "@type": "ListItem",
                    "position": 2,
                    "name": page_cfg["breadcrumb_name"],
                    "item": canonical
                }
            ]
        })

    items = []
    for idx, d in enumerate(page_deals[:30], 1):
        eff_price = d.get("effective_price") or d.get("list_price") or 0
        if eff_price <= 0:
            continue
        asin = d.get("asin") or ""
        subtag = page_cfg.get("subtag") or "bb-grid"
        aff_url = f"https://www.amazon.in/dp/{asin}?tag=dammaiprasad-21&linkCode=ll1&ascsubtag={subtag}"
        img_url = d.get("image_url") or f"https://m.media-amazon.com/images/P/{asin}.jpg"
        title = d.get("title") or "Amazon Deal"
        brand = d.get("brand") or "Amazon"

        prod_schema = {
            "@type": "Product",
            "name": title,
            "image": img_url,
            "description": f"{title} - Amazon deal at ₹{eff_price:,}",
            "sku": asin,
            "brand": {
                "@type": "Brand",
                "name": brand
            },
            "offers": {
                "@type": "Offer",
                "priceCurrency": "INR",
                "price": str(eff_price),
                "priceValidUntil": "2026-12-31",
                "availability": "https://schema.org/InStock",
                "url": aff_url
            }
        }
        if d.get("rating"):
            prod_schema["aggregateRating"] = {
                "@type": "AggregateRating",
                "ratingValue": str(d.get("rating")),
                "reviewCount": str(d.get("review_count") or 1)
            }
        items.append({
            "@type": "ListItem",
            "position": idx,
            "item": prod_schema
        })

    if items:
        graph.append({
            "@type": "ItemList",
            "@id": f"{canonical}#itemlist",
            "name": page_cfg["title"],
            "numberOfItems": len(items),
            "itemListElement": items
        })

    data = {
        "@context": "https://schema.org",
        "@graph": graph
    }
    return f'<script type="application/ld+json">\n{json.dumps(data, indent=2, ensure_ascii=False)}\n  </script>'


def filter_deals_for_page(page_cfg: dict, all_deals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Filter deals matching category, price threshold, or filter, backfilling so pages are never thin."""
    slug = page_cfg.get("category_slug")
    filt = page_cfg.get("initial_filter")
    max_price = page_cfg.get("initial_max_price")

    if filt == "loot":
        loots = [d for d in all_deals if d.get("is_loot")]
        steep = [d for d in all_deals if not d.get("is_loot") and (d.get("effective_discount_pct") or d.get("discount_pct") or 0) >= 50]
        combined = loots + [d for d in steep if d not in loots]
        if len(combined) < 15:
            combined += [d for d in all_deals if d not in combined]
        return combined[:30]

    if filt == "coupons":
        coupons = [
            d for d in all_deals
            if (d.get("coupon_discount") and d.get("coupon_discount") > 0)
            or d.get("coupon_text")
            or d.get("promo_code")
            or d.get("bank_offer")
        ]
        if len(coupons) < 10:
            extra = [d for d in all_deals if d not in coupons and (d.get("effective_discount_pct") or d.get("discount_pct") or 0) >= 40]
            coupons += extra
        if len(coupons) < 10:
            coupons += [d for d in all_deals if d not in coupons]
        return coupons[:30]

    if max_price is not None:
        matched = [d for d in all_deals if (d.get("effective_price") or d.get("list_price") or 0) <= max_price]
        if len(matched) < 6:
            # Backfill with nearest lowest price deals
            sorted_all = sorted(all_deals, key=lambda x: (x.get("effective_price") or x.get("list_price") or 999999))
            matched = sorted_all[:max(6, len(matched))]
        else:
            matched.sort(key=lambda x: (x.get("effective_price") or x.get("list_price") or 999999))
        return matched[:30]

    if not slug or slug == "all":
        return all_deals[:30]

    matched = []
    for d in all_deals:
        cat = (d.get("category") or "").lower()
        t = (d.get("title") or "").lower()
        if slug == "mobiles":
            if "mobile" in cat or any(w in t for w in ("phone", "iphone", "samsung", "oneplus", "mobile", "case", "charger", "power bank", "tempered glass")):
                matched.append(d)
        elif slug == "electronics":
            if any(c in cat for c in ("electronic", "audio", "laptop", "computing")) or any(w in t for w in ("headphone", "earphone", "audio", "speaker", "soundbar", "laptop", "monitor", "keyboard", "mouse", "ssd", "smartwatch")):
                matched.append(d)
        elif slug == "home":
            if "home" in cat or "kitchen" in cat or any(w in t for w in ("cooker", "fryer", "air fryer", "pan", "bottle", "purifier", "mixer", "grinder", "kitchen", "kettle", "bedsheet", "curtain", "appliance", "blender", "iron", "toaster")):
                matched.append(d)
        elif slug == "fashion":
            if "fashion" in cat or "footwear" in cat or any(w in t for w in ("shirt", "shoe", "shoes", "sneaker", "jeans", "saree", "kurta", "jacket", "sandals", "wallet", "backpack", "watch", "watches")):
                matched.append(d)
        elif slug == "grocery":
            if any(c in cat for c in ("grocery", "personal care", "beauty")) or any(w in t for w in ("shampoo", "cream", "serum", "perfume", "soap", "tea", "coffee", "biscuit", "almond", "oil", "chocolate", "snack", "diaper", "sunscreen", "lotion", "moisturizer")):
                matched.append(d)
        elif slug == "beauty":
            if any(c in cat for c in ("beauty", "personal care", "grooming", "skincare", "haircare")) or any(w in t for w in ("body wash", "shampoo", "cream", "serum", "lotion", "moisturizer", "sunscreen", "cleanser", "facewash", "face wash", "perfume", "deodorant", "soap", "toothpaste", "colgate", "dove", "nivea", "cetaphil", "minimalist", "derma co", "cerave", "l'oreal", "garnier", "neutrogena", "pears", "plum", "lakme", "maybelline", "tresemme", "listerine", "dettol", "savlon", "gillette", "trimmer", "grooming")):
                matched.append(d)

    # Backfill with top featured deals if category has fewer than 6 deals so Googlebot gets full indexable content
    if len(matched) < 6:
        backfill = [d for d in all_deals if d not in matched]
        matched += backfill

    return matched[:30]


def build_head_html(page_cfg: dict, page_deals: list) -> str:
    """Build a clean, standard, Google-compliant head block for any page."""
    ld_json = build_structured_data(page_cfg, page_deals)
    return f'''<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{page_cfg["title"]}</title>
  <meta name="description" content="{page_cfg["meta_description"]}">
  <meta name="keywords" content="Amazon deals, price drop alert, loot deals, coupons, India deals, Keepa charts, BuyHatke, PriceHistory">
  <link rel="canonical" href="{page_cfg["canonical"]}">

  <!-- OpenGraph / Social Sharing -->
  <meta property="og:type" content="website">
  <meta property="og:url" content="{page_cfg["canonical"]}">
  <meta property="og:title" content="{page_cfg["title"]}">
  <meta property="og:description" content="{page_cfg["meta_description"]}">
  <meta property="og:image" content="https://bharatbazaro.com/og-banner.png">
  <meta property="og:image:width" content="1200">
  <meta property="og:image:height" content="630">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{page_cfg["title"]}">
  <meta name="twitter:description" content="{page_cfg["meta_description"]}">
  <meta name="twitter:image" content="https://bharatbazaro.com/og-banner.png">

  <!-- Favicons, Touch Icons & PWA Manifest -->
  <link rel="icon" href="/favicon.ico" sizes="48x48">
  <link rel="icon" href="/favicon.svg" type="image/svg+xml">
  <link rel="icon" type="image/png" sizes="192x192" href="/icon-192.png">
  <link rel="icon" type="image/png" sizes="512x512" href="/icon-512.png">
  <link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">
  <link rel="manifest" href="/manifest.json">
  <link rel="alternate" type="application/rss+xml" title="BharatBazaro Live Amazon India Loot Deals RSS" href="https://bharatbazaro.com/feed.xml">
  <meta name="theme-color" content="#0f172a">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">

  <!-- Schema.org Structured Data -->
  {ld_json}

  <!-- Fonts & Stylesheet -->
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;600&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="style.css">
</head>'''


def render_top5_quick_compare_table(deals: List[Dict[str, Any]]) -> str:
    """Pre-render MySmartPrice-style Top 5 Quick-Compare HTML table for SEO & Featured Snippets."""
    top5 = [d for d in deals if (d.get("effective_price") or d.get("list_price") or 0) > 0][:5]
    if not top5:
        return ""

    rows = []
    for rank, d in enumerate(top5, 1):
        asin = d.get("asin")
        title = html_lib.escape(html_lib.unescape(str(d.get("title") or "Amazon Deal")).strip())
        eff_price = d.get("effective_price") or d.get("list_price") or 0
        mrp = d.get("mrp") or 0
        disc = d.get("effective_discount_pct") or d.get("discount_pct") or 0
        conf = d.get("buy_confidence") or 80
        aff_url = f"https://www.amazon.in/dp/{asin}?tag=dammaiprasad-21&linkCode=ll1&ascsubtag=bb-table"
        deal_url = f"/deal/{asin}.html"

        rows.append(f"""          <tr style="border-bottom: 1px solid var(--border-card);">
            <td style="padding: 10px; text-align:center; font-weight:800; color:#10b981;">#{rank}</td>
            <td style="padding: 10px;">
              <a href="{deal_url}" style="font-weight:600; color:var(--text-main); text-decoration:none;">{title}</a>
            </td>
            <td style="padding: 10px; font-weight:700; color:#10b981; white-space:nowrap;">₹{eff_price:,}</td>
            <td style="padding: 10px; font-weight:700; color:#ef4444; white-space:nowrap;">{disc}% OFF</td>
            <td style="padding: 10px; white-space:nowrap;"><span style="color:#10b981; font-weight:700;">🟢 {conf}%</span></td>
            <td style="padding: 10px; text-align:center; white-space:nowrap;">
              <a href="{aff_url}" target="_blank" rel="noopener noreferrer" class="btn btn-primary" style="padding:5px 12px; font-size:0.8rem; border-radius:14px; text-decoration:none; display:inline-block; font-weight:700;">🛒 View Deal ↗</a>
            </td>
          </tr>""")

    rows_html = "\n".join(rows)
    return f"""    <!-- MySmartPrice-Style Top 5 Quick-Compare Table -->
    <section class="quick-compare-section" id="top5CompareSection" style="margin-top: 36px; margin-bottom: 24px;">
      <div class="section-header" style="margin-bottom: 16px;">
        <h2 style="font-size: 1.35rem; font-weight: 800; display: flex; align-items: center; gap: 8px;">
          <span>📊 Today's Top 5 Verified Deals Quick-Compare List</span>
        </h2>
        <p style="color: var(--text-secondary); font-size: 0.88rem; margin-top: 4px;">
          Instant price drop comparison updated live every hour by BharatBazaro AI deal radar.
        </p>
      </div>
      <div class="table-responsive" style="overflow-x: auto; background: var(--bg-card); border: 1px solid var(--border-card); border-radius: var(--radius-md); padding: 12px;">
        <table class="quick-compare-table" style="width: 100%; border-collapse: collapse; text-align: left; font-size: 0.9rem;">
          <thead>
            <tr style="border-bottom: 2px solid var(--border-card); color: var(--text-muted); font-size: 0.78rem; text-transform: uppercase;">
              <th style="padding: 10px; text-align: center;">Rank</th>
              <th style="padding: 10px;">Product Name</th>
              <th style="padding: 10px;">Best Effective Price</th>
              <th style="padding: 10px;">Discount</th>
              <th style="padding: 10px;">Buy Confidence</th>
              <th style="padding: 10px; text-align: center;">Direct Deal Link</th>
            </tr>
          </thead>
          <tbody>
{rows_html}
          </tbody>
        </table>
      </div>
    </section>"""


def render_top3_spotlight_strip(deals: List[Dict[str, Any]]) -> str:
    """Render Wirecutter-style Top 3 Must-Buy Picks spotlight strip."""
    if not deals or len(deals) < 3:
        return ""

    pick1_candidates = sorted(deals, key=lambda d: ((1 if d.get("brand_tier") == "tier1" else 0), d.get("trending_score") or d.get("score") or 0, d.get("effective_discount_pct") or 0), reverse=True)
    pick1 = pick1_candidates[0] if pick1_candidates else deals[0]

    under_300 = [d for d in deals if (d.get("effective_price") or 999999) <= 299 and d != pick1]
    if under_300:
        pick2 = sorted(under_300, key=lambda d: (d.get("effective_discount_pct") or 0, -(d.get("effective_price") or 0)), reverse=True)[0]
    else:
        rem = [d for d in deals if d != pick1]
        pick2 = sorted(rem, key=lambda d: (d.get("effective_price") or 999999))[0] if rem else deals[0]

    coupon_deals = [d for d in deals if ((d.get("coupon_discount") or 0) > 0 or d.get("coupon_text")) and d not in (pick1, pick2)]
    if coupon_deals:
        pick3 = coupon_deals[0]
    else:
        rem2 = [d for d in deals if d not in (pick1, pick2)]
        pick3 = sorted(rem2, key=lambda d: (d.get("effective_discount_pct") or 0), reverse=True)[0] if rem2 else deals[0]

    picks = [
        ("🏆 #1 OVERALL SCREAMER", pick1),
        ("🪙 #1 BUDGET COOKIE-DROPPER", pick2),
        ("🎟️ #1 HIDDEN COUPON PICK", pick3),
    ]

    cards_html = []
    svg_placeholder = "data:image/svg+xml;utf8,<svg xmlns=\'http://www.w3.org/2000/svg\' width=\'120\' height=\'120\' viewBox=\'0 0 120 120\'><rect width=\'100%\' height=\'100%\' fill=\'%23f1f5f9\'/><text x=\'50%\' y=\'50%\' font-size=\'32\' text-anchor=\'middle\' dominant-baseline=\'middle\'>⚡</text></svg>"

    for ribbon, d in picks:
        asin = d.get("asin") or ""
        title = html_lib.escape(html_lib.unescape(str(d.get("title") or "Amazon Deal")).strip())
        eff_price = d.get("effective_price") or d.get("list_price") or 0
        mrp = d.get("mrp") or 0
        disc = d.get("effective_discount_pct") or d.get("discount_pct") or 0
        img = html_lib.escape(str(d.get("image_url") or f"https://m.media-amazon.com/images/P/{asin}.jpg"))
        aff_url = f"https://www.amazon.in/dp/{asin}?tag=dammaiprasad-21&linkCode=ll1&ascsubtag=bb-top3"
        deal_url = f"/deal/{asin}.html"

        mrp_str = f'<span style="font-size:0.78rem; text-decoration:line-through; color:var(--text-muted);">₹{mrp:,}</span>' if (mrp and mrp > eff_price) else ""
        disc_str = f'<span style="font-size:0.75rem; font-weight:700; color:#ef4444; background:rgba(239,68,68,0.12); padding:2px 6px; border-radius:4px;">{disc}% OFF</span>' if disc > 0 else ""

        cards_html.append(f'''        <div class="spotlight-mini-card">
          <div class="spotlight-ribbon">{ribbon}</div>
          <div style="display:flex; gap:12px; align-items:center; margin-bottom:10px;">
            <a href="{aff_url}" target="_blank" rel="noopener noreferrer" style="flex-shrink:0; width:70px; height:70px; display:flex; align-items:center; justify-content:center; background:#ffffff; border-radius:8px; overflow:hidden; border:1px solid var(--border-card);">
              <img src="{img}" alt="{title}" style="max-height:60px; max-width:60px; object-fit:contain;" loading="lazy" onerror="this.src=\'{svg_placeholder}\';">
            </a>
            <div style="flex:1; min-width:0;">
              <a href="{aff_url}" target="_blank" rel="noopener noreferrer" style="font-size:0.86rem; font-weight:700; color:var(--text-main); text-decoration:none; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; line-height:1.3; margin-bottom:4px;">
                {title}
              </a>
              <div style="display:flex; align-items:baseline; gap:6px; flex-wrap:wrap;">
                <span style="font-size:1.1rem; font-weight:800; color:#10b981;">₹{eff_price:,}</span>
                {mrp_str}
                {disc_str}
              </div>
            </div>
          </div>
          <div style="display:flex; gap:8px;">
            <a href="{aff_url}" target="_blank" rel="noopener noreferrer" class="btn btn-primary" style="flex:1; text-align:center; padding:7px 10px; font-size:0.82rem; font-weight:700; border-radius:var(--radius-full); text-decoration:none;">🛒 Grab Now ↗</a>
            <a href="{deal_url}" style="padding:7px 10px; font-size:0.82rem; font-weight:600; border-radius:var(--radius-full); background:var(--bg-input); color:var(--text-main); border:1px solid var(--border-card); text-decoration:none;">📈</a>
          </div>
        </div>''')

    cards_joined = "\n".join(cards_html)
    return f'''    <!-- Wirecutter-Style Top 3 Must-Buy Picks Hero Spotlight Strip -->
    <section id="top3SpotlightStrip" class="top3-spotlight-strip" aria-label="Today's Top 3 Must-Buy Picks">
      <div class="spotlight-header">
        <h2 class="spotlight-title">
          <span>⚡ TODAY'S TOP 3 MUST-BUY PICKS</span>
        </h2>
        <span style="font-size:0.8rem; color:var(--text-secondary); font-weight:600;">Updated Live from 24/7 AI Radar</span>
      </div>
      <div class="spotlight-cards-grid">
{cards_joined}
      </div>
    </section>'''


def generate_page_html(
    base_html: str,
    page_cfg: dict,
    page_deals: List[Dict[str, Any]],
    all_deals: List[Dict[str, Any]],
    stats: Dict[str, Any]
) -> str:
    """Generate complete standalone static HTML for a given page configuration."""
    html_out = base_html

    # 1. Cleanly replace the entire <head>...</head> section
    new_head = build_head_html(page_cfg, page_deals)
    html_out = re.sub(r'<head>.*?</head>', new_head, html_out, count=1, flags=re.DOTALL)

    # 2. Hero Title & Subtitle
    html_out = re.sub(
        r'<h1\s+class=["\']hero-title["\']>.*?</h1>',
        f'<h1 class="hero-title">\n          {page_cfg["hero_title"]}\n        </h1>',
        html_out,
        count=1,
        flags=re.DOTALL
    )
    html_out = re.sub(
        r'<p\s+class=["\']hero-subtitle["\']>.*?</p>',
        f'<p class="hero-subtitle">\n          {page_cfg["hero_subtitle"]}\n        </p>',
        html_out,
        count=1,
        flags=re.DOTALL
    )

    # 3. Stats values
    html_out = re.sub(r'(<span\s+class=["\']stat-val["\']\s+id=["\']statLiveDeals["\']>)[^<]*(</span>)', f'\\g<1>{stats.get("total_live_deals", len(all_deals))}\\g<2>', html_out)
    html_out = re.sub(r'(<span\s+class=["\']stat-val["\']\s+id=["\']statLootCount["\']>)[^<]*(</span>)', f'\\g<1>{stats.get("loot_count", 0)}\\g<2>', html_out)
    html_out = re.sub(r'(<span\s+class=["\']stat-val["\']\s+id=["\']statMaxDiscount["\']>)[^<]*(</span>)', f'\\g<1>{stats.get("max_discount_pct", 0)}%\\g<2>', html_out)
    total_sav = stats.get("total_savings_available", 0)
    sav_str = f'₹{(total_sav / 100000):.2f}L' if total_sav >= 100000 else f'₹{total_sav:,}'
    html_out = re.sub(r'(<span\s+class=["\']stat-val["\']\s+id=["\']statTotalSavings["\']>)[^<]*(</span>)', f'\\g<1>{sav_str}\\g<2>', html_out)

    # 4. Tab counts
    html_out = re.sub(r'(<span\s+class=["\']tab-count["\']\s+id=["\']countAll["\']>)[^<]*(</span>)', f'\\g<1>{len(all_deals)}\\g<2>', html_out)
    html_out = re.sub(r'(<span\s+class=["\']tab-count["\']\s+id=["\']countLoot["\']>)[^<]*(</span>)', f'\\g<1>{stats.get("loot_count", 0)}\\g<2>', html_out)
    html_out = re.sub(r'(<span\s+class=["\']tab-count["\']\s+id=["\']countDrops["\']>)[^<]*(</span>)', f'\\g<1>{stats.get("all_time_lows_count", 0)}\\g<2>', html_out)
    html_out = re.sub(r'(<span\s+class=["\']tab-count["\']\s+id=["\']countCoupons["\']>)[^<]*(</span>)', f'\\g<1>{stats.get("coupon_deals_count", 0)}\\g<2>', html_out)
    html_out = re.sub(r'(<span\s+class=["\']tab-count["\']\s+id=["\']countTier1["\']>)[^<]*(</span>)', f'\\g<1>{sum(1 for d in all_deals if d.get("brand_tier") == "tier1")}\\g<2>', html_out)

    # 5. Category pills, price pills & active states
    cat_slug = page_cfg.get("category_slug")
    init_filter = page_cfg.get("initial_filter")
    init_max_price = page_cfg.get("initial_max_price")

    # Ensure Electronics pill exists
    if 'data-category="Electronics & Audio"' not in html_out:
        html_out = html_out.replace(
            '<button class="cat-pill" data-category="Mobiles & Accessories">📱 Mobiles</button>',
            '<button class="cat-pill" data-category="Mobiles & Accessories">📱 Mobiles</button>\n        <button class="cat-pill" data-category="Electronics & Audio">⚡ Electronics</button>'
        )

    # Reset active on category pills
    html_out = re.sub(r'class="cat-pill active"', 'class="cat-pill"', html_out)
    slug_to_cat = {
        "all": "all",
        "mobiles": "Mobiles & Accessories",
        "electronics": "Electronics & Audio",
        "home": "Home & Kitchen",
        "fashion": "Fashion & Footwear",
        "grocery": "Personal Care & Grocery",
        "beauty": "Personal Care & Grocery",
    }
    target_cat = slug_to_cat.get(cat_slug, "all") if cat_slug else "all"
    html_out = html_out.replace(f'class="cat-pill" data-category="{target_cat}"', f'class="cat-pill active" data-category="{target_cat}"')

    # Reset and set active filter tab
    html_out = re.sub(r'class="filter-tab active"', 'class="filter-tab"', html_out)
    html_out = re.sub(r'class="filter-tab loot-tab active"', 'class="filter-tab loot-tab"', html_out)
    if init_filter == "loot":
        html_out = html_out.replace('class="filter-tab loot-tab" data-tab="loot"', 'class="filter-tab loot-tab active" data-tab="loot"')
    elif init_filter == "coupons":
        html_out = html_out.replace('class="filter-tab" data-tab="coupons"', 'class="filter-tab active" data-tab="coupons"')
    else:
        html_out = html_out.replace('class="filter-tab" data-tab="all"', 'class="filter-tab active" data-tab="all"')

    # Reset and set active price pill
    if init_max_price is not None:
        html_out = re.sub(r'class="price-pill active"', 'class="price-pill"', html_out)
        html_out = html_out.replace(f'class="price-pill" data-price="{init_max_price}"', f'class="price-pill active" data-price="{init_max_price}"')

    # 6. Inject deals inside #dealsGrid
    page_subtag = page_cfg.get("subtag")
    cards_html = "\n".join(render_deal_card_html(d, idx, subtag=page_subtag) for idx, d in enumerate(page_deals))
    html_out = re.sub(
        r'(<div\s+id=["\']dealsGrid["\'][^>]*>).*?(</div>\s*<!--\s*Empty State)',
        rf'\g<1>\n{cards_html}\n    \g<2>',
        html_out,
        count=1,
        flags=re.DOTALL
    )

    # Inject Wirecutter Top 3 Spotlight Strip above #dealsGrid on index.html
    if page_cfg.get("filename") == "index.html" and page_deals:
        top3_html = render_top3_spotlight_strip(all_deals or page_deals)
        if '<section id="top3SpotlightStrip"' in html_out:
            html_out = re.sub(
                r'<section\s+id=["\']top3SpotlightStrip["\'][^>]*>.*?</section>',
                top3_html.strip(),
                html_out,
                count=1,
                flags=re.DOTALL
            )
        else:
            html_out = html_out.replace(
                '<div id="dealsGrid"',
                f'{top3_html}\n    <div id="dealsGrid"'
            )

    # Inject MySmartPrice Top 5 Quick-Compare table above guides section or before </main>
    top5_table = render_top5_quick_compare_table(page_deals)
    if '<section class="quick-compare-section"' in html_out:
        html_out = re.sub(
            r'<section\s+class=["\']quick-compare-section["\'][^>]*>.*?</section>',
            top5_table.strip(),
            html_out,
            count=1,
            flags=re.DOTALL
        )
    elif '<!-- 📚 Latest AI Buying Guides & Deal Tricks -->' in html_out:
        html_out = html_out.replace(
            '<!-- 📚 Latest AI Buying Guides & Deal Tricks -->',
            f'{top5_table}\n\n    <!-- 📚 Latest AI Buying Guides & Deal Tricks -->'
        )
    elif '<section class="guides-section"' in html_out:
        html_out = html_out.replace(
            '<section class="guides-section"',
            f'{top5_table}\n\n    <section class="guides-section"'
        )
    elif '</main>' in html_out:
        html_out = html_out.replace(
            '</main>',
            f'{top5_table}\n  </main>'
        )

    # 7. Visible count
    html_out = re.sub(r'(<span\s+id=["\']visibleCount["\']\s+class=["\']font-bold["\']>)[^<]*(</span>)', f'\\g<1>{len(page_deals)}\\g<2>', html_out)

    # 8. Ensure comprehensive footer links (Category + Price Stores + RSS)
    if 'footer-stores' not in html_out:
        store_links_block = '''
        <div class="footer-links footer-stores" style="margin-top: 10px; display: flex; flex-wrap: wrap; gap: 12px;">
          <a href="/under-99.html" class="footer-link">⚡ ₹99 Store</a>
          <a href="/under-299.html" class="footer-link">🏷️ Under ₹299 Deals</a>
          <a href="/under-499.html" class="footer-link">🔥 Under ₹499 Loots</a>
          <a href="/under-999.html" class="footer-link">🎧 Under ₹999 Deals</a>
          <a href="/coupons.html" class="footer-link">🎟️ Hidden Coupons</a>
          <a href="/feed.xml" class="footer-link" target="_blank" rel="alternate" type="application/rss+xml">📡 RSS 2.0 Feed</a>
        </div>'''
        html_out = re.sub(r'(<div class="footer-links">.*?</div>)', r'\g<1>' + store_links_block, html_out, count=1, flags=re.DOTALL)

    # Ensure home.html is in footer links
    if '<a href="/home.html"' not in html_out:
        html_out = html_out.replace(
            '<a href="/mobiles.html" class="footer-link">📱 Mobiles</a>',
            '<a href="/mobiles.html" class="footer-link">📱 Mobiles</a>\n          <a href="/home.html" class="footer-link">🏠 Home & Kitchen</a>'
        )
    if '/beauty.html' not in html_out:
        html_out = html_out.replace(
            '<a href="/grocery.html" class="footer-link">🧴 Grocery</a>',
            '<a href="/grocery.html" class="footer-link">🧴 Grocery</a>\n          <a href="/beauty.html" class="footer-link">💄 Beauty & Grooming</a>'
        )

    # 9. Clean any existing window.INITIAL_* scripts
    html_out = re.sub(r'\s*<script>\s*window\.INITIAL_[^<]*</script>', '', html_out)

    # 10. Initial routing injection script
    script_injections = []
    if cat_slug and cat_slug != "all":
        script_injections.append(f'<script>window.INITIAL_CATEGORY = "{cat_slug}";</script>')
    if init_filter:
        script_injections.append(f'<script>window.INITIAL_FILTER = "{init_filter}";</script>')
    if init_max_price is not None:
        script_injections.append(f'<script>window.INITIAL_MAX_PRICE = {init_max_price};</script>')

    if script_injections:
        injection_code = "  " + "\n  ".join(script_injections) + "\n  <script src=\"app.js\">"
        html_out = re.sub(r'\s*<script\s+src=["\']app\.js["\']>', f'\n{injection_code}', html_out, count=1)

    # 11. Inject BazarAI Chat Widget
    if 'id="bazaroAiWidget"' not in html_out:
        html_out = html_out.replace('</body>', f'''{BAZAR_WIDGET_HTML}\n</body>''')
    elif 'bazaro_ai.js' not in html_out:
        html_out = html_out.replace('</body>', '  <script src="/bazaro_ai.js"></script>\n</body>')

    return html_out


def render_product_page_html(
    deal: Dict[str, Any],
    related_deals: List[Dict[str, Any]],
    stats: Dict[str, Any],
) -> str:
    """
    Generate an individual programmatic SEO product page for /deal/{asin}.html.
    Includes Keepa price history chart, price waterfall, how-to-claim checklist, and rich Product JSON-LD schema.
    """
    asin = html_lib.escape(str(deal.get("asin") or ""))
    raw_title = deal.get("title") or f"Amazon Deal ({asin})"
    title = html_lib.escape(raw_title)
    eff_price = deal.get("effective_price") or deal.get("list_price") or 0
    mrp = deal.get("mrp") or 0
    disc_pct = deal.get("effective_discount_pct") or deal.get("discount_pct") or 0
    savings_inr = deal.get("savings_inr") or (max(0, mrp - eff_price) if mrp > eff_price else 0)
    buy_conf = deal.get("buy_confidence") or 75
    brand = html_lib.escape(str(deal.get("brand") or deal.get("category") or "Amazon"))
    category = html_lib.escape(str(deal.get("category") or "Electronics & Audio"))
    rating = deal.get("rating") or 4.3
    review_count = deal.get("review_count") or 150

    raw_aff = str(deal.get("affiliate_url") or f"https://www.amazon.in/dp/{asin}?tag=dammaiprasad-21&linkCode=ll1")
    if "ascsubtag=" in raw_aff:
        raw_aff = re.sub(r"ascsubtag=[^&]+", "ascsubtag=bb-seo", raw_aff)
    else:
        raw_aff += ("&" if "?" in raw_aff else "?") + "ascsubtag=bb-seo"
    affiliate_url = html_lib.escape(raw_aff)

    image_url = html_lib.escape(str(deal.get("image_url") or f"https://m.media-amazon.com/images/P/{asin}.jpg"))
    canonical = f"https://bharatbazaro.com/deal/{asin}.html"

    page_title = f"{raw_title} — ₹{eff_price:,} ({disc_pct}% OFF) Lowest Price & Keepa Price History India | BharatBazaro"
    meta_desc = f"Buy {raw_title} at ₹{eff_price:,} (MRP ₹{mrp:,}, Save ₹{savings_inr:,}) on Amazon India. Check 90-day Keepa price history chart, BuyHatke confidence score ({buy_conf}%), and hidden on-page coupons."

    keepa_chart_url = f"https://graph.keepa.com/pricehistory.png?asin={asin}&domain=in&salesrank=1&range=90"

    hist_min = deal.get("historical_min") or eff_price
    hist_avg = deal.get("historical_avg") or (int(eff_price * 1.18) if mrp <= eff_price else int((mrp + eff_price) / 2))
    hist_max = deal.get("historical_max") or max(mrp, eff_price)

    # Badges
    badges = []
    if deal.get("is_loot"):
        badges.append('<span class="badge-tag badge-loot">🚨 LOOT ALERT</span>')
    if deal.get("price_context") == "all_time_low":
        badges.append('<span class="badge-tag badge-drop">📉 ALL-TIME LOW</span>')
    elif deal.get("is_further_drop"):
        badges.append('<span class="badge-tag badge-further">🔥 FURTHER DROP</span>')
    if deal.get("brand_tier") == "tier1":
        badges.append('<span class="badge-tag badge-tier1">🏆 TIER-1 BRAND</span>')
    if deal.get("posted_to_telegram"):
        badges.append('<span class="badge-tag badge-telegram">📲 Featured on Telegram</span>')
    badges_html = " ".join(badges)

    # Coupon Claim HTML
    coupon_claim_html = ""
    coupon_disc = deal.get("coupon_discount")
    coupon_text = deal.get("coupon_text")
    if coupon_disc or coupon_text:
        c_label = coupon_text or f"Apply ₹{coupon_disc:,} coupon"
        coupon_claim_html = f'''
        <div class="coupon-claim-banner" style="margin: 16px 0; padding: 12px 16px; border-radius: 12px; background: rgba(16, 185, 129, 0.12); border: 1px dashed #10b981;">
          <span style="font-size: 1.2rem;">☑️</span>
          <span style="color: #34d399; font-weight: 600;">How to claim coupon: Tick <b>"{html_lib.escape(str(c_label))}"</b> checkbox on Amazon India product page before adding to cart.</span>
        </div>'''

    # Structured Data (JSON-LD)
    schema_data = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "WebSite",
                "@id": "https://bharatbazaro.com/#website",
                "url": "https://bharatbazaro.com/",
                "name": "BharatBazaro",
                "description": "Live Amazon India Loot Deals, Price Drop Radar & Hidden Coupon Finder"
            },
            {
                "@type": "Organization",
                "@id": "https://bharatbazaro.com/#organization",
                "name": "BharatBazaro",
                "url": "https://bharatbazaro.com/",
                "logo": "https://bharatbazaro.com/icon-512.png"
            },
            {
                "@type": "BreadcrumbList",
                "@id": f"{canonical}#breadcrumb",
                "itemListElement": [
                    {
                        "@type": "ListItem",
                        "position": 1,
                        "name": "Home",
                        "item": "https://bharatbazaro.com/"
                    },
                    {
                        "@type": "ListItem",
                        "position": 2,
                        "name": category,
                        "item": "https://bharatbazaro.com/deals.html"
                    },
                    {
                        "@type": "ListItem",
                        "position": 3,
                        "name": raw_title[:40],
                        "item": canonical
                    }
                ]
            },
            {
                "@type": "Product",
                "@id": f"{canonical}#product",
                "name": raw_title,
                "image": image_url,
                "description": meta_desc,
                "sku": asin,
                "brand": {
                    "@type": "Brand",
                    "name": brand
                },
                "aggregateRating": {
                    "@type": "AggregateRating",
                    "ratingValue": str(rating),
                    "reviewCount": str(review_count)
                },
                "offers": {
                    "@type": "Offer",
                    "url": affiliate_url,
                    "priceCurrency": "INR",
                    "price": str(eff_price),
                    "priceValidUntil": "2026-12-31",
                    "itemCondition": "https://schema.org/NewCondition",
                    "availability": "https://schema.org/InStock",
                    "seller": {
                        "@type": "Organization",
                        "name": "Amazon India"
                    }
                }
            }
        ]
    }
    schema_json = json.dumps(schema_data, indent=2, ensure_ascii=False)

    # Related Deals Cards HTML
    related_cards_html = "\n".join(render_deal_card_html(d, idx + 3) for idx, d in enumerate(related_deals[:6]))

    return f'''<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html_lib.escape(page_title)}</title>
  <meta name="description" content="{html_lib.escape(meta_desc)}">
  <link rel="canonical" href="{canonical}">

  <!-- OpenGraph / Social Sharing -->
  <meta property="og:type" content="product">
  <meta property="og:url" content="{canonical}">
  <meta property="og:title" content="{html_lib.escape(page_title)}">
  <meta property="og:description" content="{html_lib.escape(meta_desc)}">
  <meta property="og:image" content="{image_url}">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{html_lib.escape(page_title)}">
  <meta name="twitter:description" content="{html_lib.escape(meta_desc)}">
  <meta name="twitter:image" content="{image_url}">

  <!-- Favicons & Icons -->
  <link rel="icon" href="/favicon.ico" sizes="48x48">
  <link rel="icon" href="/favicon.svg" type="image/svg+xml">
  <link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">
  <link rel="manifest" href="/manifest.json">
  <link rel="alternate" type="application/rss+xml" title="BharatBazaro Live Amazon India Loot Deals RSS" href="https://bharatbazaro.com/feed.xml">
  <meta name="theme-color" content="#0f172a">

  <!-- Schema.org Product Structured Data -->
  <script type="application/ld+json">
{schema_json}
  </script>

  <!-- Google Fonts & Stylesheet -->
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;600&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="../style.css">
</head>
<body>
  <!-- Header -->
  <header class="site-header">
    <div class="container header-inner">
      <div class="header-left">
        <a href="/" class="brand-link" title="BharatBazaro Home">
          <span class="brand-icon">⚡</span>
          <span class="brand-name">Bharat<span class="brand-accent">Bazaro</span></span>
        </a>
      </div>
      <div class="header-center">
        <span class="badge-status online">● AI Deal Radar Active</span>
      </div>
      <div class="header-right">
        <a href="https://t.me/Flash_Deals_Flipkart_Amazon" target="_blank" rel="noopener noreferrer" class="btn btn-telegram btn-sm">
          <span>📢 Join Telegram</span>
        </a>
        <a href="https://chat.whatsapp.com/GspHsCGTRm29orUszbuvh3?s=cl&p=a&mlu=4&ilr=4&iam=2" target="_blank" rel="noopener noreferrer" class="btn btn-whatsapp btn-sm" title="Join WhatsApp Deals">
          <span>💬 Join WhatsApp</span>
        </a>
        <a href="/" class="btn btn-outline btn-sm">
          <span>🌐 Browse All Deals</span>
        </a>
      </div>
    </div>
  </header>

  <!-- Breadcrumbs -->
  <nav class="container" style="padding: 16px 20px; font-size: 0.85rem; color: var(--text-secondary);" aria-label="Breadcrumb">
    <a href="/" style="color: var(--color-emerald-light); text-decoration: none;">Home</a> &gt;
    <a href="/deals.html" style="color: var(--color-emerald-light); text-decoration: none;">{category}</a> &gt;
    <span style="color: var(--text-muted);">{asin}</span>
  </nav>

  <!-- Main Product Detail View -->
  <main class="container" style="padding-bottom: 60px;">
    <div class="deal-detail-layout" style="display: grid; grid-template-columns: minmax(300px, 460px) 1fr; gap: 32px; background: var(--bg-card); border: 1px solid var(--border-card); border-radius: var(--radius-xl); padding: 32px; margin-bottom: 40px;">
      <!-- Left: Product Image & Badges -->
      <div class="product-media-col" style="text-align: center;">
        <div style="background: rgba(255, 255, 255, 0.03); border: 1px solid var(--border-subtle); border-radius: var(--radius-lg); padding: 24px; position: relative;">
          <div style="position: absolute; top: 12px; left: 12px; display: flex; flex-direction: column; gap: 6px; align-items: flex-start; z-index: 2;">
            {badges_html}
          </div>
          <img src="{image_url}" alt="{title}" style="max-width: 100%; max-height: 380px; object-fit: contain; border-radius: var(--radius-md);" onload="if(this.naturalWidth<=1||this.naturalHeight<=1){{this.src='data:image/svg+xml;utf8,<svg xmlns=\'http://www.w3.org/2000/svg\' width=\'200\' height=\'200\' viewBox=\'0 0 200 200\'><rect width=\'100%\' height=\'100%\' fill=\'%23f1f5f9\'/><text x=\'50%\' y=\'45%\' font-size=\'48\' text-anchor=\'middle\' dominant-baseline=\'middle\'>⚡</text><text x=\'50%\' y=\'72%\' font-size=\'13\' font-weight=\'bold\' fill=\'%2364748b\' text-anchor=\'middle\' font-family=\'sans-serif\'>Amazon Verified Deal</text></svg>';}}" onerror="this.src='data:image/svg+xml;utf8,<svg xmlns=\'http://www.w3.org/2000/svg\' width=\'200\' height=\'200\' viewBox=\'0 0 200 200\'><rect width=\'100%\' height=\'100%\' fill=\'%23f1f5f9\'/><text x=\'50%\' y=\'45%\' font-size=\'48\' text-anchor=\'middle\' dominant-baseline=\'middle\'>⚡</text><text x=\'50%\' y=\'72%\' font-size=\'13\' font-weight=\'bold\' fill=\'%2364748b\' text-anchor=\'middle\' font-family=\'sans-serif\'>Amazon Verified Deal</text></svg>';">
        </div>
        <div style="margin-top: 16px; font-size: 0.82rem; color: var(--text-muted);">
          Amazon ASIN: <code style="color: var(--color-cyan); font-family: var(--font-mono);">{asin}</code> · Brand: <b>{brand}</b>
        </div>
      </div>

      <!-- Right: Product Information & CTAs -->
      <div class="product-info-col">
        <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 8px;">
          <span class="brand-chip">{brand}</span>
          <span style="color: #fbbf24; font-weight: 700; font-size: 0.9rem;">⭐ {rating} <span style="color: var(--text-muted); font-size: 0.8rem;">({review_count:,} reviews)</span></span>
        </div>

        <h1 style="font-size: 1.6rem; font-weight: 800; line-height: 1.35; margin-bottom: 16px; color: var(--text-main);">
          {title}
        </h1>

        <!-- Price Waterfall Card -->
        <div class="price-waterfall-box" style="margin-bottom: 20px; padding: 20px; background: rgba(15, 23, 42, 0.7); border: 1px solid var(--border-card); border-radius: var(--radius-lg);">
          <div style="font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--text-muted); margin-bottom: 8px;">Verified Effective Deal Price</div>
          <div class="price-main-row" style="display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap;">
            <span class="effective-price" style="font-size: 2.2rem; font-weight: 800; color: #34d399; font-family: var(--font-mono);">₹{eff_price:,}</span>
            <span class="original-mrp" style="font-size: 1.2rem; text-decoration: line-through; color: var(--text-muted);">₹{mrp:,}</span>
            <span class="discount-badge" style="background: #ef4444; color: #fff; padding: 4px 10px; border-radius: 8px; font-weight: 800; font-size: 0.95rem;">{disc_pct}% OFF</span>
          </div>
          <div class="savings-line" style="margin-top: 8px; color: #38bdf8; font-weight: 700; font-size: 1rem;">
            🎉 You save ₹{savings_inr:,} on this purchase!
          </div>
        </div>

        {coupon_claim_html}

        <!-- Buy Confidence Meter -->
        <div class="confidence-meter-box" style="margin-bottom: 24px; padding: 16px; background: rgba(30, 41, 59, 0.5); border-radius: 12px; border: 1px solid var(--border-subtle);">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
            <span style="font-weight: 700; color: #34d399;">🟢 BuyHatke Deal Confidence</span>
            <span style="font-weight: 800; color: #34d399; font-family: var(--font-mono);">{buy_conf}% Rating</span>
          </div>
          <div style="height: 10px; background: rgba(255,255,255,0.1); border-radius: 999px; overflow: hidden;">
            <div style="height: 100%; width: {buy_conf}%; background: linear-gradient(90deg, #10b981, #06b6d4); border-radius: 999px;"></div>
          </div>
          <div style="margin-top: 8px; font-size: 0.8rem; color: var(--text-secondary);">
            Evaluated across price history, coupon availability, brand reliability & review ratings.
          </div>
        </div>

        <!-- Primary CTA Buttons -->
        <div style="display: flex; flex-direction: column; gap: 12px; margin-top: 24px;">
          <a href="{affiliate_url}" target="_blank" rel="noopener noreferrer" class="btn btn-primary btn-lg" style="display: flex; align-items: center; justify-content: center; gap: 10px; font-size: 1.2rem; font-weight: 800; padding: 18px 24px; text-decoration: none; text-align: center; border-radius: 12px; background: linear-gradient(135deg, #10b981, #059669); color: #fff; box-shadow: 0 4px 20px rgba(16, 185, 129, 0.4);">
            <span>🛒 Grab Diwali Deal on Amazon.in @ ₹{eff_price:,} ↗</span>
          </a>
          <div style="display: flex; gap: 12px; justify-content: space-between; flex-wrap: wrap;">
            <a href="https://api.whatsapp.com/send?text={html_lib.escape(f'🔥 Unbelievable Amazon Deal! {raw_title} at ₹{eff_price:,} ({disc_pct}% OFF): {canonical}')}" target="_blank" rel="noopener noreferrer" class="btn btn-outline" style="flex: 1; min-width: 160px; text-align: center; text-decoration: none; padding: 10px 16px;">
              <span>💬 Share WhatsApp</span>
            </a>
            <a href="/" class="btn btn-outline" style="flex: 1; min-width: 160px; text-align: center; text-decoration: none; padding: 10px 16px;">
              <span>🌐 All 50+ Live Deals</span>
            </a>
          </div>
        </div>
      </div>
    </div>

    <!-- Keepa Price History Chart Section -->
    <section style="background: var(--bg-card); border: 1px solid var(--border-card); border-radius: var(--radius-xl); padding: 32px; margin-bottom: 40px;">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; flex-wrap: wrap; gap: 12px;">
        <div>
          <h2 style="font-size: 1.35rem; font-weight: 800; color: var(--text-main); margin-bottom: 4px;">
            📈 90-Day Amazon Price History & Keepa Verification
          </h2>
          <p style="font-size: 0.88rem; color: var(--text-secondary);">
            Automated verification confirms whether this price drop is a genuine all-time low.
          </p>
        </div>
        <div style="display: flex; gap: 16px; font-family: var(--font-mono); font-size: 0.85rem;">
          <div style="background: rgba(16, 185, 129, 0.1); border: 1px solid rgba(16, 185, 129, 0.3); padding: 8px 14px; border-radius: 8px;">
            <span style="color: var(--text-muted); display: block; font-size: 0.72rem;">CURRENT</span>
            <span style="color: #34d399; font-weight: 700;">₹{eff_price:,}</span>
          </div>
          <div style="background: rgba(6, 182, 212, 0.1); border: 1px solid rgba(6, 182, 212, 0.3); padding: 8px 14px; border-radius: 8px;">
            <span style="color: var(--text-muted); display: block; font-size: 0.72rem;">TRACKED AVG</span>
            <span style="color: #38bdf8; font-weight: 700;">₹{hist_avg:,}</span>
          </div>
          <div style="background: rgba(245, 158, 11, 0.1); border: 1px solid rgba(245, 158, 11, 0.3); padding: 8px 14px; border-radius: 8px;">
            <span style="color: var(--text-muted); display: block; font-size: 0.72rem;">LOWEST RECORDED</span>
            <span style="color: #fbbf24; font-weight: 700;">₹{hist_min:,}</span>
          </div>
        </div>
      </div>

      <div style="background: #ffffff; border-radius: 12px; padding: 16px; text-align: center; overflow: hidden;">
        <img src="{keepa_chart_url}" alt="Keepa Price History Chart for {title}" style="max-width: 100%; height: auto; display: block; margin: 0 auto; border-radius: 8px;" loading="lazy">
      </div>
    </section>

    <!-- How to Claim Checklist -->
    <section style="background: var(--bg-card); border: 1px solid var(--border-card); border-radius: var(--radius-xl); padding: 32px; margin-bottom: 40px;">
      <h2 style="font-size: 1.35rem; font-weight: 800; color: var(--text-main); margin-bottom: 16px;">
        💡 How to Claim this Loot Deal & Avoid Price Spikes
      </h2>
      <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px;">
        <div style="background: rgba(15, 23, 42, 0.6); padding: 18px; border-radius: 12px; border: 1px solid var(--border-subtle);">
          <div style="font-size: 1.5rem; margin-bottom: 8px;">1️⃣</div>
          <div style="font-weight: 700; color: var(--text-main); margin-bottom: 4px;">Click Grab Deal</div>
          <div style="font-size: 0.85rem; color: var(--text-secondary);">Click the green Amazon button to open the product directly on Amazon.in.</div>
        </div>
        <div style="background: rgba(15, 23, 42, 0.6); padding: 18px; border-radius: 12px; border: 1px solid var(--border-subtle);">
          <div style="font-size: 1.5rem; margin-bottom: 8px;">2️⃣</div>
          <div style="font-weight: 700; color: var(--text-main); margin-bottom: 4px;">Tick Coupon Box</div>
          <div style="font-size: 0.85rem; color: var(--text-secondary);">If an on-page coupon is active, tick the checkbox before adding the item to cart.</div>
        </div>
        <div style="background: rgba(15, 23, 42, 0.6); padding: 18px; border-radius: 12px; border: 1px solid var(--border-subtle);">
          <div style="font-size: 1.5rem; margin-bottom: 8px;">3️⃣</div>
          <div style="font-weight: 700; color: var(--text-main); margin-bottom: 4px;">Apply Card Offers</div>
          <div style="font-size: 0.85rem; color: var(--text-secondary);">Choose an eligible bank debit/credit card during checkout for instant discount.</div>
        </div>
        <div style="background: rgba(15, 23, 42, 0.6); padding: 18px; border-radius: 12px; border: 1px solid var(--border-subtle);">
          <div style="font-size: 1.5rem; margin-bottom: 8px;">4️⃣</div>
          <div style="font-weight: 700; color: var(--text-main); margin-bottom: 4px;">Order Immediately</div>
          <div style="font-size: 0.85rem; color: var(--text-secondary);">Loot deals and price errors sell out quickly or revert to original price.</div>
        </div>
      </div>

      <div style="margin-top: 24px; text-align: center;">
        <a href="{affiliate_url}" target="_blank" rel="noopener noreferrer" class="btn btn-primary btn-lg" style="display: inline-block; font-size: 1.15rem; font-weight: 800; padding: 16px 36px; text-decoration: none; border-radius: 12px; background: linear-gradient(135deg, #10b981, #059669); color: #fff;">
          <span>🛒 Grab Deal on Amazon.in @ ₹{eff_price:,} ↗</span>
        </a>
      </div>
    </section>

    <!-- Related Deals Section -->
    <section>
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
        <h2 style="font-size: 1.35rem; font-weight: 800; color: var(--text-main);">
          🔥 Related Live Amazon Loot Deals
        </h2>
        <a href="/" style="color: var(--color-emerald-light); font-weight: 700; font-size: 0.9rem; text-decoration: none;">
          View All Live Deals →
        </a>
      </div>
      <div class="deals-grid" style="display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 20px;">
        {related_cards_html}
      </div>
    </section>
  </main>

  <!-- Footer -->
  <footer class="site-footer">
    <div class="container footer-inner">
      <div class="footer-top">
        <div class="footer-brand">
          <span class="brand-logo-small">⚡ Bharat<span class="brand-accent">Bazaro</span></span>
          <p class="footer-tagline">24/7 AI-Powered Deal Radar & Amazon Price Error Discovery Engine.</p>
        </div>
        <div class="footer-links">
          <a href="https://t.me/Flash_Deals_Flipkart_Amazon" target="_blank" rel="noopener noreferrer" class="footer-link">📢 Telegram Channel</a>
          <a href="https://chat.whatsapp.com/GspHsCGTRm29orUszbuvh3?s=cl&p=a&mlu=4&ilr=4&iam=2" target="_blank" rel="noopener noreferrer" class="footer-link">💬 WhatsApp Deals</a>
          <a href="/deals.html" class="footer-link">🔥 Deals</a>
          <a href="/electronics.html" class="footer-link">⚡ Electronics</a>
          <a href="/mobiles.html" class="footer-link">📱 Mobiles</a>
          <a href="/home.html" class="footer-link">🏠 Home & Kitchen</a>
          <a href="/fashion.html" class="footer-link">👗 Fashion</a>
          <a href="/grocery.html" class="footer-link">🛒 Grocery</a>
        </div>
        <div class="footer-links footer-stores" style="margin-top: 10px; display: flex; flex-wrap: wrap; gap: 12px;">
          <a href="/under-99.html" class="footer-link">⚡ ₹99 Store</a>
          <a href="/under-299.html" class="footer-link">🏷️ Under ₹299 Deals</a>
          <a href="/under-499.html" class="footer-link">🔥 Under ₹499 Loots</a>
          <a href="/under-999.html" class="footer-link">🎧 Under ₹999 Deals</a>
          <a href="/coupons.html" class="footer-link">🎟️ Hidden Coupons</a>
          <a href="/feed.xml" class="footer-link" target="_blank" rel="alternate" type="application/rss+xml">📡 RSS 2.0 Feed</a>
        </div>
      </div>

      <div class="footer-disclosure">
        <p><i>#ad Affiliate Disclosure: BharatBazaro.com is a participant in the Amazon Associates Programme, an affiliate advertising program designed to provide a means for sites to earn advertising fees by advertising and linking to Amazon.in. Prices, coupons, and product availability are subject to change by Amazon at any moment.</i></p>
      </div>

      <div class="footer-bottom">
        <p>&copy; 2026 BharatBazaro.com · Autonomous Deal Intelligence by DealDash AI · Powered by GitHub Actions 24/7</p>
      </div>
    </div>
  </footer>

  <!-- ⚡ BazarAI Floating Launcher & Chat Window -->
  <div id="bazaroAiWidget" class="bazaro-ai-widget">
    <!-- Welcome Tooltip Callout -->
    <div id="bazaroAiTooltip" class="bazaro-ai-tooltip hidden">
      <div class="tooltip-content">
        <span>👋 Looking for hidden coupons, price history, or the best deal under your budget? Ask me!</span>
        <button id="bazaroAiTooltipClose" class="tooltip-close" aria-label="Close tooltip">&times;</button>
      </div>
    </div>

    <!-- Floating Launcher Button -->
    <button id="bazaroAiLauncher" class="bazaro-ai-launcher" aria-label="Ask BazarAI Discounts Finder">
      <div class="launcher-avatar-wrap">
        <span class="launcher-icon">⚡</span>
        <span class="launcher-status-dot"></span>
      </div>
      <span class="launcher-label">⚡ Ask BazarAI • Discounts Finder</span>
      <span id="bazaroAiUnreadBadge" class="launcher-badge">1</span>
    </button>

    <!-- Chat Drawer -->
    <div id="bazaroAiDrawer" class="bazaro-ai-drawer hidden" role="dialog" aria-labelledby="bazaroAiTitle">
      <div class="drawer-header">
        <div class="drawer-header-left">
          <div class="drawer-avatar">⚡</div>
          <div class="drawer-titles">
            <h3 id="bazaroAiTitle" class="drawer-title">⚡ BazarAI — AI Discounts &amp; Coupon Finder</h3>
            <span class="drawer-subtitle">🟢 Online 24/7 • Finds Lowest Prices, Hidden Coupons &amp; Keepa Charts</span>
          </div>
        </div>
        <div class="drawer-controls">
          <button id="bazaroAiClearBtn" class="drawer-ctrl-btn" title="Clear Chat History">🗑️</button>
          <button id="bazaroAiExpandBtn" class="drawer-ctrl-btn desktop-only" title="Expand / Shrink Window">⛶</button>
          <button id="bazaroAiCloseBtn" class="drawer-ctrl-btn" title="Close Chat">&times;</button>
        </div>
      </div>

      <!-- Quick Action Chips Tray -->
      <div class="drawer-chips-tray" id="bazaroAiChips">
        <button class="ai-chip" data-prompt="🚨 Top Loot Deals">🚨 Top Loot Deals</button>
        <button class="ai-chip" data-prompt="🪙 Under ₹299 Store">🪙 Under ₹299 Store</button>
        <button class="ai-chip" data-prompt="🎟️ Hidden Coupons">🎟️ Hidden Coupons</button>
        <button class="ai-chip" data-prompt="🎧 Audio &amp; Tech Drops">🎧 Audio &amp; Tech Drops</button>
        <button class="ai-chip" data-prompt="🏠 Home &amp; Kitchen Loots">🏠 Home &amp; Kitchen Loots</button>
        <button class="ai-chip" data-prompt="📅 Amazon Sale Calendar">📅 Amazon Sale Calendar</button>
        <button class="ai-chip" data-prompt="📈 Check Amazon Link">📈 Check Amazon Link</button>
      </div>

      <!-- Message Stream Container -->
      <div class="drawer-messages" id="bazaroAiMessages"></div>

      <!-- Typing Indicator -->
      <div id="bazaroAiTyping" class="drawer-typing hidden">
        <span class="typing-dot"></span>
        <span class="typing-dot"></span>
        <span class="typing-dot"></span>
        <span class="typing-text">BazarAI is analyzing live deals...</span>
      </div>

      <!-- Input Bar -->
      <form id="bazaroAiForm" class="drawer-input-bar">
        <input type="text" id="bazaroAiInput" class="drawer-input" placeholder="Ask for any product, budget (e.g. earbuds under 999), sale info, or paste Amazon link..." autocomplete="off">
        <button type="submit" id="bazaroAiSendBtn" class="drawer-send-btn" aria-label="Send message">➤</button>
      </form>
    </div>
  </div>

  <script src="/bazaro_ai.js"></script>

</body>
</html>'''


def generate_rss_feed(deals: List[Dict[str, Any]], root_dir: Path) -> Path:
    """
    Generate a valid RSS 2.0 XML feed at /feed.xml with Yahoo Media tags for syndication
    across Pinterest, X/Twitter, Telegram, IFTTT, and Google Discover.
    """
    feed_path = root_dir / "feed.xml"
    now_rfc822 = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")

    items_xml = []
    for d in deals[:30]:
        asin = d.get("asin") or ""
        if not asin:
            continue
        title = d.get("title") or f"Amazon Deal ({asin})"
        eff_price = d.get("effective_price") or d.get("list_price") or 0
        mrp = d.get("mrp") or 0
        disc = d.get("effective_discount_pct") or d.get("discount_pct") or 0
        aff_url = d.get("affiliate_url") or f"https://www.amazon.in/dp/{asin}?tag=dammaiprasad-21&linkCode=ll1"
        img_url = d.get("image_url") or f"https://m.media-amazon.com/images/P/{asin}.jpg"
        deal_url = f"https://bharatbazaro.com/deal/{asin}.html"

        post_epoch = d.get("updated_at") or int(time.time())
        item_pub_date = datetime.fromtimestamp(post_epoch, tz=timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")

        coupon_note = f" (Includes {d['coupon_text']})" if d.get("coupon_text") else ""
        item_title = f"{title} — ₹{eff_price:,} ({disc}% OFF){coupon_note}"

        coupon_p = f"<p>🎟️ <strong>Coupon Trick:</strong> {html_lib.escape(str(d.get('coupon_text')))}</p>" if d.get('coupon_text') else ""
        description_content = (
            f"<p><strong>{html_lib.escape(title)}</strong></p>"
            f"<p>⚡ <strong>Deal Price:</strong> ₹{eff_price:,} (MRP: ₹{mrp:,}, Save {disc}% OFF)</p>"
            f"{coupon_p}"
            f"<p><a href='{aff_url}' target='_blank'>🛒 <strong>Grab Deal on Amazon.in</strong></a> | "
            f"<a href='{deal_url}'>📈 <strong>View Keepa Chart &amp; Price History on BharatBazaro</strong></a></p>"
        )

        item_xml = f'''    <item>
      <title>{html_lib.escape(item_title)}</title>
      <link>{deal_url}</link>
      <guid isPermaLink="true">{deal_url}</guid>
      <pubDate>{item_pub_date}</pubDate>
      <description><![CDATA[{description_content}]]></description>
      <media:content url="{img_url}" medium="image" type="image/jpeg" />
    </item>'''
        items_xml.append(item_xml)

    items_block = "\n".join(items_xml)
    rss_content = f'''<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>BharatBazaro — Live Amazon India Loot Deals, Price Drops &amp; Hidden Coupons</title>
    <link>https://bharatbazaro.com/</link>
    <description>Autonomous 24/7 radar tracking screaming price drops, 80% off loot errors, and stackable coupons on Amazon India.</description>
    <language>en-in</language>
    <lastBuildDate>{now_rfc822}</lastBuildDate>
    <atom:link href="https://bharatbazaro.com/feed.xml" rel="self" type="application/rss+xml" />
    <image>
      <url>https://bharatbazaro.com/icon-512.png</url>
      <title>BharatBazaro</title>
      <link>https://bharatbazaro.com/</link>
    </image>
{items_block}
  </channel>
</rss>
'''
    feed_path.write_text(rss_content, encoding="utf-8")
    logger.info(f"Generated RSS 2.0 media feed with {len(items_xml)} items at {feed_path}")
    return feed_path


def update_sitemap_xml(root_dir: Path, deal_asins: Optional[List[str]] = None):
    """Ensure sitemap.xml contains all core landing pages, price stores, and programmatic deal pages."""
    sitemap_path = root_dir / "sitemap.xml"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    urls = [
        # 1. Home
        ("https://bharatbazaro.com/", today, "hourly", "1.0"),
        # 2. Category Pages
        ("https://bharatbazaro.com/deals.html", today, "hourly", "0.9"),
        ("https://bharatbazaro.com/mobiles.html", today, "daily", "0.9"),
        ("https://bharatbazaro.com/electronics.html", today, "daily", "0.9"),
        ("https://bharatbazaro.com/home.html", today, "daily", "0.9"),
        ("https://bharatbazaro.com/fashion.html", today, "daily", "0.9"),
        ("https://bharatbazaro.com/grocery.html", today, "daily", "0.9"),
        ("https://bharatbazaro.com/beauty.html", today, "daily", "0.9"),
        # 3. High-Intent Price Stores & Coupons
        ("https://bharatbazaro.com/under-99.html", today, "daily", "0.9"),
        ("https://bharatbazaro.com/under-299.html", today, "daily", "0.9"),
        ("https://bharatbazaro.com/under-499.html", today, "daily", "0.9"),
        ("https://bharatbazaro.com/under-999.html", today, "daily", "0.9"),
        ("https://bharatbazaro.com/coupons.html", today, "hourly", "0.9"),
        # Evergreen SEO Buying Guides
        ("https://bharatbazaro.com/guides/best-earbuds-headphones-deals-india.html", today, "weekly", "0.8"),
        ("https://bharatbazaro.com/guides/best-mobile-smartphone-price-drops-india.html", today, "weekly", "0.8"),
        ("https://bharatbazaro.com/guides/best-home-kitchen-appliances-loot-deals.html", today, "weekly", "0.8"),
        ("https://bharatbazaro.com/guides/amazon-india-hidden-coupon-price-error-tricks.html", today, "weekly", "0.8"),
        ("https://bharatbazaro.com/guides/best-amazon-deals-under-99-299-499-india.html", today, "weekly", "0.8"),
    ]

    # 4. Programmatic Deal Pages
    if deal_asins:
        for asin in deal_asins:
            if asin:
                urls.append((f"https://bharatbazaro.com/deal/{asin}.html", today, "daily", "0.8"))

    url_blocks = []
    for loc, lastmod, freq, prio in urls:
        url_blocks.append(f'''  <url>
    <loc>{loc}</loc>
    <lastmod>{lastmod}</lastmod>
    <changefreq>{freq}</changefreq>
    <priority>{prio}</priority>
  </url>''')

    xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{"".join(url_blocks)}
</urlset>
'''
    sitemap_path.write_text(xml, encoding="utf-8")
    logger.info(f"Updated {sitemap_path} with {len(urls)} URLs.")


def pre_render_all_pages(deals: List[Dict[str, Any]], stats: Dict[str, Any], root_dir: Optional[Path] = None):
    """
    Pre-render static deal cards and rich schema across:
    1. index.html, 6 category pages, 5 price store pages
    2. Programmatic individual product pages in /deal/{asin}.html
    3. feed.xml (RSS 2.0 Media Feed)
    4. Comprehensive sitemap.xml
    """
    if root_dir is None:
        root_dir = Path(__file__).resolve().parent.parent

    base_index_path = root_dir / "index.html"
    if not base_index_path.exists():
        logger.warning(f"Base index.html not found at {base_index_path}")
        return

    base_html = base_index_path.read_text(encoding="utf-8")

    # 1. Pre-render 12 core landing pages (Home + Categories + Price Stores)
    for page_cfg in PAGE_CONFIGS:
        try:
            page_deals = filter_deals_for_page(page_cfg, deals)
            page_html = generate_page_html(base_html, page_cfg, page_deals, deals, stats)
            dest_file = root_dir / page_cfg["filename"]
            dest_file.write_text(page_html, encoding="utf-8")
            logger.info(f"Pre-rendered {len(page_deals)} deals into {dest_file} ({page_cfg['title'][:35]}...)")
        except Exception as e:
            logger.error(f"Failed to pre-render {page_cfg['filename']}: {e}", exc_info=True)

    # 2. Pre-render individual product pages (/deal/{asin}.html)
    deal_dir = root_dir / "deal"
    deal_dir.mkdir(parents=True, exist_ok=True)
    generated_asins = []

    for idx, d in enumerate(deals[:60]):
        asin = d.get("asin")
        if not asin:
            continue
        try:
            # Related deals from the same category or other top deals
            related = [x for x in deals if x.get("asin") != asin]
            prod_html = render_product_page_html(d, related, stats)
            prod_dest = deal_dir / f"{asin}.html"
            prod_dest.write_text(prod_html, encoding="utf-8")
            generated_asins.append(asin)
        except Exception as e:
            logger.error(f"Failed to pre-render product page for {asin}: {e}", exc_info=True)

    logger.info(f"Generated {len(generated_asins)} programmatic product pages in {deal_dir}")

    # 2b. Generate 5 Evergreen SEO Buying Guides
    try:
        from dealbot.guides_engine import generate_all_guides
        generate_all_guides(deals, root_dir)
    except Exception as e:
        logger.error(f"Failed to generate buying guides: {e}", exc_info=True)

    # 3. Generate RSS 2.0 Feed (feed.xml)
    try:
        generate_rss_feed(deals, root_dir)
    except Exception as e:
        logger.error(f"Failed to generate feed.xml: {e}", exc_info=True)

    # 4. Expand sitemap.xml
    try:
        update_sitemap_xml(root_dir, generated_asins)
    except Exception as e:
        logger.error(f"Failed to update sitemap.xml: {e}", exc_info=True)

    # 5. Run SEO Promotion (IndexNow, llms.txt, robots.txt, promo pack)
    try:
        from scripts.seo_promoter import run_seo_promotion
        run_seo_promotion(root_dir, deals)
    except Exception as e:
        logger.error(f"Failed to run SEO promotion: {e}", exc_info=True)
