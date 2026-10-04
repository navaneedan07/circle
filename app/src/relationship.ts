/**
 * Relationship metrics.
 *
 * Status is explainable ("Active — 12 interactions in the last 14 days"), not
 * a black box. Topics come from the messages, and a person's own name is never
 * allowed to become one of their topics.
 */
import { deterministicId, extractTopics, normalizeName } from "./parsers/common.js";
import { nowIso } from "./domain.js";
import type {
  RelationshipEvent,
  RelationshipProfile,
  RelationshipStatus,
  SourceType,
  TopicStat,
} from "./domain.js";
import type { Store } from "./store.js";

const DAY = 86_400_000;

export function recordEvent(
  store: Store,
  event: Omit<RelationshipEvent, "id" | "origin"> & { origin?: RelationshipEvent["origin"] }
): void {
  const id = `evt-${deterministicId(event.person_id, event.kind, event.record_id || event.summary, event.occurred_at)}`;
  store.insertRelationshipEvent({
    id,
    person_id: event.person_id,
    kind: event.kind,
    source: event.source,
    occurred_at: event.occurred_at,
    summary: event.summary,
    record_id: event.record_id,
    origin: event.origin ?? "imported",
  });
}

/** Status thresholds, in interactions over the window. */
export function statusFor(interactions14d: number, interactions60d: number, lastAt: string | null): {
  status: RelationshipStatus;
  reason: string;
} {
  const daysSince = lastAt ? Math.floor((Date.now() - new Date(lastAt).getTime()) / DAY) : null;
  if (interactions14d >= 8) {
    return { status: "Very Active", reason: `${interactions14d} interactions in the last 14 days` };
  }
  if (interactions14d >= 3) {
    return { status: "Active", reason: `${interactions14d} interactions in the last 14 days` };
  }
  if (interactions60d >= 1) {
    return { status: "Occasional", reason: `${interactions60d} interactions in the last 60 days` };
  }
  if (daysSince !== null) {
    return { status: "Low Activity", reason: `last interaction ${daysSince} days ago` };
  }
  return { status: "No Recent Activity", reason: "no recorded interactions" };
}

export function computeProfile(store: Store, personId: string): RelationshipProfile | null {
  const person = store.getPerson(personId);
  if (!person) return null;
  const events = store.listRelationshipEvents(personId, 1000);
  const now = Date.now();

  const within = (days: number) =>
    events.filter((e) => now - new Date(e.occurred_at).getTime() <= days * DAY);

  const events14 = within(14);
  const events60 = within(60);
  const lastAt = events.length > 0 ? events[0]!.occurred_at : null;
  const { status, reason } = statusFor(events14.length, events60.length, lastAt);

  const sourceBreakdown: Record<string, number> = {};
  for (const e of events) sourceBreakdown[e.source] = (sourceBreakdown[e.source] || 0) + 1;

  const topicMap = new Map<string, TopicStat>();
  const selfNames = new Set<string>([
    normalizeName(person.display_name),
    ...(person.aliases || []).map((a) => normalizeName(a)),
  ]);
  for (const event of events) {
    for (const topic of extractTopics(event.summary || "", 6)) {
      if (isNoiseTopic(topic, selfNames)) continue;
      const existing = topicMap.get(topic);
      if (existing) {
        existing.count++;
        if (!existing.last_seen || event.occurred_at > existing.last_seen) existing.last_seen = event.occurred_at;
      } else {
        topicMap.set(topic, { topic, count: 1, last_seen: event.occurred_at });
      }
    }
  }
  const topics = [...topicMap.values()].sort((a, b) => b.count - a.count).slice(0, 12);
  const activeTopics = topics.filter((t) => t.last_seen && now - new Date(t.last_seen).getTime() <= 60 * DAY);

  const upcoming = store.listCalendarUpcoming(personId, 10).map((e) => ({
    id: e.id,
    title: e.title,
    starts_at: e.starts_at,
    location: e.location,
  }));

  return {
    person_id: personId,
    status,
    status_reason: reason,
    interaction_count: events.length,
    interactions_14d: events14.length,
    interactions_60d: events60.length,
    last_interaction_at: lastAt,
    source_breakdown: sourceBreakdown,
    topics,
    active_topics: activeTopics,
    upcoming_events: upcoming,
    summary: "",
    summary_sources: [],
    updated_at: nowIso(),
  };
}

export function refreshProfile(store: Store, personId: string): void {
  const profile = computeProfile(store, personId);
  if (profile) store.upsertProfile(profile);
}

/**
 * A topic that is really just the person's name (or a token of it) is noise:
 * "Gladwin R" must not produce the topic "gladwin" on their own page.
 */
export function isNoiseTopic(topic: string, knownNames: Set<string>): boolean {
  const t = normalizeName(topic);
  if (!t || t.length < 3) return true;
  if (knownNames.has(t)) return true;
  for (const name of knownNames) {
    for (const token of name.split(" ")) {
      if (token.length >= 3 && token === t) return true;
    }
  }
  return false;
}

export function topicTotals(store: Store, limit = 10): [string, number][] {
  const people = store.allPeopleDocs();
  const known = new Set<string>();
  for (const p of people) {
    known.add(normalizeName(p.display_name));
    for (const token of normalizeName(p.display_name).split(" ")) {
      if (token.length >= 3) known.add(token);
    }
    for (const alias of p.aliases || []) known.add(normalizeName(alias));
  }
  const totals = new Map<string, number>();
  for (const profile of store.listProfiles()) {
    for (const t of profile.topics || []) {
      const topic = (t.topic || "").trim();
      if (!topic || isNoiseTopic(topic, known)) continue;
      totals.set(topic, (totals.get(topic) || 0) + (t.count || 0));
    }
  }
  return [...totals.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, limit);
}

export function sourceLabel(source: SourceType | string): string {
  const labels: Record<string, string> = {
    whatsapp: "WhatsApp",
    telegram: "Telegram",
    instagram: "Instagram",
    x: "X",
    email: "Email",
    calendar: "Calendar",
    contacts: "Contacts",
    notes: "Notes",
    voice: "Voice",
    chat: "Chat",
    document: "Document",
  };
  return labels[source] ?? String(source);
}
