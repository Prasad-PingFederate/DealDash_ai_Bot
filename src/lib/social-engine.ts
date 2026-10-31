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

export function followFooter(links?: { telegram?: string; whatsapp?: string; x?: string; instagram?: string; reddit?: string; facebook?: string }): string {
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

export function captionFor(deal: Deal, channel: ChannelId, links?: { telegram?: string; whatsapp?: string; x?: string; instagram?: string; reddit?: string; facebook?: string }): string {
  const params = new URLSearchParams({ tag: AFFILIATE_TAG, linkCode: "ll1", ascsubtag: channel === "telegram" ? "bb-tg" : channel === "x" ? "bb-x" : channel === "instagram" ? "bb-ig" : "bb-wa" });
  const link = `https://www.amazon.in/dp/${deal.asin ?? ""}?${params.toString()}`;
  const price = `\u20b9${Math.round(deal.effective_price ?? 0).toLocaleString("en-IN")}`;
  const mrp = `\u20b9${Math.round(deal.mrp ?? 0).toLocaleString("en-IN")}`;
  const off = Math.round(deal.effective_discount_pct ?? deal.discount_pct ?? 0);
  const name = (deal.title ?? "Deal").slice(0, 78);
  const footer = followFooter(links);
  const body = `#ad ${name} is ${price} (MRP ${mrp}, ${off}% off).\nI earn a commission if you buy. No extra cost to you.\n${link}${footer}`;
  if (channel !== "x" || body.length <= 280) return body;
  return `#ad ${name} ${price} (${off}% off). Affiliate.\n${link}`;
}
