import { COMPUTED_LABEL, MEASURED_LABEL } from "../sim/provenance";

interface ProvenanceProps {
  kind: "measured" | "computed";
  detail?: string;
  className?: string;
}

/** The one place a panel says where its numbers came from. */
export function Provenance({ kind, detail, className }: ProvenanceProps) {
  const base = kind === "measured" ? MEASURED_LABEL : COMPUTED_LABEL;
  return (
    <span className={className ? `provenance ${className}` : "provenance"}>
      {detail ? `${base}. ${detail}` : base}
    </span>
  );
}
