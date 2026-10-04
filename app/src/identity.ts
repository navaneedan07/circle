/**
 * Identity resolution.
 *
 * Deterministic-first: an exact email, phone or username match resolves to the
 * same person. Names are compared only after normalization, and an uncertain
 * match becomes a *suggestion* the user confirms -- Circle never silently
 * merges two humans who happen to share a first name.
 */
import { normalizeName, repairMojibake } from "./parsers/common.js";
import { nowIso } from "./domain.js";
import type { IdentityLink, Person, SourceType } from "./domain.js";
import type { Store } from "./store.js";

export interface ResolveResult {
  person: Person;
  created: boolean;
}

const USER_LABELS = new Set(["you", "me", "self", "owner"]);

export class IdentityResolver {
  constructor(
    private readonly store: Store,
    private readonly userLabel = "You"
  ) {}

  isUserLabel(label: string): boolean {
    return USER_LABELS.has(label.trim().toLowerCase());
  }

  /** Resolve or create a person for a sender in a chat source. */
  resolveSender(
    label: string,
    source: SourceType,
    extras: { email?: string; phone?: string; username?: string } = {}
  ): ResolveResult {
    const clean = repairMojibake(label).trim();

    // Deterministic matches first.
    if (extras.email) {
      const found = this.store.findPersonByIdentity("email", extras.email.toLowerCase());
      if (found) return { person: found, created: false };
    }
    if (extras.phone) {
      const found = this.store.findPersonByIdentity("phone", normalizePhone(extras.phone));
      if (found) return { person: found, created: false };
    }
    if (extras.username) {
      const found = this.store.findPersonByIdentity("username", extras.username.toLowerCase());
      if (found) return { person: found, created: false };
    }
    if (clean && !this.isUserLabel(clean)) {
      const byName = this.store.findPersonByName(clean);
      if (byName) return { person: byName, created: false };
    }

    const isUser = this.isUserLabel(clean);
    const identities: IdentityLink[] = [];
    if (extras.email) identities.push({ kind: "email", value: extras.email.toLowerCase(), source, origin: "imported" });
    if (extras.phone) identities.push({ kind: "phone", value: normalizePhone(extras.phone), source, origin: "imported" });
    if (extras.username) identities.push({ kind: "username", value: extras.username.toLowerCase(), source, origin: "imported" });
    if (clean) identities.push({ kind: "name", value: normalizeName(clean), source, label: clean, origin: "imported" });

    const person: Person = {
      id: "",
      display_name: isUser ? this.userLabel : clean || "Unknown",
      aliases: [],
      identities,
      avatar_color: colorFor(clean || "unknown"),
      is_user: isUser,
      merged_from: [],
      created_at: nowIso(),
      updated_at: nowIso(),
      origin: "imported",
    };
    const created = this.store.insertPerson(person);
    return { person: created, created: true };
  }

  /** Ingest a contacts list; emails/phones seed the identity table. */
  ingestContacts(
    contacts: { name: string; emails: string[]; phones: string[]; aliases?: string[] }[]
  ): number {
    let created = 0;
    for (const contact of contacts) {
      if (!contact.name?.trim()) continue;
      const existing =
        (contact.emails[0] && this.store.findPersonByIdentity("email", contact.emails[0]!.toLowerCase())) ||
        (contact.phones[0] && this.store.findPersonByIdentity("phone", normalizePhone(contact.phones[0]!))) ||
        this.store.findPersonByName(contact.name);
      if (existing) {
        const merged = mergeIdentities(existing, contact, "contacts");
        this.store.updatePerson(merged);
        continue;
      }
      const person: Person = {
        id: "",
        display_name: repairMojibake(contact.name).trim(),
        aliases: contact.aliases ?? [],
        identities: [
          ...contact.emails.map((e): IdentityLink => ({ kind: "email", value: e.toLowerCase(), source: "contacts", origin: "imported" })),
          ...contact.phones.map((p): IdentityLink => ({ kind: "phone", value: normalizePhone(p), source: "contacts", origin: "imported" })),
          { kind: "name", value: normalizeName(contact.name), source: "contacts", label: contact.name, origin: "imported" },
        ],
        avatar_color: colorFor(contact.name),
        is_user: false,
        merged_from: [],
        created_at: nowIso(),
        updated_at: nowIso(),
        origin: "imported",
      };
      this.store.insertPerson(person);
      created++;
    }
    return created;
  }

  /**
   * Queue a possible duplicate for review.
   *
   * Never merges. The user decides in Settings > Possible matches.
   */
  queueSuggestion(a: Person, b: Person, reason: string): void {
    if (!a.id || !b.id || a.id === b.id) return;
    const [first, second] = a.id < b.id ? [a, b] : [b, a];
    const id = `sugg-${first.id}-${second.id}`;
    if (this.store.getIdentitySuggestion(id)) return;
    this.store.upsertIdentitySuggestion(id, {
      id,
      label: `${first.display_name} / ${second.display_name}`,
      reason,
      person_a_id: first.id,
      person_b_id: second.id,
      status: "pending",
    });
  }

  /** Accept a suggestion: move every record onto the surviving person. */
  mergePeople(keepId: string, removeId: string): number {
    const keep = this.store.getPerson(keepId);
    const remove = this.store.getPerson(removeId);
    if (!keep || !remove) return 0;
    const moved = this.store.reassignPersonRecords(keepId, removeId);
    keep.aliases = [...new Set([...(keep.aliases || []), remove.display_name, ...(remove.aliases || [])])];
    keep.identities = [...(keep.identities || []), ...(remove.identities || [])];
    keep.merged_from = [...new Set([...(keep.merged_from || []), removeId])];
    keep.updated_at = nowIso();
    this.store.updatePerson(keep);
    this.store.deletePerson(removeId);
    return moved;
  }
}

function mergeIdentities(
  person: Person,
  contact: { name: string; emails: string[]; phones: string[]; aliases?: string[] },
  source: string
): Person {
  const identities = [...(person.identities || [])];
  const have = new Set(identities.map((i) => `${i.kind}:${i.value}`));
  for (const email of contact.emails) {
    const key = `email:${email.toLowerCase()}`;
    if (!have.has(key)) {
      identities.push({ kind: "email", value: email.toLowerCase(), source: source as SourceType, origin: "imported" });
      have.add(key);
    }
  }
  for (const phone of contact.phones) {
    const key = `phone:${normalizePhone(phone)}`;
    if (!have.has(key)) {
      identities.push({ kind: "phone", value: normalizePhone(phone), source: source as SourceType, origin: "imported" });
      have.add(key);
    }
  }
  return { ...person, identities, aliases: [...new Set([...(person.aliases || []), ...(contact.aliases || [])])] };
}

export function normalizePhone(phone: string): string {
  return phone.replace(/[^\d+]/g, "");
}

function colorFor(seed: string): string {
  const palette = ["#6366f1", "#8b5cf6", "#ec4899", "#f59e0b", "#10b981", "#06b6d4", "#ef4444", "#84cc16"];
  let hash = 0;
  for (const ch of seed) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return palette[hash % palette.length]!;
}
