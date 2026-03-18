from psycopg import Connection


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


def get_account_balances(conn: Connection, user_id: str, profile_id: str) -> list[dict]:
    query = """
    select
      account_id::text as account_id,
      account_name,
      account_type,
      opening_balance,
      current_balance
    from public.get_account_balances(%s::uuid, %s::uuid)
    """
    with conn.cursor() as cur:
        cur.execute(query, (user_id, profile_id))
        rows = cur.fetchall() or []

    return rows


def list_accounts(conn: Connection, user_id: str, profile_id: str) -> list[dict]:
    query = """
    select
      a.id::text as id,
      a.user_id::text as user_id,
      a.name,
      a.type,
      a.opening_balance,
      a.opening_date::text as opening_date,
      a.institution_name,
      a.account_number,
      a.allow_overdraft,
      a.overdraft_limit,
      a.is_active,
      a.created_at::text as created_at,
      a.updated_at::text as updated_at
    from public.accounts a
    where a.user_id = %s::uuid
      and a.profile_id = %s::uuid
      and a.is_active = true
    order by a.created_at asc
    """
    with conn.cursor() as cur:
        cur.execute(query, (user_id, profile_id))
        rows = cur.fetchall() or []

    return rows


def create_account_with_opening(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    name: str,
    account_type: str,
    opening_balance: float,
    opening_date: str,
) -> str:
    query = """
    select public.create_account_with_opening(
      %s::uuid,
      %s::text,
      %s::text,
      %s::numeric,
      %s::date,
      %s::uuid
    ) as account_id
    """
    with conn.cursor() as cur:
        cur.execute(
            query,
            (user_id, name, account_type, opening_balance, opening_date, profile_id),
        )
        row = cur.fetchone()
    return str(row["account_id"])


def get_account_by_id(conn: Connection, *, user_id: str, account_id: str) -> dict | None:
    query = """
    select
      a.id::text as id,
      a.user_id::text as user_id,
      a.name,
      a.type,
      a.opening_balance,
      a.opening_date::text as opening_date,
      a.institution_name,
      a.account_number,
      a.allow_overdraft,
      a.overdraft_limit,
      a.is_active,
      a.created_at::text as created_at,
      a.updated_at::text as updated_at
    from public.accounts a
    where a.id = %s::uuid
      and a.user_id = %s::uuid
    limit 1
    """
    with conn.cursor() as cur:
        cur.execute(query, (account_id, user_id))
        return cur.fetchone()


def update_account_name(
    conn: Connection, *, user_id: str, account_id: str, name: str
) -> dict | None:
    query = """
    update public.accounts
    set name = %s::text
    where id = %s::uuid
      and user_id = %s::uuid
    returning
      id::text as id,
      user_id::text as user_id,
      name,
      type,
      opening_balance,
      opening_date::text as opening_date,
      institution_name,
      account_number,
      allow_overdraft,
      overdraft_limit,
      is_active,
      created_at::text as created_at,
      updated_at::text as updated_at
    """
    with conn.cursor() as cur:
        cur.execute(query, (name, account_id, user_id))
        return cur.fetchone()


def update_bank_account_settings(
    conn: Connection,
    *,
    user_id: str,
    account_id: str,
    name: str,
    allow_overdraft: bool,
    overdraft_limit: float,
) -> dict | None:
    query = """
    update public.accounts
    set
      name = %s::text,
      allow_overdraft = %s::boolean,
      overdraft_limit = %s::numeric
    where id = %s::uuid
      and user_id = %s::uuid
      and type = 'bank'
    returning
      id::text as id,
      user_id::text as user_id,
      name,
      type,
      opening_balance,
      opening_date::text as opening_date,
      institution_name,
      account_number,
      allow_overdraft,
      overdraft_limit,
      is_active,
      created_at::text as created_at,
      updated_at::text as updated_at
    """
    with conn.cursor() as cur:
        cur.execute(
            query,
            (
                name,
                allow_overdraft,
                overdraft_limit if allow_overdraft else 0,
                account_id,
                user_id,
            ),
        )
        return cur.fetchone()
