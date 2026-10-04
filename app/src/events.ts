/** Minimal in-process event broker (replaces the Python SSE broker). */
export type Subscriber = (event: { type: string; payload: Record<string, unknown> }) => void;

export class EventBroker {
  private subscribers = new Set<Subscriber>();

  subscribe(fn: Subscriber): () => void {
    this.subscribers.add(fn);
    return () => this.subscribers.delete(fn);
  }

  publish(type: string, payload: Record<string, unknown>): void {
    for (const fn of this.subscribers) {
      try {
        fn({ type, payload });
      } catch {
        /* a slow subscriber must not break the pipeline */
      }
    }
  }

  get subscriberCount(): number {
    return this.subscribers.size;
  }
}

export const broker = new EventBroker();
