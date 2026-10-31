import { useEffect, useRef } from "react";
import { describe, actorName, isNarrative } from "./describe";
import type { Row } from "./api";

interface Props {
  rows: Row[];
  running: boolean;
}

export function Transcript({ rows, running }: Props) {
  const feedRef = useRef<HTMLDivElement>(null);
  const visible = rows.filter(isNarrative);
  const lastId = visible.length ? visible[visible.length - 1].id : null;

  // Follow the tape. Without this the newest events land below the fold and a
  // live run looks like it has stalled.
  useEffect(() => {
    const feed = feedRef.current;
    if (!feed) return;
    // rAF: the new row is in the DOM but not yet laid out when the effect runs,
    // so scrollHeight would be one row short.
    const frame = requestAnimationFrame(() => {
      feed.scrollTop = feed.scrollHeight;
    });
    return () => cancelAnimationFrame(frame);
  }, [visible.length]);

  if (!visible.length) {
    return (
      <div className="empty">
        <h2>No deal running</h2>
        <p>
          Pick a scenario below. Two agents negotiate a price, the trustee holds the money, and the seller either ships
          the module or ships a fake.
        </p>
      </div>
    );
  }

  return (
    <div className="feed" ref={feedRef}>
      {visible.map((row) => {
        const line = describe(row);
        return (
          <div
            key={row.id}
            className={`turn ${row.id === lastId && running ? "last" : ""}`}
            data-kind={row.kind}
          >
            <div className={`who actor-${actorName(row.actor)}`}>{actorName(row.actor)}</div>
            <div className="body">
              <div className="headline">{line.headline}</div>
              {line.detail ? <div className="sub">{line.detail}</div> : null}
            </div>
          </div>
        );
      })}
    </div>
  );
}
