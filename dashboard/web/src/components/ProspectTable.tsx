import { Fragment, useEffect, useState } from "react";
import { fetchProspects, updateProspect } from "../api";
import type { Prospect, ProspectStatus } from "../types";
import { StatusButtons } from "./StatusButtons";
import { ProspectDetail } from "./ProspectDetail";
import { SuppressionFlagBanner } from "./SuppressionFlagBanner";
import { ContactCells } from "./ContactCells";

function formatRevenue(value: number | null): string {
  if (value === null) return "—";
  return `$${Math.round(value).toLocaleString()}`;
}

export function ProspectTable({ clientId, reviewerName }: { clientId: number; reviewerName: string }) {
  const [prospects, setProspects] = useState<Prospect[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [notesDraft, setNotesDraft] = useState<Record<number, string>>({});
  const [savingId, setSavingId] = useState<number | null>(null);

  function load() {
    fetchProspects(clientId)
      .then(setProspects)
      .catch((err) => setError(err.message));
  }

  useEffect(load, [clientId]);

  async function handleStatusChange(prospect: Prospect, status: ProspectStatus) {
    if (!reviewerName) {
      setError("Enter your name above before changing a status.");
      return;
    }
    setSavingId(prospect.id);
    try {
      await updateProspect(prospect.id, { status, updated_by: reviewerName });
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingId(null);
    }
  }

  async function handleSaveNotes(prospect: Prospect) {
    if (!reviewerName) {
      setError("Enter your name above before saving a note.");
      return;
    }
    setSavingId(prospect.id);
    try {
      await updateProspect(prospect.id, { notes: notesDraft[prospect.id] ?? "", updated_by: reviewerName });
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingId(null);
    }
  }

  if (error) return <div className="banner banner--error">{error}</div>;
  if (prospects === null) return <div className="banner">Loading prospects…</div>;
  if (prospects.length === 0) return <div className="banner">No published prospects for this client yet.</div>;

  return (
    <table className="prospect-table">
      <thead>
        <tr>
          <th>#</th>
          <th>Organization</th>
          <th>Revenue</th>
          <th>Gap rank</th>
          <th>Criteria met</th>
          <th>Trigger</th>
          <th>Contact name</th>
          <th>Title</th>
          <th>Email</th>
          <th>Phone</th>
          <th>Contact status</th>
          <th>Status</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {prospects.map((prospect, index) => {
          const expanded = expandedId === prospect.id;
          return (
            <Fragment key={prospect.id}>
              <tr className={prospect.suppression_flag ? "row--flagged" : ""}>
                <td>{index + 1}</td>
                <td>
                  <div className="org-name">{prospect.org.name}</div>
                  <div className="org-location">
                    {[prospect.org.city, prospect.org.state].filter(Boolean).join(", ")}
                  </div>
                </td>
                <td>{formatRevenue(prospect.org.revenue_latest)}</td>
                <td>{prospect.gap_rank !== null ? prospect.gap_rank.toFixed(1) : "—"}</td>
                <td>{prospect.score.alignment?.criteria_met_count ?? "—"}/6</td>
                <td>
                  {prospect.assigned_trigger ? (
                    <>
                      <div>{prospect.assigned_trigger.replace(/_/g, " ")}</div>
                      {prospect.trigger_angle && <div className="trigger-angle">{prospect.trigger_angle}</div>}
                    </>
                  ) : (
                    "—"
                  )}
                </td>
                <ContactCells prospect={prospect} onEnriched={load} />
                <td>
                  <StatusButtons
                    current={prospect.status}
                    disabled={savingId === prospect.id}
                    onChange={(status) => handleStatusChange(prospect, status)}
                  />
                </td>
                <td>
                  <button
                    type="button"
                    className="button button--link"
                    onClick={() => setExpandedId(expanded ? null : prospect.id)}
                  >
                    {expanded ? "Hide" : "Details"}
                  </button>
                </td>
              </tr>
              {expanded && (
                <tr className="row--detail">
                  <td colSpan={13}>
                    {prospect.suppression_flag && (
                      <SuppressionFlagBanner
                        prospectId={prospect.id}
                        flag={prospect.suppression_flag}
                        reviewerName={reviewerName}
                        onResolved={load}
                      />
                    )}
                    <ProspectDetail score={prospect.score} />
                    <div className="notes-editor">
                      <h4>Review notes</h4>
                      <textarea
                        value={notesDraft[prospect.id] ?? prospect.notes ?? ""}
                        onChange={(e) => setNotesDraft({ ...notesDraft, [prospect.id]: e.target.value })}
                        rows={3}
                        placeholder="Add a note for this prospect…"
                      />
                      <button
                        type="button"
                        className="button"
                        disabled={savingId === prospect.id}
                        onClick={() => handleSaveNotes(prospect)}
                      >
                        Save note
                      </button>
                      <div className="notes-editor__meta">
                        Last updated {prospect.updated_by ? `by ${prospect.updated_by}` : ""} at{" "}
                        {new Date(prospect.updated_at).toLocaleString()}
                      </div>
                    </div>
                  </td>
                </tr>
              )}
            </Fragment>
          );
        })}
      </tbody>
    </table>
  );
}
