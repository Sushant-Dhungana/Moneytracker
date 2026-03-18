from psycopg import Connection


def get_active_profile_id(conn: Connection, user_id: str) -> str | None:
    query = """
    select up.active_profile_id::text as active_profile_id
    from public.user_profiles up
    where up.id = %s::uuid
    limit 1
    """
    with conn.cursor() as cur:
        cur.execute(query, (user_id,))
        row = cur.fetchone()
    if not row:
        return None
    return row.get("active_profile_id")


def list_counterparties(conn: Connection, user_id: str, profile_id: str) -> list[dict]:
    query = """
    select
      c.id::text as id,
      c.user_id::text as user_id,
      c.name,
      c.relation_type,
      c.opening_balance,
      c.opening_date::text as opening_date,
      c.note,
      c.is_active,
      c.created_at::text as created_at,
      c.updated_at::text as updated_at
    from public.counterparties c
    where c.user_id = %s::uuid
      and c.profile_id = %s::uuid
      and c.is_active = true
    order by c.created_at asc
    """
    with conn.cursor() as cur:
        cur.execute(query, (user_id, profile_id))
        return cur.fetchall() or []


def create_counterparty_with_opening(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    name: str,
    relation_type: str,
    opening_balance: float,
    opening_date: str,
    note: str | None,
) -> str:
    query = """
    select public.create_counterparty_with_opening(
      %s::uuid,
      %s::text,
      %s::text,
      %s::numeric,
      %s::date,
      %s::text,
      %s::uuid
    ) as counterparty_id
    """
    with conn.cursor() as cur:
        cur.execute(
            query,
            (
                user_id,
                name,
                relation_type,
                opening_balance,
                opening_date,
                note,
                profile_id,
            ),
        )
        row = cur.fetchone()
    return str(row["counterparty_id"])


def get_counterparty_by_id(conn: Connection, *, user_id: str, counterparty_id: str) -> dict | None:
    query = """
    select
      c.id::text as id,
      c.user_id::text as user_id,
      c.name,
      c.relation_type,
      c.opening_balance,
      c.opening_date::text as opening_date,
      c.note,
      c.is_active,
      c.created_at::text as created_at,
      c.updated_at::text as updated_at
    from public.counterparties c
    where c.id = %s::uuid
      and c.user_id = %s::uuid
    limit 1
    """
    with conn.cursor() as cur:
        cur.execute(query, (counterparty_id, user_id))
        return cur.fetchone()


def list_counterparty_positions(conn: Connection, *, user_id: str, profile_id: str) -> list[dict]:
    query = """
    select
      counterparty_id::text as counterparty_id,
      name,
      relation_type,
      opening_balance,
      receivable,
      payable,
      net_position
    from public.get_counterparty_positions(%s::uuid, %s::uuid)
    """
    with conn.cursor() as cur:
        cur.execute(query, (user_id, profile_id))
        return cur.fetchall() or []

