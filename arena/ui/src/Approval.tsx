import { useEffect, useState } from "react";
import type { Row } from "./api";
import { safeLink } from "./describe";

function clock(totalSeconds: number): string {
  const s = Math.max(0, Math.ceil(totalSeconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/**
 * The real-sandbox step: funds are held only after the buyer approves in PayPal. The countdown
 * starts when this banner first sees the row, which is also right after a page refresh replay.
 */
export function ApprovalBanner({ row }: { row: Row }) {
  const [now, setNow] = useState(() => Date.now());
  const [start, setStart] = useState(() => Date.now());

  useEffect(() => {
    setStart(Date.now());
  }, [row.id]);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  const limit = Number(row.wait_s) || 0;
  const left = limit - (now - start) / 1000;
  const href = safeLink(row.link);

  return (
    <div className="approval">
      <div className="approval-head">
        <span role="status">Waiting for the buyer to approve the payment in PayPal</span>
        {limit ? <span aria-hidden="true"> — {clock(left)}</span> : null}
      </div>
      <p>
        This is the <b>PayPal sandbox</b>: test accounts, fake money. Trustee holds nothing until the buyer approves
        the payment in PayPal.
      </p>
      {href ? (
        <a className="approve-btn" href={href} target="_blank" rel="noopener noreferrer">
          Approve in PayPal sandbox
        </a>
      ) : null}
    </div>
  );
}
