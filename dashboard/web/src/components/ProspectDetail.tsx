import type { Score } from "../types";

const SIGNAL_LABELS: Record<string, string> = {
  leadership_composition: "Leadership composition",
  population_served: "Population served",
  mission_language: "Mission language",
  programming: "Programming",
  funder_base: "Funder base",
};

const CRITERION_LABELS: Record<string, string> = {
  priority_tier_metro: "Priority-tier metro",
  leadership_advances_equity: "Leadership advances equity",
  mission_alignment: "Mission alignment",
  case_study_potential: "Case-study potential",
  connected_to_influencer_networks: "Connected to influencer networks",
  at_inflection_point: "At an inflection point",
};

export function ProspectDetail({ score }: { score: Score }) {
  if (!score.values_signals || !score.alignment) {
    return <div className="detail detail--empty">No Sonnet-stage score found for this prospect.</div>;
  }

  return (
    <div className="detail">
      <div className="detail__section">
        <h4>Values signals</h4>
        <table className="detail-table">
          <tbody>
            {Object.entries(score.values_signals).map(([key, signal]) => (
              <tr key={key}>
                <td className="detail-table__label">{SIGNAL_LABELS[key] ?? key}</td>
                <td className="detail-table__score">{signal.score ?? "—"}</td>
                <td className="detail-table__rationale">
                  {signal.rationale}
                  {signal.citation && <div className="citation">“{signal.citation}”</div>}
                  {signal.needs_human_verification && (
                    <span className="badge badge--warn">needs human verification</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="detail__section">
        <h4>
          Alignment ({score.alignment.criteria_met_count}/6){" "}
          <span className={score.alignment.qualifies ? "badge badge--success" : "badge badge--muted"}>
            {score.alignment.qualifies ? "qualifies" : "does not qualify"}
          </span>
        </h4>
        <table className="detail-table">
          <tbody>
            {Object.entries(score.alignment.criteria).map(([key, criterion]) => (
              <tr key={key}>
                <td className="detail-table__label">{CRITERION_LABELS[key] ?? key}</td>
                <td className="detail-table__score">
                  <span className={criterion.met ? "badge badge--success" : "badge badge--muted"}>
                    {criterion.met ? "met" : "not met"}
                  </span>
                </td>
                <td className="detail-table__rationale">
                  {criterion.rationale}
                  {criterion.citation && <div className="citation">“{criterion.citation}”</div>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {score.capacity && (
        <div className="detail__section">
          <h4>Capacity (public criteria only)</h4>
          <p>
            Due diligence process present: <strong>{score.capacity.dd_present ? "yes" : "no"}</strong> · Fundraising
            spend ratio:{" "}
            <strong>
              {score.capacity.fundraising_spend_ratio !== null
                ? score.capacity.fundraising_spend_ratio.toFixed(3)
                : "—"}
            </strong>
          </p>
          <p className="detail__note">{score.capacity.note}</p>
        </div>
      )}

      {score.soft_flags?.heavy_govt_funding && (
        <div className="detail__section">
          <span className="badge badge--warn">heavy government funding</span>
        </div>
      )}
    </div>
  );
}
