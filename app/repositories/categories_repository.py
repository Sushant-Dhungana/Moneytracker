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


def list_categories(conn: Connection, user_id: str, profile_id: str) -> list[dict]:
    query = """
    select
      c.id::text as id,
      c.user_id::text as user_id,
      c.name,
      c.type,
      c.parent_id::text as parent_id,
      c.icon,
      c.color,
      c.created_at::text as created_at
    from public.categories c
    where c.user_id = %s::uuid
      and c.profile_id = %s::uuid
    order by c.created_at asc
    """
    with conn.cursor() as cur:
        cur.execute(query, (user_id, profile_id))
        return cur.fetchall() or []


def insert_category(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    name: str,
    cat_type: str,
    parent_id: str | None,
    icon: str | None,
    color: str | None,
) -> dict:
    query = """
    insert into public.categories (
      user_id, profile_id, name, type, parent_id, icon, color
    )
    values (
      %s::uuid, %s::uuid, %s::text, %s::text, %s::uuid, %s::text, %s::text
    )
    returning
      id::text as id,
      user_id::text as user_id,
      name,
      type,
      parent_id::text as parent_id,
      icon,
      color,
      created_at::text as created_at
    """
    with conn.cursor() as cur:
        cur.execute(query, (user_id, profile_id, name, cat_type, parent_id, icon, color))
        row = cur.fetchone()
    return row


def remove_category(conn: Connection, *, user_id: str, profile_id: str, category_id: str) -> None:
    query = """
    delete from public.categories
    where id = %s::uuid
      and user_id = %s::uuid
      and profile_id = %s::uuid
    """
    with conn.cursor() as cur:
        cur.execute(query, (category_id, user_id, profile_id))


def upsert_default_categories(conn: Connection, *, user_id: str, profile_id: str) -> None:
    defaults = [
        ("Salary", "income", "account-balance-wallet", "#10B981"),
        ("Business", "income", "business", "#3B82F6"),
        ("Food & Dining", "expense", "restaurant", "#EF4444"),
        ("Transportation", "expense", "directions-car", "#F59E0B"),
        ("Shopping", "expense", "shopping-bag", "#EC4899"),
        ("Bills & Utilities", "expense", "receipt", "#6366F1"),
        ("Entertainment", "expense", "movie", "#8B5CF6"),
        ("Health", "expense", "local-hospital", "#14B8A6"),
    ]
    query = """
    insert into public.categories (user_id, profile_id, name, type, icon, color)
    values (%s::uuid, %s::uuid, %s::text, %s::text, %s::text, %s::text)
    on conflict (profile_id, name, type) do nothing
    """
    with conn.cursor() as cur:
        for name, cat_type, icon, color in defaults:
            cur.execute(query, (user_id, profile_id, name, cat_type, icon, color))

