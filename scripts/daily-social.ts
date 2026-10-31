import { appendFileSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

import {
  buildPlan,
  FEED_URL,
  istParts,
  pickDeal,
  recentAsins,
  slotForHour,
  type Deal,
  type History,
  type SlotId,
} from "../src/lib/social-engine.ts";
import { configuredChannels, deliverDeal, secretsFromEnv, type Secrets } from "../src/lib/social-post.ts";

const HISTORY_PATH = resolve(process.cwd(), "out/posted.json");

function readHistory(path: string): History {
  try {
    const parsed = JSON.parse(readFileSync(path, "utf8")) as History;
    return { posts: Array.isArray(parsed.posts) ? parsed.posts : [] };
  } catch {
    return { posts: [] };
  }
}

function slotsToRun(raw: string | undefined, now: Date): SlotId[] {
  if (raw === "all") return ["beauty", "fashion", "kitchen"];
  if (raw === "beauty" || raw === "fashion" || raw === "kitchen") return [raw];
  return [slotForHour(istParts(now).hour)];
}

export async function runDaily(options: {
  deals: Deal[];
  now?: Date;
  slot?: string;
  secrets?: Secrets;
  dryRun?: boolean;
  history?: History;
  fetchImpl?: typeof fetch;
  log?: (line: string) => void;
}): Promise<{ history: History; lines: string[]; failed: boolean }> {
  const now = options.now ?? new Date();
  const log = options.log ?? ((line: string) => console.log(line));
  const secrets = options.secrets ?? {};
  const history = options.history ?? { posts: [] };
  const { date } = istParts(now);
  const channels = configuredChannels(secrets);
  const lines: string[] = [];
  let failed = false;
  const plan = buildPlan(options.deals, now, history);
  lines.push(`Date ${plan.date} IST. Skipped ${plan.skippedMobiles} mobile rows. ${plan.considered} payable deals.`);

  for (const slot of slotsToRun(options.slot, now)) {
    const key = `${date}|${slot}`;
    if (history.posts.some((post) => post.key === key)) {
      lines.push(`${slot}: already posted today, skipped`);
      continue;
    }
    const deal = pickDeal(options.deals, slot, now, recentAsins(history, now));
    if (!deal) {
      lines.push(`${slot}: no non-mobile deal in this category`);
      failed = true;
      continue;
    }
    const preview = plan.slots.find((item) => item.id === slot);
    for (const post of preview?.posts ?? []) {
      lines.push(`--- ${slot} ${post.channel} ---\n${post.text}`);
    }
    if (options.dryRun || channels.length === 0) {
      lines.push(`${slot}: dry run for ${deal.asin}. Nothing sent.`);
      continue;
    }
    const results = await deliverDeal(deal, secrets, options.fetchImpl);
    const hardFailures = results.filter((result) => !result.ok && !result.skipped);
    for (const result of results) {
      if (result.channel === "whatsapp") {
        lines.push("whatsapp: copy only (Status has no official post API)");
        continue;
      }
      lines.push(`${result.channel}: ${result.skipped ? "skipped" : result.ok ? "ok" : "failed"} — ${result.detail}`);
    }
    if (hardFailures.length > 0) {
      failed = true;
      continue;
    }
    if (results.some((result) => result.ok && !result.skipped)) {
      history.posts.push({ key, asin: deal.asin ?? "", at: now.toISOString(), slot });
    }
  }

  for (const line of lines) log(line);
  return { history, lines, failed };
}

async function main(): Promise<void> {
  const now = new Date();
  const dryRun = process.env.DRY_RUN === "1" || process.env.DRY_RUN === "true";
  const response = await fetch(FEED_URL, { signal: AbortSignal.timeout(20000) });
  if (!response.ok) throw new Error(`Feed returned ${response.status}`);
  const deals = (await response.json()) as Deal[];
  if (!Array.isArray(deals)) throw new Error("Feed was not a list");
  const history = readHistory(HISTORY_PATH);
  const secrets = secretsFromEnv();
  const channels = configuredChannels(secrets);
  if (!dryRun && channels.length === 0 && process.env.GITHUB_ACTIONS === "true") {
    console.error(
      "No channel secrets configured. Add TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET, IG_ACCESS_TOKEN, IG_USER_ID under GitHub Actions secrets. Do not commit them.",
    );
    process.exit(1);
  }
  const result = await runDaily({
    deals,
    now,
    slot: process.env.SLOT,
    secrets,
    dryRun: dryRun || channels.length === 0,
    history,
  });
  mkdirSync(dirname(HISTORY_PATH), { recursive: true });
  writeFileSync(HISTORY_PATH, JSON.stringify(result.history, null, 2));
  if (process.env.GITHUB_STEP_SUMMARY) {
    appendFileSync(
      process.env.GITHUB_STEP_SUMMARY,
      ["## BharatBazaro daily posts", "", ...result.lines.map((line) => line.startsWith("---") ? `\n\`\`\`\n${line}\n\`\`\`\n` : line)].join("\n"),
    );
  }
  if (result.failed && !dryRun && channels.length > 0) process.exit(1);
}

const entry = process.argv[1] ?? "";
if (entry.endsWith("daily-social.ts")) {
  main().catch((error: unknown) => {
    console.error(error instanceof Error ? error.message : error);
    process.exit(1);
  });
}
