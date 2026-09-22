import type { ProspectStatus } from "../types";

const STATUSES: ProspectStatus[] = ["new", "reviewed", "approved", "rejected"];

export function StatusButtons({
  current,
  onChange,
  disabled,
}: {
  current: ProspectStatus;
  onChange: (status: ProspectStatus) => void;
  disabled?: boolean;
}) {
  return (
    <div className="status-buttons" role="group" aria-label="Prospect status">
      {STATUSES.map((status) => (
        <button
          key={status}
          type="button"
          disabled={disabled}
          className={`status-button status-button--${status} ${current === status ? "status-button--active" : ""}`}
          onClick={() => onChange(status)}
        >
          {status}
        </button>
      ))}
    </div>
  );
}
