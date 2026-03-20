from psycopg import Connection
from psycopg.types.json import Json


def get_active_profile_id(conn: Connection, user_id: str) -> str | None:
    query = """
    select up.active_profile_id::text as active_profile_id
    from public.user_profiles up
    where up.id = %s
    limit 1
    """
    with conn.cursor() as cur:
        cur.execute(query, (user_id,))
        row = cur.fetchone()

    if not row:
        return None
    return row.get("active_profile_id")


def create_income_expense_entry(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    tx_type: str,
    amount: float,
    account_id: str,
    category_id: str | None,
    date_value: str,
    description: str | None,
    attachment_url: str | None,
    metadata: dict,
) -> str:
    fn = "public.post_income_entry" if tx_type == "income" else "public.post_expense_entry"
    query = f"""
    select {fn}(
      %s::uuid,
      %s::uuid,
      %s::numeric,
      %s::date,
      %s::text,
      %s::uuid,
      %s::text,
      %s::jsonb,
      %s::uuid
    ) as entry_id
    """

    with conn.cursor() as cur:
      cur.execute(
          query,
          (
              user_id,
              account_id,
              amount,
              date_value,
              description,
              category_id,
              attachment_url,
              Json(metadata),
              profile_id,
          ),
      )
      row = cur.fetchone()

    return str(row["entry_id"])


def get_entry_created_at(conn: Connection, *, user_id: str, entry_id: str) -> str:
    query = """
    select le.created_at::text as created_at
    from public.ledger_entries le
    where le.id = %s::uuid
      and le.user_id = %s::uuid
    limit 1
    """
    with conn.cursor() as cur:
        cur.execute(query, (entry_id, user_id))
        row = cur.fetchone()

    if not row:
        raise ValueError("Ledger entry was created but cannot be read back.")
    return str(row["created_at"])


def find_income_expense_entry_by_transaction_id(
    conn: Connection, *, user_id: str, transaction_id: str
) -> dict | None:
    query = """
    select
      tfv.ledger_entry_id::text as ledger_entry_id,
      tfv.external_transaction_id::text as external_transaction_id,
      tfv.txn_type,
      tfv.amount,
      tfv.date::text as date,
      tfv.description,
      tfv.category_id::text as category_id,
      tfv.account_id::text as account_id,
      tfv.attachment_url,
      tfv.created_at::text as created_at
    from public.transaction_feed_view tfv
    where tfv.user_id = %s::uuid
      and tfv.external_transaction_id = %s
      and tfv.txn_type in ('income', 'expense')
    limit 1
    """
    with conn.cursor() as cur:
        cur.execute(query, (user_id, transaction_id))
        return cur.fetchone()


def find_ledger_entry_by_transaction_id(
    conn: Connection, *, user_id: str, txn_type: str, transaction_id: str
) -> dict | None:
    query = """
    select
      le.id::text as entry_id,
      le.txn_type,
      le.metadata
    from public.ledger_entries le
    where le.user_id = %s::uuid
      and le.txn_type = %s
      and coalesce(le.metadata->>'transaction_id', '') = %s
    limit 1
    """
    with conn.cursor() as cur:
        cur.execute(query, (user_id, txn_type, transaction_id))
        return cur.fetchone()


def update_income_expense_entry(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    entry_id: str,
    amount: float,
    date_value: str,
    description: str | None,
    category_id: str | None,
    account_id: str,
    attachment_url: str | None,
) -> str:
    query = """
    select public.update_income_expense_entry(
      %s::uuid,
      %s::uuid,
      %s::numeric,
      %s::date,
      %s::text,
      %s::uuid,
      %s::uuid,
      %s::text,
      %s::uuid
    ) as entry_id
    """
    with conn.cursor() as cur:
        cur.execute(
            query,
            (
                user_id,
                entry_id,
                amount,
                date_value,
                description,
                category_id,
                account_id,
                attachment_url,
                profile_id,
            ),
        )
        row = cur.fetchone()
    return str(row["entry_id"])


def reverse_income_expense_entry(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    entry_id: str,
    reason: str,
) -> str:
    query = """
    select public.reverse_income_expense_entry(
      %s::uuid,
      %s::uuid,
      %s::text,
      %s::uuid
    ) as reversal_id
    """
    with conn.cursor() as cur:
        cur.execute(query, (user_id, entry_id, reason, profile_id))
        row = cur.fetchone()
    return str(row["reversal_id"])


def create_transfer_entry(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    from_account_id: str,
    to_account_id: str,
    amount: float,
    date_value: str,
    description: str | None,
    attachment_url: str | None,
    metadata: dict | None,
) -> str:
    query = """
    select public.post_transfer_entry(
      %s::uuid,
      %s::uuid,
      %s::uuid,
      %s::numeric,
      %s::date,
      %s::text,
      %s::text,
      %s::jsonb,
      %s::uuid
    ) as entry_id
    """
    with conn.cursor() as cur:
        cur.execute(
            query,
            (
                user_id,
                from_account_id,
                to_account_id,
                amount,
                date_value,
                description,
                attachment_url,
                Json(metadata or {}),
                profile_id,
            ),
        )
        row = cur.fetchone()
    return str(row["entry_id"])


def create_loan_out_entry(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    counterparty_id: str,
    from_account_id: str,
    amount: float,
    date_value: str,
    description: str | None,
    attachment_url: str | None,
    metadata: dict | None,
) -> str:
    query = """
    select public.post_loan_out_entry(
      %s::uuid,
      %s::uuid,
      %s::uuid,
      %s::numeric,
      %s::date,
      %s::text,
      %s::text,
      %s::jsonb,
      %s::uuid
    ) as entry_id
    """
    with conn.cursor() as cur:
        cur.execute(
            query,
            (
                user_id,
                counterparty_id,
                from_account_id,
                amount,
                date_value,
                description,
                attachment_url,
                Json(metadata or {}),
                profile_id,
            ),
        )
        row = cur.fetchone()
    return str(row["entry_id"])


def create_loan_in_entry(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    counterparty_id: str,
    to_account_id: str,
    amount: float,
    date_value: str,
    description: str | None,
    attachment_url: str | None,
    metadata: dict | None,
) -> str:
    query = """
    select public.post_loan_in_entry(
      %s::uuid,
      %s::uuid,
      %s::uuid,
      %s::numeric,
      %s::date,
      %s::text,
      %s::text,
      %s::jsonb,
      %s::uuid
    ) as entry_id
    """
    with conn.cursor() as cur:
        cur.execute(
            query,
            (
                user_id,
                counterparty_id,
                to_account_id,
                amount,
                date_value,
                description,
                attachment_url,
                Json(metadata or {}),
                profile_id,
            ),
        )
        row = cur.fetchone()
    return str(row["entry_id"])


def create_repayment_in_entry(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    counterparty_id: str,
    to_account_id: str,
    amount: float,
    date_value: str,
    description: str | None,
    attachment_url: str | None,
    metadata: dict | None,
) -> str:
    query = """
    select public.post_repayment_in_entry(
      %s::uuid,
      %s::uuid,
      %s::uuid,
      %s::numeric,
      %s::date,
      %s::text,
      %s::text,
      %s::jsonb,
      %s::uuid
    ) as entry_id
    """
    with conn.cursor() as cur:
        cur.execute(
            query,
            (
                user_id,
                counterparty_id,
                to_account_id,
                amount,
                date_value,
                description,
                attachment_url,
                Json(metadata or {}),
                profile_id,
            ),
        )
        row = cur.fetchone()
    return str(row["entry_id"])


def create_repayment_out_entry(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    counterparty_id: str,
    from_account_id: str,
    amount: float,
    date_value: str,
    description: str | None,
    attachment_url: str | None,
    metadata: dict | None,
) -> str:
    query = """
    select public.post_repayment_out_entry(
      %s::uuid,
      %s::uuid,
      %s::uuid,
      %s::numeric,
      %s::date,
      %s::text,
      %s::text,
      %s::jsonb,
      %s::uuid
    ) as entry_id
    """
    with conn.cursor() as cur:
        cur.execute(
            query,
            (
                user_id,
                counterparty_id,
                from_account_id,
                amount,
                date_value,
                description,
                attachment_url,
                Json(metadata or {}),
                profile_id,
            ),
        )
        row = cur.fetchone()
    return str(row["entry_id"])


def list_account_ledger_rows(conn: Connection, *, user_id: str, account_id: str) -> list[dict]:
    query = """
    select
      e.id::text as entry_id,
      e.txn_type,
      e.date::text as date,
      coalesce(e.description, '') as description,
      e.attachment_url,
      p.amount,
      p.direction,
      e.created_at::text as created_at
    from public.ledger_postings p
    join public.ledger_entries e
      on e.id = p.entry_id
     and e.user_id = p.user_id
    where p.user_id = %s::uuid
      and p.leg_type = 'account'
      and p.ref_id = %s::uuid
    order by e.date desc, e.created_at desc
    """
    with conn.cursor() as cur:
        cur.execute(query, (user_id, account_id))
        return cur.fetchall() or []


def list_ledger_postings(
    conn: Connection,
    *,
    user_id: str,
    entry_id: str | None = None,
    entry_ids: list[str] | None = None,
    leg_type: str | None = None,
    ref_id: str | None = None,
    include_entry: bool = False,
) -> list[dict]:
    select_clause = """
    select
      p.id::text as id,
      p.entry_id::text as entry_id,
      p.user_id::text as user_id,
      p.leg_type,
      p.ref_id::text as ref_id,
      p.direction,
      p.amount,
      p.created_at::text as created_at
    """
    join_clause = ""
    if include_entry:
        select_clause += """,
      e.date::text as entry_date,
      e.created_at::text as entry_created_at,
      e.txn_type as entry_txn_type,
      coalesce(e.description, '') as entry_description,
      e.attachment_url as entry_attachment_url
    """
        join_clause = """
    join public.ledger_entries e
      on e.id = p.entry_id
     and e.user_id = p.user_id
    """

    where_parts = ["p.user_id = %(user_id)s::uuid"]
    bind: dict = {"user_id": user_id}

    if entry_id:
        where_parts.append("p.entry_id = %(entry_id)s::uuid")
        bind["entry_id"] = entry_id
    if entry_ids:
        where_parts.append("p.entry_id = any(%(entry_ids)s::uuid[])")
        bind["entry_ids"] = entry_ids
    if leg_type:
        where_parts.append("p.leg_type = %(leg_type)s")
        bind["leg_type"] = leg_type
    if ref_id is not None:
        if ref_id == "null":
            where_parts.append("p.ref_id is null")
        else:
            where_parts.append("p.ref_id = %(ref_id)s::uuid")
            bind["ref_id"] = ref_id

    query = (
        select_clause
        + "\nfrom public.ledger_postings p\n"
        + join_clause
        + "\nwhere "
        + "\n  and ".join(where_parts)
        + "\norder by p.created_at asc"
    )
    with conn.cursor() as cur:
        cur.execute(query, bind)
        return cur.fetchall() or []
