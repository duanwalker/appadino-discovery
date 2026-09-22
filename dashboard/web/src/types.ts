// Mirrors dashboard/functions_api/function_app.py's response shapes exactly —
// keep in sync by hand (no shared codegen yet, flagged as a future nicety, not
// a gap worth blocking G2.x on for a single-tenant dev review).

export type ProspectStatus = "new" | "reviewed" | "approved" | "rejected";

export interface SignalScore {
  score: number | null;
  rationale: string;
  citation: string | null;
  needs_human_verification: boolean;
}

export interface ValuesSignals {
  leadership_composition: SignalScore;
  population_served: SignalScore;
  mission_language: SignalScore;
  programming: SignalScore;
  funder_base: SignalScore;
}

export interface AlignmentCriterion {
  met: boolean;
  rationale: string;
  citation: string | null;
}

export interface Alignment {
  criteria: Record<string, AlignmentCriterion>;
  criteria_met_count: number;
  qualifies: boolean;
}

export interface Capacity {
  dd_present: boolean | null;
  fundraising_spend_ratio: number | null;
  note: string;
}

export interface Score {
  values_signals: ValuesSignals | null;
  alignment: Alignment | null;
  capacity: Capacity | null;
  soft_flags: { heavy_govt_funding: boolean | null } | null;
  disqualified: boolean | null;
  dq_reason: string | null;
}

// §5 v2.3 contact columns. Name/title are always populated (free, from the org's own
// 990 filing officers) regardless of enrichment_enabled. Email/phone/email_status
// stay null until `enriched` is true — a human has clicked Enrich for this prospect
// at least once. `enriched && email_status === "not_found"` is a real result (no
// contact found), distinct from `!enriched` (never tried) — the dashboard must not
// show the same "locked" UI for both.
export type EmailStatus = "verified" | "catch_all" | "not_found" | "stale_likely_moved";

export interface Contact {
  name: string | null;
  title: string | null;
  enriched: boolean;
  email: string | null;
  email_status: EmailStatus | null;
  stale_detail: string | null;
  phone: string | null;
  provider_confidence: number | null;
  credits_spent: number | null;
  retention_expires_at: string | null;
  retention_expired: boolean;
  enrichment_enabled: boolean;
  can_enrich: boolean;
  cannot_enrich_reason: string | null;
  // Name/Title always track the *current* latest-filing officer; the enrichment
  // result stays pinned to whoever was actually submitted. These can diverge when
  // leadership changes between an enrichment and a later filing update (real case:
  // an org's enrichment is for a since-departed exec, Name/Title now shows their
  // successor) — contact_mismatch flags it so the email/phone are never silently
  // implied to belong to whoever Name/Title currently shows.
  enriched_contact_name: string | null;
  enriched_contact_title: string | null;
  contact_mismatch: boolean;
}

export interface Prospect {
  id: number;
  ein: string;
  client_id: number;
  status: ProspectStatus;
  assigned_trigger: string | null;
  trigger_angle: string | null;
  trigger_evidence: Record<string, unknown> | null;
  gap_rank: number | null;
  suppression_flag: string | null;
  notes: string | null;
  updated_by: string | null;
  updated_at: string;
  org: {
    name: string;
    city: string | null;
    state: string | null;
    revenue_latest: number | null;
  };
  score: Score;
  contact: Contact;
}

export interface SuppressionEntry {
  id: number;
  client_id: number;
  ein: string | null;
  org_name: string;
  kind: "client" | "active_prospect" | "partner_attribution";
  source: string | null;
  added_at: string;
  partner_window_expires_at: string | null;
}

export interface Run {
  id: number;
  client_id: number | null;
  stage: string;
  started_at: string;
  finished_at: string | null;
  status: string;
  counts: Record<string, unknown> | null;
  error: string | null;
}
