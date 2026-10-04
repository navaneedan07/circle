/**
 * A priority queue in front of Ollama.
 *
 * Ollama serves one request at a time. That is fine until an import starts:
 * ingestion embeds messages in batches back to back, and a question asked
 * during that time waits behind them. Measured on a real archive, the same
 * question took 6s when idle and over 100s mid-import -- which reads to the
 * user as "the model is not connected".
 *
 * The fix is not to make imports slower. It is to stop queued background work
 * from delaying a question that somebody is waiting on:
 *
 *   - A question (interactive) goes to the FRONT of the queue.
 *   - An import (background) goes to the back, and will not start while a
 *     question is queued or running.
 *
 * A request already in flight cannot be recalled, so the worst case for a
 * question is one batch -- not the whole import.
 *
 * The subtlety this file exists to get right: chaining everything onto a
 * single promise tail puts a late interactive request BEHIND everything
 * already queued, which is exactly the bug it is supposed to fix. Priority
 * only works if the queue itself is ordered.
 */
export type Priority = "interactive" | "background";

const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

interface Waiter {
  priority: Priority;
  run: () => void;
}

/** Pending work, ordered: interactive first, background last. */
const queue: Waiter[] = [];
let running = false;

function takeNext(): Waiter | undefined {
  // An interactive request is always served before any background one, no
  // matter when each of them arrived.
  const index = queue.findIndex((w) => w.priority === "interactive");
  return index === -1 ? queue.shift() : queue.splice(index, 1)[0];
}

async function pump(): Promise<void> {
  if (running) return;
  running = true;
  try {
    for (;;) {
      // Background work yields while a question is outstanding, so an import
      // cannot start a batch that would keep somebody waiting.
      if (queue.length > 0 && queue[0]!.priority === "background" && queue.some((w) => w.priority === "interactive")) {
        await sleep(20);
        continue;
      }
      const next = takeNext();
      if (!next) return;
      next.run();
    }
  } finally {
    running = false;
  }
}

/**
 * Run `fn` with exclusive access to Ollama, ahead of any queued background
 * work when `priority` is "interactive".
 */
export function withOllama<T>(priority: Priority, fn: () => Promise<T>): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    queue.push({
      priority,
      run: () => {
        fn().then(resolve, reject);
      },
    });
    void pump();
  });
}
