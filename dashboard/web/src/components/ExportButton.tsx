import { exportCsvUrl } from "../api";

export function ExportButton({ clientId }: { clientId: number }) {
  return (
    <a className="button button--primary" href={exportCsvUrl(clientId)} download>
      Export CSV
    </a>
  );
}
