import { useEffect, useMemo, useRef, useState } from "react";
import { AgGridReact } from "ag-grid-react";
import {
  themeQuartz,
  type ColDef,
  type ICellRendererParams,
  type IRowNode,
  type RowClassParams,
} from "ag-grid-community";
import { Check, ExternalLink, X } from "lucide-react";
import type { Row } from "./api";
import { actorName, safeLink } from "./describe";

const theme = themeQuartz.withParams({
  backgroundColor: "#0e1521",
  foregroundColor: "#e9eef6",
  headerBackgroundColor: "#0e1521",
  headerTextColor: "#a6b2c5",
  borderColor: "#1a253a",
  oddRowBackgroundColor: "#0c121d",
  fontFamily: "ui-sans-serif, -apple-system, BlinkMacSystemFont, 'Segoe UI', Inter, system-ui, sans-serif",
  fontSize: "12.5px",
  headerFontSize: "12px",
  headerFontWeight: 600,
  rowHeight: 30,
  headerHeight: 34,
  cellHorizontalPadding: 10,
  accentColor: "#9cc8ff",
  wrapperBorder: false,
  wrapperBorderRadius: 0,
  rowHoverColor: "rgba(156, 200, 255, 0.06)",
});

const KindCell = ({ value }: ICellRendererParams<Row, string>) =>
  value ? <span className={`kpill k-${value}`}>{value}</span> : null;

/** Identity rows: whose signature was checked and the result, in one cell. */
const CheckCell = ({ value, data }: ICellRendererParams<Row, string>) => {
  const signer = data?.signer ? actorName(data.signer) : "";
  const who = signer ? <span className={`signer actor-${signer}`}>{signer}</span> : null;
  return value === "verified" ? (
    <span className="check-cell">
      <span className="badge good">
        <Check size={11} strokeWidth={3} aria-hidden="true" /> Verified
      </span>
      {who}
    </span>
  ) : value === "rejected" ? (
    <span className="check-cell">
      <span className="badge bad">
        <X size={11} strokeWidth={3} aria-hidden="true" /> Rejected
      </span>
      {who}
    </span>
  ) : null;
};

const LinkCell = ({ value }: ICellRendererParams<Row, string>) => {
  const href = safeLink(value ?? "");
  return href ? (
    <a className="cell-btn" href={href} target="_blank" rel="noopener noreferrer">
      approve <ExternalLink size={11} aria-hidden="true" />
    </a>
  ) : null;
};

// State rows read "DRAFT → NEGOTIATING" in the event column instead of a mostly empty column of their own.
const COLUMNS: ColDef<Row>[] = [
  {
    field: "ts",
    headerName: "Time",
    width: 92,
    valueGetter: (p) => (p.data?.ts ?? "").slice(11, 21),
    cellClass: "mono dim",
  },
  {
    colId: "actor",
    headerName: "Actor",
    width: 116,
    valueGetter: (p) => actorName(p.data?.actor ?? ""),
    cellClass: (p) => `actor-${p.value}`,
  },
  { field: "kind", headerName: "Kind", width: 100, cellRenderer: KindCell },
  {
    colId: "event",
    headerName: "Event",
    width: 190,
    valueGetter: (p) => (p.data?.state_from ? `${p.data.state_from} → ${p.data.state_to}` : (p.data?.event ?? "")),
    cellClass: (p) => (p.data?.state_from ? "mono cell-arrow" : "mono"),
    tooltipValueGetter: (p) => (p.data?.state_from ? `${p.data.event}: ${p.value}` : undefined),
  },
  {
    field: "check",
    headerName: "Signature",
    width: 150,
    cellRenderer: CheckCell,
    getQuickFilterText: (p) => `${p.value ?? ""} ${p.data?.signer ?? ""}`,
  },
  {
    field: "price",
    headerName: "Amount",
    width: 84,
    type: "rightAligned",
    headerClass: "ag-right-aligned-header",
    cellClass: "cell-price mono",
  },
  { field: "note", headerName: "Note", flex: 1, minWidth: 160, tooltipField: "note" },
  { field: "link", headerName: "Approval", width: 104, cellRenderer: LinkCell },
  { field: "order_id", headerName: "PayPal order", width: 140, cellClass: "mono dim", tooltipField: "order_id" },
  { field: "sha", headerName: "sha256", width: 140, cellClass: "mono dim", tooltipField: "sha" },
];

/**
 * Below these grid widths the order and hash columns, then kind and signature, hide so the note stays readable.
 * Nothing is lost: the row tint shows the kind and the note names whose signature was checked.
 */
const WIDE = 1180;
const NARROW = 820;

const KINDS = ["all", "msg", "identity", "mandate", "paypal", "state", "policy", "receipt", "ruling", "api"] as const;
type KindFilter = (typeof KINDS)[number];

export function LedgerGrid({ rows }: { rows: Row[] }) {
  const gridRef = useRef<AgGridReact<Row>>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const [kind, setKind] = useState<KindFilter>("all");
  const [search, setSearch] = useState("");
  const [follow, setFollow] = useState(true);
  const [shown, setShown] = useState(0);
  const defaultColDef = useMemo<ColDef<Row>>(
    () => ({ sortable: true, resizable: true, suppressHeaderMenuButton: true }),
    [],
  );
  const counts = useMemo(() => {
    const c: Record<string, number> = { all: rows.length };
    rows.forEach((r) => {
      c[r.kind] = (c[r.kind] ?? 0) + 1;
    });
    return c;
  }, [rows]);

  // Which columns fit is decided from the grid's own width, so the columns follow the panel, not the window.
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    setWidth(el.clientWidth);
    const ro = new ResizeObserver(([entry]) => setWidth(Math.round(entry.contentRect.width)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const hasLink = rows.some((r) => r.link);
  // Through the API rather than columnDefs: only setColumnsVisible makes AG Grid re-share the note's flex width.
  const [ready, setReady] = useState(false);
  useEffect(() => {
    const api = gridRef.current?.api;
    if (!ready || !api || !width) return;
    const hidden = new Set<string>();
    if (!hasLink) hidden.add("link");
    if (width < WIDE) ["order_id", "sha"].forEach((c) => hidden.add(c));
    if (width < NARROW) ["kind", "check"].forEach((c) => hidden.add(c));
    const ids = COLUMNS.map((c) => c.colId ?? c.field ?? "");
    api.setColumnsVisible(ids.filter((id) => !hidden.has(id)), true);
    api.setColumnsVisible([...hidden], false);
  }, [ready, width, hasLink]);

  // External filter: AG Grid re-evaluates it only when told the filter state changed.
  useEffect(() => {
    gridRef.current?.api?.onFilterChanged();
  }, [kind]);

  // Follow live: keep the newest displayed row in view while the run streams in.
  useEffect(() => {
    const api = gridRef.current?.api;
    if (!follow || !api) return;
    const last = api.getDisplayedRowCount() - 1;
    if (last >= 0) api.ensureIndexVisible(last, "bottom");
  }, [rows.length, follow, kind, search]);

  return (
    <div className="grid-col">
      <div className="grid-toolbar">
        <div className="chips" role="group" aria-label="filter by kind">
          {KINDS.map((k) => (
            <button
              key={k}
              type="button"
              className={`chip ${kind === k ? "on" : ""}`}
              onClick={() => setKind(k)}
              aria-pressed={kind === k}
            >
              {k}
              <span className="n">{counts[k] ?? 0}</span>
            </button>
          ))}
        </div>
        <input
          className="filter"
          type="search"
          placeholder="Search the ledger"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          spellCheck={false}
          aria-label="search ledger"
        />
        <button
          type="button"
          className={`chip follow ${follow ? "on" : ""}`}
          aria-pressed={follow}
          onClick={() => setFollow((v) => !v)}
        >
          follow live
        </button>
      </div>
      <div className="grid-wrap" ref={wrapRef}>
        <AgGridReact<Row>
          ref={gridRef}
          theme={theme}
          rowData={rows}
          columnDefs={COLUMNS}
          defaultColDef={defaultColDef}
          getRowId={(p) => p.data.id}
          animateRows={false}
          suppressMovableColumns
          tooltipShowDelay={400}
          quickFilterText={search}
          onModelUpdated={(e) => setShown(e.api.getDisplayedRowCount())}
          onGridReady={() => setReady(true)}
          isExternalFilterPresent={() => kind !== "all"}
          doesExternalFilterPass={(node: IRowNode<Row>) => kind === "all" || node.data?.kind === kind}
          rowClassRules={{
            "row-paypal": (p: RowClassParams<Row>) => p.data?.kind === "paypal",
            "row-ruling": (p: RowClassParams<Row>) => p.data?.kind === "ruling" || p.data?.kind === "attest",
            "row-state": (p: RowClassParams<Row>) => p.data?.kind === "state",
            "row-run": (p: RowClassParams<Row>) => p.data?.kind === "run",
            "row-api": (p: RowClassParams<Row>) => p.data?.kind === "api",
            "row-policy-ok": (p: RowClassParams<Row>) => p.data?.kind === "policy" && p.data?.result !== "fail",
            "row-policy-bad": (p: RowClassParams<Row>) => p.data?.kind === "policy" && p.data?.result === "fail",
            "row-receipt": (p: RowClassParams<Row>) => p.data?.kind === "receipt",
            "row-id-ok": (p: RowClassParams<Row>) => p.data?.kind === "identity" && p.data?.check === "verified",
            "row-id-bad": (p: RowClassParams<Row>) => p.data?.kind === "identity" && p.data?.check === "rejected",
          }}
          overlayNoRowsTemplate='<span class="grid-empty">No events yet. Run a deal to fill the tape.</span>'
        />
      </div>
      <div className="grid-foot" aria-live="polite">
        {shown === rows.length ? `${rows.length} rows` : `${shown} of ${rows.length} rows`}
      </div>
    </div>
  );
}
