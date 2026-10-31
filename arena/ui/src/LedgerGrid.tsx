import { useMemo } from "react";
import { AgGridReact } from "ag-grid-react";
import { themeQuartz, type ColDef, type RowClassParams } from "ag-grid-community";
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

interface Props {
  rows: Row[];
  quickFilter: string;
}

export function LedgerGrid({ rows, quickFilter }: Props) {
  const defaultColDef = useMemo<ColDef<Row>>(
    () => ({ sortable: true, resizable: true, suppressHeaderMenuButton: true }),
    [],
  );

  return (
    <div className="grid-wrap">
      <AgGridReact<Row>
        theme={theme}
        rowData={rows}
        columnDefs={COLUMNS}
        defaultColDef={defaultColDef}
        getRowId={(p) => p.data.id}
        animateRows
        suppressCellFocus
        suppressMovableColumns
        quickFilterText={quickFilter}
        rowClassRules={{
          "row-paypal": (p: RowClassParams<Row>) => p.data?.kind === "paypal",
          "row-ruling": (p: RowClassParams<Row>) => p.data?.kind === "ruling" || p.data?.kind === "attest",
          "row-state": (p: RowClassParams<Row>) => p.data?.kind === "state",
          "row-run": (p: RowClassParams<Row>) => p.data?.kind === "run",
        }}
        overlayNoRowsTemplate='<span style="color:#5c6470;font-size:12px">No events yet — run a deal to fill the tape.</span>'
      />
    </div>
  );
}
