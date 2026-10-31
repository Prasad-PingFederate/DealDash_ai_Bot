export const AFFILIATE_TAG = "dammaiprasad-21";
export const FEED_URL = "https://bharatbazaro.com/data/live_deals.json";

export type SlotId = "beauty" | "fashion" | "kitchen";
export type ChannelId = "telegram" | "x" | "instagram" | "whatsapp";

export type Deal = {
  asin?: string;
  title?: string;
  brand?: string;
  category?: string;
  image_url?: string;
  mrp?: number;
  effective_price?: number;
  discount_pct?: number;
  effective_discount_pct?: number;
  coupon_text?: string | null;
  buy_confidence?: number | null;
  is_loot?: boolean;
  expired?: boolean;
  price_spiked?: boolean;
};

export type History = {
  posts: Array<{ key: string; asin: string; at: string; slot: SlotId }>;
};

const ACCESSORY =
  /\b(earbuds?|earphones|headphones|neckband|\bcase\b|\bcover\b|cable|charger|adapter|power\s?bank|screen\s?guard|tempered|macbook|laptop|tablet|smart\s?watch|\bwatch\b)\b/i;

const HANDSET =
  /\b(iphone|smart\s?phone|pixel\s?\d|nothing\s+phone|iqoo|\bpoco\b|infinix|tecno|redmi|realme\s+(?:narzo|note|gt|\d)|oneplus\s+(?:nord\s+ce\d*|nord\s+\d+|\d{1,2}r?)|motorola\s+g\d+|moto\s+g\d+|galaxy\s+[a-z]?\d+)\b/i;

const STAPLE =
  /\b(atta|edible oil|almond|cashew|muesli|detergent|garbage bag|disinfectant|fabric conditioner|toothpaste|sanitary|shampoo)\b/i;

const SUBTAG: Record<ChannelId, string> = {
  telegram: "bb-tg",
  x: "bb-x",
  instagram: "bb-ig",
  whatsapp: "bb-wa",
};

export const SLOT_META: Record<
  SlotId,
  { when: string; cronUtc: string; blurb: string }
> = {
  beauty: {
    when: "12:30 IST",
    cronUtc: "0 7 * * *",
    blurb: "Personal care. Highest Amazon India rate.",
  },
  fashion: {
    when: "7:00pm IST",
    cronUtc: "30 13 * * *",
    blurb: "Shoes, apparel, bags, analog watches.",
  },
  kitchen: {
    when: "9:30pm IST",
    cronUtc: "0 16 * * *",
    blurb: "Home and kitchen appliances.",
  },
};

export function isMobileHandset(deal: Pick<Deal, "title" | "category">): boolean {
  const title = deal.title ?? "";
  if (ACCESSORY.test(title)) return false;
  if (HANDSET.test(title)) return true;
  const category = deal.category ?? "";
  return /^Mobiles/i.test(category) && /\b5g\b/i.test(title) && /\b\d+\s?gb\b/i.test(title);
}

export function shouldCapture(deal: Deal): boolean {
  if (isMobileHandset(deal)) return false;
  if (/^Mobiles/i.test(deal.category ?? "")) return false;
  return true;
}

export function istParts(now: Date): { date: string; hour: number; dayOfYear: number } {
  const date = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata", year: "numeric", month: "2-digit", day: "2-digit" }).format(now);
  const hour = Number(new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Kolkata", hour: "numeric", hourCycle: "h23" }).format(now));
  const [year, month, day] = date.split("-").map(Number);
  const utc = Date.UTC(year, month - 1, day);
  const start = Date.UTC(year, 0, 1);
  return { date, hour, dayOfYear: Math.floor((utc - start) / 86400000) + 1 };
}

export type FollowLinks = {
  telegram?: string;
  whatsapp?: string;
  x?: string;
  instagram?: string;
  reddit?: string;
  facebook?: string;
};

export function followFooter(links?: FollowLinks): string {
  if (!links) return "";
  const lines = [
    links.telegram ? `Telegram ${links.telegram}` : "",
    links.whatsapp ? `WhatsApp ${links.whatsapp}` : "",
    links.x ? `X ${links.x}` : "",
    links.instagram ? `Instagram ${links.instagram}` : "",
    links.reddit ? `Reddit ${links.reddit}` : "",
    links.facebook ? `Facebook ${links.facebook}` : "",
  ].filter((line) => line.length > 0);
  return lines.length ? `\n${lines.join("\n")}` : "";
}

export function slotForHour(hour: number): SlotId {
  if (hour >= 16 && hour < 21) return "fashion";
  if (hour >= 21 || hour < 8) return "kitchen";
  return "beauty";
}

function inr(value: number): string {
  return `\u20b9${Math.round(value).toLocaleString("en-IN")}`;
}

export function shortName(title: string): string {
  const cleaned = title.replace(/^(\[.*?\]\s*|LOOT ALERT:\s*)/i, "").trim();
  const cut = cleaned.split("|")[0]?.trim() ?? cleaned;
  if (cut.length <= 78) return cut;
  return `${cut.slice(0, 75).replace(/\s+\S*$/, "")}\u2026`;
}

export function affiliateLink(asin: string, channel: ChannelId): string {
  const params = new URLSearchParams({ tag: AFFILIATE_TAG, linkCode: "ll1", ascsubtag: SUBTAG[channel] });
  return `https://www.amazon.in/dp/${asin}?${params.toString()}`;
}

export function captionFor(deal: Deal, channel: ChannelId, links?: FollowLinks): string {
  const link = affiliateLink(deal.asin ?? "", channel);
  const price = inr(deal.effective_price ?? 0);
  const mrp = inr(deal.mrp ?? 0);
  const off = Math.round(deal.effective_discount_pct ?? deal.discount_pct ?? 0);
  const name = shortName(deal.title ?? "Deal");
  const coupon = deal.coupon_text ? ` ${deal.coupon_text} on the Amazon page.` : "";
  const footer = followFooter(links);
  if (channel === "x") {
    const full = `#ad ${name} is ${price} (MRP ${mrp}, ${off}% off).${coupon} Affiliate, no extra cost.\n${link}${footer}`;
    if (full.length <= 280) return full;
    const short = `#ad ${name} ${price} (${off}% off). Affiliate.\n${link}`;
    return short.length + footer.length <= 280 ? `${short}${footer}` : short;
  }
  return `#ad ${name} is ${price} (MRP ${mrp}, ${off}% off).${coupon}\nI earn a commission if you buy. No extra cost to you.\n${link}${footer}`;
}

function eligible(deal: Deal): boolean {
  if (!deal.asin || !deal.title) return false;
  if (!shouldCapture(deal)) return false;
  if (deal.expired || deal.price_spiked) return false;
  if (/^\[HDFC/i.test(deal.title)) return false;
  if ((deal.effective_price ?? 0) <= 0) return false;
  return true;
}

function matchesSlot(deal: Deal, slot: SlotId): boolean {
  const category = deal.category ?? "";
  const title = deal.title ?? "";
  if (slot === "beauty") return category === "Personal Care & Grocery" && !STAPLE.test(title);
  if (slot === "fashion") return category === "Fashion & Footwear";
  return category === "Home & Kitchen" && !STAPLE.test(title) && !/\b(shampoo|body wash|lotion|face wash)\b/i.test(title);
}

function score(deal: Deal, slot: SlotId): number {
  const discount = deal.effective_discount_pct ?? deal.discount_pct ?? 0;
  let value = (deal.buy_confidence ?? 0) + discount * 0.25 + (deal.is_loot ? 6 : 0);
  const title = deal.title ?? "";
  if (slot === "fashion" && /\bsmart\s?watch\b/i.test(title)) value -= 18;
  if (slot === "kitchen" && /\b(kettle|induction|mixer|cooktop|scale|fan|underblanket|fryer)\b/i.test(title)) value += 8;
  if (slot === "beauty" && /\b(cetaphil|nivea|dove|plum|lakme|vaseline|serum|face wash|lotion)\b/i.test(title)) value += 8;
  return value;
}

export function pickDeal(deals: Deal[], slot: SlotId, now: Date, exclude: Set<string> = new Set()): Deal | null {
  const pool = deals.filter((deal) => eligible(deal) && matchesSlot(deal, slot));
  const fresh = pool.filter((deal) => !exclude.has(deal.asin ?? ""));
  const list = (fresh.length > 0 ? fresh : pool).slice();
  if (list.length === 0) return null;
  list.sort((a, b) => score(b, slot) - score(a, slot) || (a.asin ?? "").localeCompare(b.asin ?? ""));
  const top = list.slice(0, 8);
  const { dayOfYear } = istParts(now);
  return top[dayOfYear % top.length] ?? null;
}

export type PlannedPost = { channel: ChannelId; delivery: "auto" | "copy"; text: string };
export type PlannedSlot = {
  id: SlotId;
  when: string;
  blurb: string;
  deal: { asin: string; title: string; price: number; mrp: number; discount: number; image: string; category: string; coupon: string } | null;
  posts: PlannedPost[];
  alreadyPosted: boolean;
};
export type DailyPlan = { date: string; timezone: "Asia/Kolkata"; skippedMobiles: number; considered: number; slots: PlannedSlot[] };

const CHANNELS: ChannelId[] = ["telegram", "x", "instagram", "whatsapp"];

export function recentAsins(history: History | undefined, now: Date): Set<string> {
  const cutoff = now.getTime() - 14 * 86400000;
  const set = new Set<string>();
  for (const post of history?.posts ?? []) {
    const at = Date.parse(post.at);
    if (!Number.isNaN(at) && at >= cutoff) set.add(post.asin);
  }
  return set;
}

export function buildPlan(deals: Deal[], now: Date, history?: History): DailyPlan {
  const { date } = istParts(now);
  const exclude = recentAsins(history, now);
  const postedKeys = new Set((history?.posts ?? []).map((post) => post.key));
  const slots = (Object.keys(SLOT_META) as SlotId[]).map((id) => {
    const meta = SLOT_META[id];
    const deal = pickDeal(deals, id, now, exclude);
    const posts: PlannedPost[] = deal
      ? CHANNELS.map((channel) => ({ channel, delivery: channel === "whatsapp" ? "copy" : "auto", text: captionFor(deal, channel) }))
      : [];
    return {
      id,
      when: meta.when,
      blurb: meta.blurb,
      alreadyPosted: postedKeys.has(`${date}|${id}`),
      deal: deal
        ? {
            asin: deal.asin ?? "",
            title: shortName(deal.title ?? ""),
            price: deal.effective_price ?? 0,
            mrp: deal.mrp ?? 0,
            discount: Math.round(deal.effective_discount_pct ?? deal.discount_pct ?? 0),
            image: deal.image_url ?? "",
            category: deal.category ?? "",
            coupon: deal.coupon_text ?? "",
          }
        : null,
      posts,
    };
  });
  return {
    date,
    timezone: "Asia/Kolkata",
    skippedMobiles: deals.filter((deal) => !shouldCapture(deal)).length,
    considered: deals.filter((deal) => eligible(deal)).length,
    slots,
  };
}
