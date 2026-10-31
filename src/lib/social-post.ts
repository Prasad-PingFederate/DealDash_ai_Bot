import crypto from "node:crypto";

import {
  affiliateLink,
  captionFor,
  type ChannelId,
  type Deal,
  type FollowLinks,
  type SlotId,
} from "./social-engine.ts";

export type Secrets = {
  telegramToken?: string;
  telegramChatId?: string;
  xApiKey?: string;
  xApiSecret?: string;
  xAccessToken?: string;
  xAccessSecret?: string;
  igAccessToken?: string;
  igUserId?: string;
  links?: FollowLinks;
};

export type DeliveryResult = {
  channel: ChannelId;
  ok: boolean;
  skipped?: boolean;
  detail: string;
};

function percent(value: string): string {
  return encodeURIComponent(value).replace(/[!'()*]/g, (char) => `%${char.charCodeAt(0).toString(16).toUpperCase()}`);
}

export function xAuthHeader(input: {
  url: string;
  consumerKey: string;
  consumerSecret: string;
  token: string;
  tokenSecret: string;
  nonce?: string;
  timestamp?: string;
}): string {
  const oauth: Record<string, string> = {
    oauth_consumer_key: input.consumerKey,
    oauth_nonce: input.nonce ?? crypto.randomBytes(16).toString("hex"),
    oauth_signature_method: "HMAC-SHA1",
    oauth_timestamp: input.timestamp ?? String(Math.floor(Date.now() / 1000)),
    oauth_token: input.token,
    oauth_version: "1.0",
  };
  const paramString = Object.keys(oauth)
    .sort()
    .map((key) => `${percent(key)}=${percent(oauth[key] ?? "")}`)
    .join("&");
  const base = `POST&${percent(input.url)}&${percent(paramString)}`;
  const key = `${percent(input.consumerSecret)}&${percent(input.tokenSecret)}`;
  oauth.oauth_signature = crypto.createHmac("sha1", key).update(base).digest("base64");
  return (
    "OAuth " +
    Object.keys(oauth)
      .sort()
      .map((name) => `${percent(name)}="${percent(oauth[name] ?? "")}"`)
      .join(", ")
  );
}

async function readError(response: Response): Promise<string> {
  const text = await response.text();
  return text.slice(0, 280) || `HTTP ${response.status}`;
}

export async function postTelegram(
  deal: Deal,
  secrets: Secrets,
  fetchImpl: typeof fetch,
): Promise<DeliveryResult> {
  if (!secrets.telegramToken || !secrets.telegramChatId) {
    return { channel: "telegram", ok: false, skipped: true, detail: "Telegram secrets not set" };
  }
  const text = captionFor(deal, "telegram", secrets.links);
  const endpoint = `https://api.telegram.org/bot${secrets.telegramToken}`;
  if (deal.image_url) {
    const photo = await fetchImpl(`${endpoint}/sendPhoto`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        chat_id: secrets.telegramChatId,
        photo: deal.image_url,
        caption: text.slice(0, 1024),
      }),
    });
    if (photo.ok) return { channel: "telegram", ok: true, detail: "Photo posted" };
  }
  const message = await fetchImpl(`${endpoint}/sendMessage`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      chat_id: secrets.telegramChatId,
      text,
      disable_web_page_preview: false,
    }),
  });
  if (!message.ok) {
    return { channel: "telegram", ok: false, detail: await readError(message) };
  }
  return { channel: "telegram", ok: true, detail: "Message posted" };
}

export async function postX(
  deal: Deal,
  secrets: Secrets,
  fetchImpl: typeof fetch,
): Promise<DeliveryResult> {
  if (!secrets.xApiKey || !secrets.xApiSecret || !secrets.xAccessToken || !secrets.xAccessSecret) {
    return { channel: "x", ok: false, skipped: true, detail: "X secrets not set" };
  }
  const url = "https://api.x.com/2/tweets";
  const response = await fetchImpl(url, {
    method: "POST",
    headers: {
      authorization: xAuthHeader({
        url,
        consumerKey: secrets.xApiKey,
        consumerSecret: secrets.xApiSecret,
        token: secrets.xAccessToken,
        tokenSecret: secrets.xAccessSecret,
      }),
      "content-type": "application/json",
    },
    body: JSON.stringify({ text: captionFor(deal, "x", secrets.links) }),
  });
  if (!response.ok) return { channel: "x", ok: false, detail: await readError(response) };
  return { channel: "x", ok: true, detail: "Posted" };
}

export async function postInstagram(
  deal: Deal,
  secrets: Secrets,
  fetchImpl: typeof fetch,
): Promise<DeliveryResult> {
  if (!secrets.igAccessToken || !secrets.igUserId) {
    return { channel: "instagram", ok: false, skipped: true, detail: "Instagram secrets not set" };
  }
  if (!deal.image_url) {
    return { channel: "instagram", ok: false, detail: "No public image on this deal" };
  }
  const base = `https://graph.facebook.com/v21.0/${secrets.igUserId}`;
  const created = await fetchImpl(`${base}/media`, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      image_url: deal.image_url,
      caption: captionFor(deal, "instagram", secrets.links),
      access_token: secrets.igAccessToken,
    }),
  });
  if (!created.ok) return { channel: "instagram", ok: false, detail: await readError(created) };
  const body = (await created.json()) as { id?: string };
  if (!body.id) return { channel: "instagram", ok: false, detail: "Instagram did not return a media id" };
  const published = await fetchImpl(`${base}/media_publish`, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      creation_id: body.id,
      access_token: secrets.igAccessToken,
    }),
  });
  if (!published.ok) return { channel: "instagram", ok: false, detail: await readError(published) };
  return { channel: "instagram", ok: true, detail: "Published" };
}

export function whatsAppCopy(deal: Deal, secrets: Secrets = {}): DeliveryResult {
  return {
    channel: "whatsapp",
    ok: true,
    skipped: true,
    detail: captionFor(deal, "whatsapp", secrets.links),
  };
}

export async function deliverDeal(
  deal: Deal,
  secrets: Secrets,
  fetchImpl: typeof fetch = fetch,
): Promise<DeliveryResult[]> {
  const [telegram, x, instagram] = await Promise.all([
    postTelegram(deal, secrets, fetchImpl),
    postX(deal, secrets, fetchImpl),
    postInstagram(deal, secrets, fetchImpl),
  ]);
  return [telegram, x, instagram, whatsAppCopy(deal, secrets)];
}

export function secretsFromEnv(env: NodeJS.ProcessEnv = process.env): Secrets {
  const read = (name: string) => {
    const value = env[name]?.trim();
    return value ? value : undefined;
  };
  return {
    telegramToken: read("TELEGRAM_BOT_TOKEN"),
    telegramChatId: read("TELEGRAM_CHAT_ID"),
    xApiKey: read("X_API_KEY"),
    xApiSecret: read("X_API_SECRET"),
    xAccessToken: read("X_ACCESS_TOKEN"),
    xAccessSecret: read("X_ACCESS_SECRET"),
    igAccessToken: read("IG_ACCESS_TOKEN"),
    igUserId: read("IG_USER_ID"),
    links: {
      telegram: read("TELEGRAM_CHANNEL_LINK"),
      whatsapp: read("WHATSAPP_CHANNEL_LINK"),
      x: read("X_ACCOUNT_LINK"),
      instagram: read("INSTAGRAM_ACCOUNT_LINK"),
      reddit: read("REDDIT_COMMUNITY_LINK"),
      facebook: read("FACEBOOK_PAGE_LINK"),
    },
  };
}

export function configuredChannels(secrets: Secrets): ChannelId[] {
  const channels: ChannelId[] = [];
  if (secrets.telegramToken && secrets.telegramChatId) channels.push("telegram");
  if (secrets.xApiKey && secrets.xApiSecret && secrets.xAccessToken && secrets.xAccessSecret) channels.push("x");
  if (secrets.igAccessToken && secrets.igUserId) channels.push("instagram");
  return channels;
}

export function linkFor(asin: string, channel: ChannelId): string {
  return affiliateLink(asin, channel);
}

export type { SlotId };
