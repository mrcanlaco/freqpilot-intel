"use strict";

const {createHash} = require("node:crypto");
const {STREAMS, ideas, makeDraft, ORIGIN} = require("../src/bti/static/square-streams.js");
const digest = value => createHash("sha256").update(JSON.stringify(value)).digest("hex");

async function readPublic(route) {
  const res = await fetch(new URL(route, ORIGIN), {redirect: "error", signal: AbortSignal.timeout(60000),
    headers: {Accept: "application/json"}});
  if (res.status === 404 && route.startsWith("/api/articles/")) return null;
  if (!res.ok) throw new Error(`Intel HTTP ${res.status}`);
  return res.json();
}

module.exports = {STREAMS, ideas, makeDraft, readPublic, digest, ORIGIN};
