/**
 * yadisk-knowledge — Knowledge Base Integration for Pi
 *
 * Port of AIWikiVault Claude Code hooks (session-start.py, session-end.py, pre-compact.py)
 * to Pi Coding Agent's extension system.
 *
 * Features:
 *   1. before_agent_start — injects SEK wiki context on first turn
 *   2. session_shutdown   — saves conversation context to daily log, spawns flush.py
 *   3. session_before_compact — same as shutdown, runs before compaction
 *
 * Install (project-local, auto /reload):
 *   .pi/extensions/yadisk-knowledge.ts
 *
 * Install (global):
 *   ~/.pi/agent/extensions/yadisk-knowledge.ts
 *
 * Requirements:
 *   - Node.js built-in modules only (fs, path, child_process)
 *   - uv + Python environment at AIWikiVault for flush.py (optional)
 *
 * @see D:\YandexDisk\Sync\Coding\AIWikiVault\wiki\references\pi-knowledge-extension.md
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import {
  readFileSync,
  writeFileSync,
  appendFileSync,
  existsSync,
  mkdirSync,
} from "fs";
import { join, resolve } from "path";
import { spawn } from "child_process";

// ═════════════════════════════════════════════════════════════════════
//  Configuration
// ═════════════════════════════════════════════════════════════════════

const VAULT_ROOT = resolve(
  "D:\\YandexDisk\\Sync\\Coding\\AIWikiVault",
);
const WIKI_DIR = join(VAULT_ROOT, "wiki");
const MEMORY_DIR = join(VAULT_ROOT, "memory");
const DAILY_DIR = join(VAULT_ROOT, "daily");
const SCRIPTS_DIR = join(VAULT_ROOT, "scripts");

const MAX_CONTEXT_CHARS = 20_000;
const MAX_LOG_LINES = 30;
const MAX_TURNS = 30;
const MIN_TURNS_TO_FLUSH = 5;

// ═════════════════════════════════════════════════════════════════════
//  Helpers
// ═════════════════════════════════════════════════════════════════════

function safeRead(path: string, fallback = ""): string {
  try {
    if (!existsSync(path)) return fallback;
    return readFileSync(path, "utf-8").trim();
  } catch {
    return fallback;
  }
}

/** Format date as "Monday, 15 June 2026" */
function formatDate(d: Date): string {
  const weekdays = [
    "Sunday", "Monday", "Tuesday", "Wednesday",
    "Thursday", "Friday", "Saturday",
  ];
  const months = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
  ];
  return `${weekdays[d.getDay()]}, ${d.getDate()} ${months[d.getMonth()]} ${d.getFullYear()}`;
}

/** ISO date string YYYY-MM-DD */
function isoDate(d: Date): string {
  return d.toISOString().slice(0, 10);
}

/** Read last N lines of the most recent daily log (today or yesterday) */
function getRecentLog(): string {
  const today = new Date();
  for (let offset = 0; offset < 2; offset++) {
    const d = new Date(today);
    d.setDate(d.getDate() - offset);
    const logPath = join(DAILY_DIR, `${isoDate(d)}.md`);
    const content = safeRead(logPath);
    if (content) {
      const lines = content.split("\n");
      return lines.slice(-MAX_LOG_LINES).join("\n");
    }
  }
  return "(no recent daily log)";
}

/** Assemble SEK knowledge context (L1 wiki + L3 inbox + legacy sources) */
function buildKnowledgeContext(): string {
  const parts: string[] = [];
  const today = new Date();

  parts.push(`## Today\n${formatDate(today)}`);

  // L1: Wiki routing index
  const wikiIndex = safeRead(join(WIKI_DIR, "_index.md"));
  if (wikiIndex) {
    parts.push(`## Wiki Index (L1)\n\n${wikiIndex}`);
  } else {
    parts.push("## Wiki Index (L1)\n\n(empty)");
  }

  // L3: Memory inbox
  const inbox = safeRead(join(MEMORY_DIR, "inbox.md"));
  if (inbox && /^\|.*\|/.test(inbox)) {
    parts.push(`## Memory Inbox (L3)\n\n${inbox}`);
  } else {
    parts.push("## Memory Inbox (L3)\n\n(empty — no active observations)");
  }

  // Legacy: knowledge base index
  const knowledgeIndex = safeRead(join(VAULT_ROOT, "knowledge", "index.md"));
  if (knowledgeIndex) {
    parts.push(`## Knowledge Base Index\n\n${knowledgeIndex}`);
  }

  // Legacy: recent daily log
  parts.push(`## Recent Daily Log\n\n${getRecentLog()}`);

  let context = parts.join("\n\n---\n\n");
  if (context.length > MAX_CONTEXT_CHARS) {
    context = context.slice(0, MAX_CONTEXT_CHARS) + "\n\n...(truncated)";
  }
  return context;
}

/** Extract last N conversation turns from session entries */
function extractConversation(entries: any[]): string {
  const turns: string[] = [];

  for (const entry of entries) {
    if (entry.type !== "message" || !entry.message?.role) continue;
    const role = entry.message.role;
    if (role !== "user" && role !== "assistant") continue;

    let content = entry.message.content;
    if (Array.isArray(content)) {
      content = content
        .filter((b: any) => b.type === "text" && b.text)
        .map((b: any) => b.text)
        .join("\n");
    }

    if (typeof content === "string" && content.trim()) {
      const label = role === "user" ? "User" : "Assistant";
      turns.push(`**${label}:** ${content.trim()}\n`);
    }
  }

  const recent = turns.slice(-MAX_TURNS);
  let context = recent.join("\n");

  if (context.length > MAX_CONTEXT_CHARS) {
    context = context.slice(-MAX_CONTEXT_CHARS);
    const boundary = context.indexOf("\n**");
    if (boundary > 0) context = context.slice(boundary + 1);
  }

  return context;
}

/** Append content to today's daily log (creates file if missing) */
function appendToDailyLog(content: string, section = "Session"): void {
  const today = new Date();
  const dateStr = isoDate(today);
  const logPath = join(DAILY_DIR, `${dateStr}.md`);

  if (!existsSync(logPath)) {
    mkdirSync(DAILY_DIR, { recursive: true });
    writeFileSync(
      logPath,
      `# Daily Log: ${dateStr}\n\n## Sessions\n\n## Memory Maintenance\n\n`,
      "utf-8",
    );
  }

  const timeStr = today.toTimeString().slice(0, 5);
  const entry = `### ${section} (${timeStr})\n\n${content}\n\n`;
  appendFileSync(logPath, entry, "utf-8");
}

/** Write context temp file and spawn flush.py in background */
function spawnFlushProcess(context: string, sessionId: string): void {
  const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
  const contextFile = join(
    SCRIPTS_DIR,
    `session-flush-${sessionId}-${timestamp}.md`,
  );

  try {
    writeFileSync(contextFile, context, "utf-8");
  } catch (e) {
    console.error("[kb] Failed to write context file:", e);
    return;
  }

  const flushScript = join(SCRIPTS_DIR, "flush.py");
  if (!existsSync(flushScript)) {
    console.error("[kb] flush.py not found, context saved to:", contextFile);
    return;
  }

  try {
    const child = spawn(
      "uv",
      [
        "run",
        "--directory",
        VAULT_ROOT,
        "python",
        flushScript,
        contextFile,
        sessionId,
      ],
      {
        stdio: "ignore",
        detached: true,
        windowsHide: true,
        shell: true,
      },
    );
    child.unref();
  } catch (e) {
    console.error("[kb] Failed to spawn flush.py:", e);
  }
}

// ═════════════════════════════════════════════════════════════════════
//  Extension Entry Point
// ═════════════════════════════════════════════════════════════════════

export default function (pi: ExtensionAPI) {
  // ── Track injection: only inject once per session ──
  let knowledgeInjected = false;

  // ── 1. Inject knowledge context on first turn ──
  //
  // Equivalent to session-start.py (Claude Code hook).
  // Fires before every agent turn, but we only inject once.
  //
  pi.on("before_agent_start", async (_event, ctx) => {
    if (knowledgeInjected) return;
    knowledgeInjected = true;

    const context = buildKnowledgeContext();
    ctx.ui.notify(
      `[KB] Injected wiki context (${context.length} chars)`,
      "info",
    );

    return {
      message: {
        customType: "yadisk-knowledge",
        content: context,
        display: true,
      },
    };
  });

  // ── 2. Save conversation on session shutdown ──
  //
  // Equivalent to session-end.py (Claude Code hook).
  //
  pi.on("session_shutdown", async (event, ctx) => {
    try {
      const entries = ctx.sessionManager.getBranch();
      const conversation = extractConversation(entries);

      if (!conversation.trim() || conversation.length < 200) return;

      appendToDailyLog(conversation, "Session");
      spawnFlushProcess(conversation, event.reason ?? "shutdown");
    } catch (e) {
      console.error("[kb] session_shutdown error:", e);
    }
  });

  // ── 3. Save conversation before compaction ──
  //
  // Equivalent to pre-compact.py (Claude Code hook).
  //
  pi.on("session_before_compact", async (event, ctx) => {
    try {
      const entries = ctx.sessionManager.getBranch();
      const conversation = extractConversation(entries);

      if (!conversation.trim() || conversation.length < 200) return;

      appendToDailyLog(conversation, "Memory Flush");
      spawnFlushProcess(conversation, "pre-compact");
    } catch (e) {
      console.error("[kb] session_before_compact error:", e);
    }
  });
}
