import assert from "node:assert/strict";
import test from "node:test";

import {
  buildPlan,
  captionFor,
  isMobileHandset,
  pickDeal,
  shouldCapture,
  type Deal,
} from "./social-engine.ts";

const phone: Deal = {
  asin: "PHONE1",
  title: "Apple iPhone 15 (128 GB) - Black",
  category: "Mobiles & Accessories",
  effective_price: 57999,
  mrp: 79900,
  effective_discount_pct: 27,
  buy_confidence: 95,
};

const buds: Deal = {
  asin: "BUDS1",
  title: "OnePlus Nord Buds 2 TWS in Ear Earbuds",
  category: "Mobiles & Accessories",
  effective_price: 2199,
  mrp: 3299,
  effective_discount_pct: 33,
  buy_confidence: 90,
};

const dove: Deal = {
  asin: "DOVE1",
  title: "Dove Refreshing Body Wash 1 Litre",
  category: "Personal Care & Grocery",
  effective_price: 389,
  mrp: 799,
  effective_discount_pct: 51,
  buy_confidence: 95,
  image_url: "https://example.com/dove.jpg",
};

const atta: Deal = {
  asin: "ATTA1",
  title: "Aashirvaad Superior MP Sharbati Whole Wheat Atta 5kg",
  category: "Personal Care & Grocery",
  effective_price: 289,
  mrp: 345,
  effective_discount_pct: 16,
  buy_confidence: 80,
};

const puma: Deal = {
  asin: "PUMA1",
  title: "Puma Unisex Smashic Casual Sneakers",
  category: "Fashion & Footwear",
  effective_price: 1899,
  mrp: 4499,
  effective_discount_pct: 58,
  buy_confidence: 95,
};

const kettle: Deal = {
  asin: "KETTLE1",
  title: "Pigeon 1.5 Litre Stainless Steel Electric Kettle",
  category: "Home & Kitchen",
  effective_price: 599,
  mrp: 1245,
  effective_discount_pct: 52,
  buy_confidence: 91,
};

test("phones are handsets and earbuds are not", () => {
  assert.equal(isMobileHandset(phone), true);
  assert.equal(isMobileHandset({ title: "Samsung Galaxy S23 FE 5G (8GB, 128GB Storage)" }), true);
  assert.equal(isMobileHandset(buds), false);
  assert.equal(isMobileHandset({ title: "Apple MacBook Air Laptop 8GB RAM" }), false);
});

test("mobiles category is never captured", () => {
  assert.equal(shouldCapture(phone), false);
  assert.equal(shouldCapture(buds), false);
  assert.equal(shouldCapture(dove), true);
});

test("daily plan has no mobile affiliate links", () => {
  const plan = buildPlan([phone, buds, dove, atta, puma, kettle], new Date("2026-11-01T07:00:00Z"));
  const blob = JSON.stringify(plan);
  assert.equal(blob.includes("PHONE1"), false);
  assert.equal(blob.includes("BUDS1"), false);
  assert.equal(blob.includes("dammaiprasad-21"), true);
  assert.equal(plan.slots.find((slot) => slot.id === "beauty")?.deal?.asin, "DOVE1");
  assert.equal(plan.slots.find((slot) => slot.id === "fashion")?.deal?.asin, "PUMA1");
  assert.equal(plan.slots.find((slot) => slot.id === "kitchen")?.deal?.asin, "KETTLE1");
  const x = plan.slots[0]?.posts.find((post) => post.channel === "x");
  assert.ok(x);
  assert.ok(x.text.length <= 280);
  assert.match(x.text, /ascsubtag=bb-x/);
  assert.match(plan.slots[0]?.posts.find((post) => post.channel === "telegram")?.text ?? "", /ascsubtag=bb-tg/);
  assert.equal(plan.slots[0]?.posts.find((post) => post.channel === "whatsapp")?.delivery, "copy");
});

test("pick rotates and stays off phones", () => {
  const second: Deal = { ...dove, asin: "NIVEA1", title: "Nivea Men Active Clean Shower Gel" };
  const first = pickDeal([phone, dove, second], "beauty", new Date("2026-01-01T07:00:00Z"));
  const later = pickDeal([phone, dove, second], "beauty", new Date("2026-01-02T07:00:00Z"));
  assert.notEqual(first?.asin, "PHONE1");
  assert.ok(first && later);
  assert.notEqual(first.asin, later.asin);
});

test("caption always discloses the affiliate link", () => {
  const text = captionFor(dove, "instagram");
  assert.match(text, /^#ad /);
  assert.match(text, /tag=dammaiprasad-21/);
  assert.match(text, /ascsubtag=bb-ig/);
});
