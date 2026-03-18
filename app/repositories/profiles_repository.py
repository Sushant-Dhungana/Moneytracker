from psycopg import Connection


def list_profiles(conn: Connection, *, user_id: str) -> list[dict]:
    query = """
    select
      p.id as id,
      p.user_id as user_id,
      p.profile_type::text as profile_type,
      p.name,
      p.phone_number,
      p.address,
      p.pan_number,
      p.created_at as created_at,
      p.updated_at as updated_at
    from public.profiles p
    where p.user_id = %s::uuid
    order by case when p.profile_type = 'personal' then 0 else 1 end, p.created_at asc
    """
    with conn.cursor() as cur:
        cur.execute(query, (user_id,))
        return cur.fetchall() or []


def upsert_personal_profile(conn: Connection, *, user_id: str, name: str) -> dict:
    query = """
    insert into public.profiles (user_id, profile_type, name)
    values (%s::uuid, 'personal', %s::text)
    on conflict (user_id, profile_type)
    do update set name = excluded.name
    returning
      id as id,
      user_id as user_id,
      profile_type::text as profile_type,
      name,
      phone_number,
      address,
      pan_number,
      created_at as created_at,
      updated_at as updated_at
    """
    with conn.cursor() as cur:
        cur.execute(query, (user_id, name))
        row = cur.fetchone()
    return row


def get_user_active_profile_id(conn: Connection, *, user_id: str) -> str | None:
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


def set_user_active_profile_id(conn: Connection, *, user_id: str, profile_id: str) -> None:
    query = """
    update public.user_profiles
    set active_profile_id = %s::uuid
    where id = %s::uuid
    """
    with conn.cursor() as cur:
        cur.execute(query, (profile_id, user_id))


def get_business_setup_state(conn: Connection, *, user_id: str) -> dict:
    query = """
    select * from public.get_business_setup_state(%s::uuid)
    """
    with conn.cursor() as cur:
        cur.execute(query, (user_id,))
        row = cur.fetchone() or {}
    return row


def switch_active_profile(conn: Connection, *, user_id: str, target_type: str) -> None:
    query = """
    select public.switch_active_profile(%s::uuid, %s::public.profile_type_enum)
    """
    with conn.cursor() as cur:
        cur.execute(query, (user_id, target_type))


def upsert_business_profile_with_opening(
    conn: Connection,
    *,
    user_id: str,
    name: str,
    phone_number: str | None,
    address: str | None,
    pan_number: str | None,
    opening_balance: float | None,
    opening_date: str,
) -> dict:
    query = """
    select * from public.upsert_business_profile_with_opening(
      p_user_id := %s::uuid,
      p_name := %s::text,
      p_phone_number := %s::text,
      p_address := %s::text,
      p_pan_number := %s::text,
      p_opening_balance := %s::numeric,
      p_opening_date := %s::date
    )
    """
    with conn.cursor() as cur:
        cur.execute(
            query,
            (
                user_id,
                name,
                phone_number,
                address,
                pan_number,
                opening_balance,
                opening_date,
            ),
        )
        return cur.fetchone()
