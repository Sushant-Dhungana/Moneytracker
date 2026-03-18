from psycopg import Connection


def get_profile_summary(conn: Connection, user_id: str) -> dict | None:
    query = """
    select
      up.id::text as user_id,
      up.email,
      up.username,
      up.first_name,
      up.last_name,
      up.country_code,
      up.currency_code,
      up.profile_completed,
      up.avatar_url,
      up.active_profile_id::text as active_profile_id,
      p.profile_type::text as active_profile_type
    from public.user_profiles up
    left join public.profiles p on p.id = up.active_profile_id
    where up.id = %s
    limit 1
    """
    with conn.cursor() as cur:
        cur.execute(query, (user_id,))
        return cur.fetchone()


def update_profile_name(
    conn: Connection, *, user_id: str, first_name: str, last_name: str
) -> dict | None:
    query = """
    update public.user_profiles
    set
      first_name = %s,
      last_name = %s,
      username = trim(%s || ' ' || %s)
    where id = %s
    returning
      trim(first_name || ' ' || last_name) as full_name,
      first_name,
      last_name
    """
    with conn.cursor() as cur:
        cur.execute(query, (first_name, last_name, first_name, last_name, user_id))
        return cur.fetchone()


def complete_profile(
    conn: Connection,
    *,
    user_id: str,
    first_name: str,
    last_name: str,
    country_code: str,
    currency_code: str,
) -> dict | None:
    query = """
    update public.user_profiles
    set
      first_name = %s,
      last_name = %s,
      username = trim(%s || ' ' || %s),
      country_code = %s,
      currency_code = %s,
      profile_completed = true
    where id = %s
    returning
      trim(first_name || ' ' || last_name) as full_name,
      first_name,
      last_name,
      country_code,
      currency_code,
      profile_completed
    """
    with conn.cursor() as cur:
        cur.execute(
            query,
            (
                first_name,
                last_name,
                first_name,
                last_name,
                country_code,
                currency_code,
                user_id,
            ),
        )
        return cur.fetchone()
