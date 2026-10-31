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
  backgroundColor: "#0d1015",
  foregroundColor: "#eceff4",
  headerBackgroundColor: "#0d1015",
  headerTextColor: "#a7b0bd",
  borderColor: "#1e2430",
  oddRowBackgroundColor: "#0a0d11",
  fontFamily: "ui-sans-serif, -apple-system, BlinkMacSystemFont, 'Segoe UI', Inter, system-ui, sans-serif",
  fontSize: "12px",
  headerFontSize: "11px",
  headerFontWeight: 600,
  rowHeight: 30,
  headerHeight: 32,
  cellHorizontalPadding: 10,
  accentColor: "#f5b544",
  wrapperBorder: false,
  wrapperBorderRadius: 0,
  rowHoverColor: "rgba(245, 181, 68, 0.07)",
});

const KindCell = ({ value }: ICellRendererParams<Row, string>) =>
  value ? <span className={`kpill k-${value}`}>{value}</span> : null;

const CheckCell = ({ value }: ICellRendererParams<Row, string>) =>
  value === "verified" ? (
    <span className="badge good">
      <Check size={11} strokeWidth={3} aria-hidden="true" /> verified
    </span>
  ) : value === "rejected" ? (
    <span className="badge bad">
      <X size={11} strokeWidth={3} aria-hidden="true" /> rejected
    </span>
  ) : null;

const LinkCell = ({ value }: ICellRendererParams<Row, string>) => {
  const href = safeLink(value ?? "");
  return href ? (
    <a className="cell-btn" href={href} target="_blank" rel="noopener noreferrer">
      approve <ExternalLink size={11} aria-hidden="true" />
    </a>
  ) : null;
};

const COLUMNS: ColDef<Row>[] = [
  {
    field: "ts",
    headerName: "time",
    width: 100,
    valueGetter: (p) => (p.data?.ts ?? "").slice(11, 21),
    cellClass: "mono dim",
  },
  {
    headerName: "actor",
    width: 96,
    valueGetter: (p) => actorName(p.data?.actor ?? ""),
    cellClass: (p) => `actor-${p.value}`,
  },
  { field: "kind", headerName: "kind", width: 92, cellRenderer: KindCell },
  { field: "event", headerName: "event", width: 132, cellClass: "mono" },
  {
    headerName: "signer",
    width: 96,
    valueGetter: (p) => (p.data?.signer ? actorName(p.data.signer) : ""),
    cellClass: (p) => `actor-${p.value}`,
  },
  { field: "check", headerName: "check", width: 104, cellRenderer: CheckCell },
  {
    headerName: "state",
    width: 190,
    valueGetter: (p) => (p.data?.state_from ? `${p.data.state_from} → ${p.data.state_to}` : ""),
    cellClass: "cell-arrow mono",
  },
  {
    field: "price",
    headerName: "amount",
    width: 92,
    type: "rightAligned",
    headerClass: "ag-right-aligned-header",
    cellClass: "cell-price mono",
  },
  { field: "order_id", headerName: "order", width: 130, cellClass: "mono dim", tooltipField: "order_id" },
  { field: "sha", headerName: "sha256", width: 130, cellClass: "mono dim", tooltipField: "sha" },
  { field: "link", headerName: "approval", width: 104, cellRenderer: LinkCell },
  { field: "note", headerName: "note", flex: 1, minWidth: 260, tooltipField: "note" },
];

const KINDS = ["all", "msg", "identity", "mandate", "paypal", "state", "policy", "receipt", "ruling", "api"] as const;
type KindFilter = (typeof KINDS)[number];

export function LedgerGrid({ rows }: { rows: Row[] }) {
  const gridRef = useRef<AgGridReact<Row>>(null);
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
      <div className="grid-wrap">
        <AgGridReact<Row>
          ref={gridRef}
          theme={theme}
          rowData={rows}
          columnDefs={COLUMNS}
          defaultColDef={defaultColDef}
          getRowId={(p) => p.data.id}
          animateRows={false}
          suppressCellFocus
          suppressMovableColumns
          tooltipShowDelay={400}
          quickFilterText={search}
          onModelUpdated={(e) => setShown(e.api.getDisplayedRowCount())}
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
