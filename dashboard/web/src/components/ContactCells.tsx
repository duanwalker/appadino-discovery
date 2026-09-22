import type { ReactNode } from "react";
import type { Contact } from "../types";
import { useEnrich } from "../hooks/useEnrich";

const STATUS_LABEL: Record<string, string> = {
  verified: "Verified",
  catch_all: "Catch-all",
  not_found: "Not found",
  stale_likely_moved: "Stale — likely moved",
};

function formatDate(iso: string | null): string {
  if (!iso) return "";
  return new Date(iso).toLocaleDateString();
}

function StatusBadge({ contact }: { contact: Contact }) {
  const status = contact.email_status;
  if (!status) return <>—</>;
  const variant =
    status === "verified"
      ? "badge--success"
      : status === "stale_likely_moved"
        ? "badge--warn"
        : status === "not_found"
          ? "badge--muted"
          : "badge--warn";
  return (
    <span
      className={`badge ${variant}`}
      title={
        status === "stale_likely_moved" && contact.stale_detail
          ? `FullEnrich resolved a different domain (${contact.stale_detail}) than the org's own — likely no longer at this org, even though the contact itself is real.`
          : `Provider confidence: ${contact.provider_confidence ?? "—"}`
      }
    >
      {STATUS_LABEL[status] ?? status}
    </span>
  );
}

/** §5 v2.3: five contact columns. Name/Title are always rendered (free, from the
 * org's own 990 filing, unverified). Email/Phone/Status stay locked until a human
 * clicks Enrich for this prospect — this component owns that button and its
 * submit-then-poll pending state (useEnrich), so the dashboard never blocks
 * silently on FullEnrich's async response. */
export function ContactCells({ prospect, onEnriched }: { prospect: { id: number; contact: Contact }; onEnriched: () => void }) {
  const { contact } = prospect;
  const { pending, error, trigger } = useEnrich(prospect.id, onEnriched);

  const nameCell = (
    <>
      {contact.name ?? "—"}
      {contact.name && (
        <span className="badge badge--muted contact-unverified" title="From the org's own 990 filing — not confirmed by any enrichment provider">
          unverified
        </span>
      )}
    </>
  );
  const titleCell = contact.title ?? "—";

  let emailCell: ReactNode;
  let phoneCell: ReactNode;
  let statusCell: ReactNode;

  if (pending) {
    emailCell = <span className="spinner" aria-label="Enriching…" />;
    phoneCell = <span className="spinner" aria-hidden="true" />;
    statusCell = <span className="spinner" aria-hidden="true" />;
  } else if (!contact.enriched) {
    const lockReason = !contact.enrichment_enabled
      ? "Enrichment isn't enabled for this account — contact us to upgrade."
      : contact.cannot_enrich_reason ?? "Approve this prospect to enable enrichment.";
    if (contact.can_enrich) {
      emailCell = (
        <button type="button" className="button button--small" onClick={() => void trigger()}>
          Enrich
        </button>
      );
    } else {
      emailCell = (
        <span className="contact-locked" title={lockReason}>
          🔒 Enrich to unlock
        </span>
      );
    }
    phoneCell = (
      <span className="contact-locked" title={lockReason}>
        Enrich to unlock
      </span>
    );
    statusCell = (
      <span className="contact-locked" title={lockReason}>
        Enrich to unlock
      </span>
    );
  } else {
    const expired = contact.retention_expired;
    emailCell = <span className={expired ? "contact-expired" : ""}>{contact.email ?? "—"}</span>;
    phoneCell = <span className={expired ? "contact-expired" : ""}>{contact.phone ?? "—"}</span>;
    statusCell = (
      <div className={expired ? "contact-expired" : ""}>
        <StatusBadge contact={contact} />
        {expired && <div className="contact-expired-note">expired {formatDate(contact.retention_expires_at)}</div>}
      </div>
    );
  }

  // Name/Title always reflect the current officer; the enrichment stays pinned to
  // whoever was actually submitted. When those diverge (leadership changed since
  // the last enrichment), say so instead of letting the row imply the email/phone
  // belong to whoever Name/Title currently shows.
  const mismatchNote = contact.contact_mismatch && (
    <div className="contact-mismatch-note" title="Name/Title above reflect the org's current filing; this contact info is from an earlier enrichment for a different, likely departed, person.">
      ⚠ enriched for {contact.enriched_contact_name}
      {contact.enriched_contact_title ? `, ${contact.enriched_contact_title}` : ""}
    </div>
  );

  return (
    <>
      <td>{nameCell}</td>
      <td>{titleCell}</td>
      <td>
        {emailCell}
        {error && <div className="contact-error">{error}</div>}
        {mismatchNote}
      </td>
      <td>{phoneCell}</td>
      <td>{statusCell}</td>
    </>
  );
}
