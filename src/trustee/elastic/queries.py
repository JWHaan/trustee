"""The ES|QL the Elastic backend and the Agent Builder tools share. One place, so the docs, the setup script and
the reputation provider cannot drift apart. Parameters use ES|QL named parameters (`?name`), never string
interpolation of user input; only the index name (operator config) is formatted into the text.
"""

from __future__ import annotations

import re

_INDEX_OK = re.compile(r"^[a-z0-9][a-z0-9._*,-]*$")


def _index(index: str) -> str:
    if not _INDEX_OK.match(index):
        raise ValueError(f"unsafe index name {index!r}")
    return index


def reputation_query(index: str, exclude_deal: bool = False) -> str:
    """One row: deals_total, settled, voided, disputed, rulings_lost, median_delivery_s, first_seen, last_seen.
    Parameter: ?agent (and ?exclude_deal when `exclude_deal`)."""
    skip = " AND deal_id != ?exclude_deal" if exclude_deal else ""
    return f"""FROM {_index(index)}
| WHERE (buyer == ?agent OR seller == ?agent){skip}
| EVAL f_settled = CASE(kind == "state" AND state_to == "SETTLED", 1, 0),
       f_voided = CASE(kind == "state" AND state_to == "VOIDED", 1, 0),
       f_disputed = CASE(kind == "state" AND state_to == "DISPUTED", 1, 0),
       f_lost = CASE(kind == "ruling" AND decision == "release" AND seller == ?agent, 1, 0),
       t_escrowed = CASE(kind == "state" AND state_to == "ESCROWED", @timestamp),
       t_delivered = CASE(kind == "state" AND state_to == "DELIVERED" AND seller == ?agent, @timestamp)
| STATS d_settled = MAX(f_settled), d_voided = MAX(f_voided), d_disputed = MAX(f_disputed), d_lost = MAX(f_lost),
        d_escrowed = MIN(t_escrowed), d_delivered = MAX(t_delivered),
        d_first = MIN(@timestamp), d_last = MAX(@timestamp) BY deal_id
| EVAL delivery_s = DATE_DIFF("seconds", d_escrowed, d_delivered)
| STATS deals_total = COUNT(*), settled = SUM(d_settled), voided = SUM(d_voided), disputed = SUM(d_disputed),
        rulings_lost = SUM(d_lost), median_delivery_s = MEDIAN(delivery_s),
        first_seen = MIN(d_first), last_seen = MAX(d_last)"""


_HISTORY_COLUMNS = (
    "@timestamp, actor, kind, event, op, state_from, state_to, decision, clause, ok, override, "
    "buyer, seller, amount, currency, detail"
)


def deal_history_query(index: str) -> str:
    return f"""FROM {_index(index)}
| WHERE deal_id == ?deal_id
| SORT @timestamp ASC
| KEEP {_HISTORY_COLUMNS}
| LIMIT 200"""


def spend_query(index: str) -> str:
    return f"""FROM {_index(index)}
| WHERE kind == "state" AND state_to == "SETTLED" AND buyer == ?agent_id AND @timestamp >= ?since
| STATS settled_deals = COUNT(*), total_spent = SUM(amount), largest = MAX(amount) BY currency"""


def earnings_query(index: str) -> str:
    return f"""FROM {_index(index)}
| WHERE kind == "state" AND state_to == "SETTLED" AND seller == ?agent_id AND @timestamp >= ?since
| STATS settled_deals = COUNT(*), total_received = SUM(amount), largest = MAX(amount) BY currency"""


def void_reasons_query(index: str) -> str:
    return f"""FROM {_index(index)}
| WHERE kind == "state" AND state_to == "VOIDED" AND @timestamp >= ?since
| KEEP @timestamp, deal_id, buyer, seller, amount, currency, detail
| SORT @timestamp DESC
| LIMIT ?limit"""
