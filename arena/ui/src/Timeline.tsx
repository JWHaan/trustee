import { Check, Minus, Undo2, UserCheck, X } from "lucide-react";
import type { Row, Snapshot } from "./api";
import { deriveTimeline, type StepStatus } from "./derive";

function Mark({ status, n }: { status: StepStatus; n: number }) {
  switch (status) {
    case "done":
      return <Check size={14} strokeWidth={3} aria-hidden="true" />;
    case "person":
      return <UserCheck size={14} strokeWidth={2.5} aria-hidden="true" />;
    case "failed":
      return <X size={14} strokeWidth={3} aria-hidden="true" />;
    case "refund":
      return <Undo2 size={14} strokeWidth={2.5} aria-hidden="true" />;
    case "skipped":
      return <Minus size={14} aria-hidden="true" />;
    default:
      return <span aria-hidden="true">{n}</span>;
  }
}

const SPOKEN: Record<StepStatus, string> = {
  todo: "pending",
  active: "in progress",
  done: "done",
  person: "passed only with a person's approval",
  failed: "failed",
  refund: "refunded",
  skipped: "not reached",
};

interface Props {
  rows: Row[];
  snap: Snapshot | null;
  running: boolean;
}

export function Timeline({ rows, snap, running }: Props) {
  const steps = deriveTimeline(rows, snap, running);
  return (
    <ol className="timeline" aria-label="deal timeline">
      {steps.map((s, i) => (
        <li key={s.key} className={`tstep ${s.status}`} aria-current={s.status === "active" ? "step" : undefined} title={s.reason}>
          <span className="bullet">
            <Mark status={s.status} n={i + 1} />
          </span>
          <span className="tlabel">
            {s.label}
            <span className="sr-only"> ({s.reason ?? SPOKEN[s.status]})</span>
          </span>
          <span className="tdetail">{s.detail}</span>
        </li>
      ))}
    </ol>
  );
}
