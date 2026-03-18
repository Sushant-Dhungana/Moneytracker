from datetime import date

from psycopg import Connection


BASE_SELECT = """
select tfv.*
from public.transaction_feed_view tfv
where tfv.user_id = %(user_id)s
"""


BASE_COUNT = """
select count(*) as total
from public.transaction_feed_view tfv
where tfv.user_id = %(user_id)s
"""


def _build_filters(params: dict) -> tuple[str, dict]:
    filters: list[str] = []

    if params.get("profile_id"):
        filters.append("tfv.profile_id = %(profile_id)s::uuid")
    if params.get("txn_type"):
        filters.append("tfv.txn_type = %(txn_type)s")
    if params.get("account_id"):
        filters.append("tfv.account_id = %(account_id)s::uuid")
    if params.get("category_id"):
        filters.append("tfv.category_id = %(category_id)s::uuid")
    if params.get("counterparty_id"):
        filters.append("tfv.counterparty_id = %(counterparty_id)s::uuid")

    start_date: date | None = params.get("start_date")
    end_date: date | None = params.get("end_date")

    if start_date:
        filters.append("tfv.date >= %(start_date)s")
    if end_date:
        filters.append("tfv.date <= %(end_date)s")

    filter_sql = ""
    if filters:
        filter_sql = "\n  and " + "\n  and ".join(filters)

    return filter_sql, params


def list_transaction_feed(conn: Connection, params: dict) -> tuple[list[dict], int]:
    filter_sql, bind = _build_filters(params)

    select_sql = (
        BASE_SELECT
        + filter_sql
        + "\norder by tfv.date desc, tfv.created_at desc\nlimit %(limit)s offset %(offset)s"
    )
    count_sql = BASE_COUNT + filter_sql

    with conn.cursor() as cur:
        cur.execute(select_sql, bind)
        items = cur.fetchall() or []

    with conn.cursor() as cur:
        cur.execute(count_sql, bind)
        total_row = cur.fetchone() or {"total": 0}

    total = int(total_row.get("total", 0))
    return items, total


def find_transaction_feed_item_by_external_id(
    conn: Connection, *, user_id: str, transaction_id: str
) -> dict | None:
    query = """
    select tfv.*
    from public.transaction_feed_view tfv
    where tfv.user_id = %s::uuid
      and tfv.external_transaction_id = %s
      and tfv.txn_type in ('income', 'expense')
    limit 1
    """
    with conn.cursor() as cur:
        cur.execute(query, (user_id, transaction_id))
        return cur.fetchone()
