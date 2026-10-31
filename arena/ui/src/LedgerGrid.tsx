import { useEffect, useMemo, useRef, useState } from "react";
import { AgGridReact } from "ag-grid-react";
import {
  themeQuartz,
  type ColDef,
  type IRowNode,
  type RowClassParams,
} from "ag-grid-community";
import type { Row } from "./api";
import { actorName } from "./describe";

const theme = themeQuartz.withParams({
  backgroundColor: "#0e1014",
  foregroundColor: "#e9ecf1",
  headerBackgroundColor: "#0e1014",
  headerTextColor: "#8a929f",
  borderColor: "#1d222a",
  oddRowBackgroundColor: "#0b0d10",
  fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
  fontSize: "11px",
  rowHeight: 27,
  headerHeight: 30,
  cellHorizontalPadding: 10,
  accentColor: "#f5b544",
  wrapperBorder: false,
  wrapperBorderRadius: 0,
});

const COLUMNS: ColDef<Row>[] = [
  {
    field: "ts",
    headerName: "time",
    width: 96,
    valueGetter: (p) => (p.data?.ts ?? "").slice(11, 21),
  },
  {
    headerName: "actor",
    width: 88,
    valueGetter: (p) => actorName(p.data?.actor ?? ""),
    cellClass: (p) => `actor-${p.value}`,
  },
  { field: "kind", headerName: "kind", width: 74, cellClass: "cell-kind" },
  { field: "event", headerName: "event", width: 126, cellClass: "cell-kind" },
  {
    headerName: "signer",
    width: 96,
    valueGetter: (p) => (p.data?.signer ? actorName(p.data.signer) : ""),
    cellClass: (p) => `actor-${p.value}`,
  },
  {
    field: "check",
    headerName: "check",
    width: 84,
    cellClass: (p) => (p.value ? `cell-check ${p.value}` : ""),
  },
  {
    headerName: "state",
    width: 182,
    valueGetter: (p) => {
      const d = p.data;
      if (!d?.state_from) return "";
      return `${d.state_from} → ${d.state_to}`;
    },
    cellClass: "cell-arrow",
  },
  { field: "price", headerName: "amount", width: 82, cellClass: "cell-price" },
  { field: "order_id", headerName: "order", width: 104, cellClass: "cell-order", tooltipField: "order_id" },
  { field: "note", headerName: "note", flex: 1, minWidth: 200, tooltipField: "note" },
];

const KINDS = ["all", "msg", "identity", "mandate", "paypal", "state", "ruling", "api"] as const;
type KindFilter = (typeof KINDS)[number];

interface Props {
  rows: Row[];
}

export function LedgerGrid({ rows }: Props) {
  const gridRef = useRef<AgGridReact<Row>>(null);
  const [kind, setKind] = useState<KindFilter>("all");
  const [search, setSearch] = useState("");
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

  return (
    <div className="grid-col">
      <div className="grid-toolbar">
        <div className="chips" role="group" aria-label="filter by kind">
          {KINDS.map((k) => (
            <button
              key={k}
              type="button"
              className={`chip kind-${k} ${kind === k ? "on" : ""}`}
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
          placeholder="search…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          spellCheck={false}
          aria-label="search ledger"
        />
      </div>
      <div className="grid-wrap">
        <AgGridReact<Row>
          ref={gridRef}
          theme={theme}
          rowData={rows}
          columnDefs={COLUMNS}
          defaultColDef={defaultColDef}
          getRowId={(p) => p.data.id}
          animateRows
          suppressCellFocus
          suppressMovableColumns
          quickFilterText={search}
          isExternalFilterPresent={() => kind !== "all"}
          doesExternalFilterPass={(node: IRowNode<Row>) => kind === "all" || node.data?.kind === kind}
          rowClassRules={{
            "row-paypal": (p: RowClassParams<Row>) => p.data?.kind === "paypal",
            "row-ruling": (p: RowClassParams<Row>) => p.data?.kind === "ruling" || p.data?.kind === "attest",
            "row-state": (p: RowClassParams<Row>) => p.data?.kind === "state",
            "row-run": (p: RowClassParams<Row>) => p.data?.kind === "run",
            "row-api": (p: RowClassParams<Row>) => p.data?.kind === "api",
            "row-id-ok": (p: RowClassParams<Row>) => p.data?.kind === "identity" && p.data?.check === "verified",
            "row-id-bad": (p: RowClassParams<Row>) => p.data?.kind === "identity" && p.data?.check === "rejected",
          }}
          overlayNoRowsTemplate='<span style="color:#5c6470;font-size:12px">No events yet — run a deal to fill the tape.</span>'
        />
      </div>
    </div>
  );
}
