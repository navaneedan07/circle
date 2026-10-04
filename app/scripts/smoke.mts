/**
 * End-to-end smoke test.
 *
 * Creates a temp data dir and watch folder, drops a WhatsApp export and a Meta
 * (Instagram) data export in, runs the real pipeline, and checks the archive.
 * Run with:
 *   npx tsx scripts/smoke.mts
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { AppContext } from "../src/context.js";

function instagramThread(dir: string, folder: string, participants: string[], turns: [string, string][]): void {
  const thread = path.join(dir, folder);
  fs.mkdirSync(thread, { recursive: true });
  const messages = turns.map(([sender, content], i) => ({
    sender_name: sender,
    timestamp_ms: Date.UTC(2024, 4, 12, 9, 40 + i),
    content,
  }));
  fs.writeFileSync(
    path.join(thread, "message_1.json"),
    JSON.stringify({ participants: participants.map((name) => ({ name })), messages }, null, 2),
    "utf-8"
  );
}

async function main(): Promise<void> {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "circle-smoke-"));
  const watch = path.join(tmp, "MyBackups");
  fs.mkdirSync(watch, { recursive: true });

  const chat = [
    "12/05/2024, 9:41 PM - Messages and calls are end-to-end encrypted.",
    "12/05/2024, 9:42 PM - Alice: hey, did you finish the internship report?",
    "12/05/2024, 9:43 PM - You: almost, sending it tonight",
    "12/05/2024, 9:44 PM - Alice: great, the deadline is friday",
    "12/05/2024, 9:45 PM - Alice: also the trip photos are ready",
    "13/05/2024, 10:02 AM - Bob: morning!",
  ].join("\n");
  fs.writeFileSync(path.join(watch, "WhatsApp Chat with Alice.txt"), chat, "utf-8");

  const ctx = new AppContext(path.join(tmp, "data"));
  // Start first (no folder yet, so the watcher stays off) and then point the
  // pipeline at the folder: the assertions below drive processPath directly and
  // must not race the watcher for the same files.
  await ctx.start();
  ctx.setWatchFolder(watch);

  // Process the file directly through the pipeline (watcher timing is separate).
  const file = path.join(watch, "WhatsApp Chat with Alice.txt");
  const result = await ctx.pipeline.processPath(file);
  console.log("pipeline:", result.status, result.error || "");

  const people = ctx.store.listPeople(100);
  const alice = people.find((p) => p.display_name === "Alice");
  console.log("people:", people.map((p) => p.display_name).join(", "));
  console.log("messages:", ctx.store.countMessages());
  console.log("memories:", ctx.store.countMemories());

  // Asking a countable question must be answered from the archive, not the model.
  const { ask } = await import("../src/ai/rag.js");
  const top = await ask(ctx.ragDeps(), "who do I talk to most");
  console.log("ask(top):", JSON.stringify(top.answer));

  const recent = await ask(ctx.ragDeps(), "who did I talk to in the last 700 days");
  console.log("ask(recent):", JSON.stringify(recent.answer));

  // Keyword retrieval should find the internship discussion.
  const { hybridRetrieve } = await import("../src/ai/rag.js");
  const hits = await hybridRetrieve(ctx.ragDeps(), "internship report deadline", { k: 3 });
  console.log("retrieval hits:", hits.length, hits[0]?.text.slice(0, 60) ?? "");

  const whatsappOk =
    result.status === "COMPLETED" &&
    people.length >= 2 &&
    Boolean(alice) &&
    ctx.store.countMessages() === 5 &&
    top.answer.includes("Alice");
  console.log("whatsapp:", whatsappOk ? "PASS" : "FAIL");

  // ---- Meta export: every thread must land in its OWN conversation. ----
  // Regression: a typo'd `participants` key once made every thread collapse
  // onto one conversation, so the whole export looked like a single person.
  const exportRoot = path.join(
    watch,
    "meta-2026-Oct-02-05-25-53",
    "instagram-you-2026-10-02-abc123",
    "your_instagram_activity",
    "messages",
    "inbox"
  );
  instagramThread(exportRoot, "05esther__12_18005080511581432", ["05esther__12", "You"], [
    ["05esther__12", "hi! did you see the photos"],
    ["You", "yes, they look great"],
  ]);
  instagramThread(exportRoot, "kaviyasrikanth_17941679075581432", ["kaviyasrikanth", "You"], [
    ["kaviyasrikanth", "assignment due tomorrow"],
    ["You", "on it"],
    ["kaviyasrikanth", "thanks!"],
  ]);
  instagramThread(exportRoot, "arjun_1176371147183798", ["Arjun", "You"], [
    ["Arjun", "coffee this weekend?"],
  ]);

  // Meta noise must never be ingested: a non-conversation file in the export.
  fs.writeFileSync(path.join(exportRoot, "..", "saved_posts.json"), JSON.stringify({ media: [] }), "utf-8");

  const igResults: string[] = [];
  for (const folder of fs.readdirSync(exportRoot)) {
    const full = path.join(exportRoot, folder);
    if (!fs.statSync(full).isDirectory()) continue;
    igResults.push((await ctx.pipeline.processPath(path.join(full, "message_1.json"))).status);
  }
  const noiseResult = await ctx.pipeline.processPath(path.join(exportRoot, "..", "saved_posts.json"));

  const conversations = ctx.store.listConversations(100);
  const igConversations = conversations.filter((c) => c.source === "instagram");
  const igSenders = new Set(
    ctx.store
      .listPeople(500)
      .map((p) => p.display_name)
      .filter((n) => ["05esther__12", "kaviyasrikanth", "Arjun"].includes(n))
  );

  console.log("instagram jobs:", igResults.join(", "));
  console.log("noise skipped:", noiseResult.status);
  console.log("instagram conversations:", igConversations.map((c) => c.title).join(" | "));
  console.log("instagram conversation count:", igConversations.length);
  console.log("instagram senders found:", [...igSenders].join(", "));

  const instagramOk =
    igResults.every((s) => s === "COMPLETED") &&
    noiseResult.status === "SKIPPED" &&
    igConversations.length === 3 &&
    new Set(igConversations.map((c) => c.external_key)).size === 3 &&
    igSenders.size === 3;
  console.log("instagram:", instagramOk ? "PASS" : "FAIL");

  const ok = whatsappOk && instagramOk;
  console.log(ok ? "\nSMOKE PASS" : "\nSMOKE FAIL");

  await ctx.shutdown();
  fs.rmSync(tmp, { recursive: true, force: true });
  process.exit(ok ? 0 : 1);
}

void main();
