from psycopg import Connection

from app.repositories.profile_repository import get_profile_summary
from app.services.accounts_service import fetch_accounts
from app.services.ai_business_service import validate_business_profile_ownership
from app.services.ai_business_vector_service import collect_business_live_snapshot
from app.services.profiles_service import fetch_active_profile_state


def _fetch_personal_lightweight_summary(
    conn: Connection, *, user_id: str, profile_id: str | None
) -> dict:
    if not profile_id:
        return {"income_total": 0.0, "expense_total": 0.0, "net_total": 0.0}
    with conn.cursor() as cur:
        cur.execute(
            """
            select
              coalesce(sum(case when tfv.txn_type = 'income' then tfv.amount else 0 end), 0) as income_total,
              coalesce(sum(case when tfv.txn_type = 'expense' then tfv.amount else 0 end), 0) as expense_total,
              coalesce(count(*), 0) as transaction_count
            from public.transaction_feed_view tfv
            where tfv.user_id = %(user_id)s::uuid
              and tfv.profile_id = %(profile_id)s::uuid
              and tfv.txn_type in ('income', 'expense')
            """,
            {"user_id": user_id, "profile_id": profile_id},
        )
        row = cur.fetchone() or {}
    income_total = float(row.get("income_total") or 0)
    expense_total = float(row.get("expense_total") or 0)
    return {
        "income_total": income_total,
        "expense_total": expense_total,
        "net_total": round(income_total - expense_total, 2),
        "transaction_count": int(row.get("transaction_count") or 0),
    }


def fetch_mobile_bootstrap(conn: Connection, *, user_id: str, email: str | None) -> dict:
    state = fetch_active_profile_state(conn, user_id=user_id, email=email)
    profile_summary = get_profile_summary(conn, user_id) or {}
    active_profile_id = state.get("activeProfileId")
    active_profile_type = str(state.get("activeProfileType") or "personal")
    currency_code = str(profile_summary.get("currency_code") or "NPR").upper()
    is_personal_profile_complete = bool(profile_summary.get("profile_completed"))

    has_cash_account = False
    if active_profile_id and active_profile_type == "personal":
        try:
            accounts = fetch_accounts(conn, user_id, active_profile_id)
        except Exception:
            accounts = []
        has_cash_account = any(str(item.get("type") or "") == "cash" for item in accounts)

    if active_profile_id and active_profile_type == "business":
        validate_business_profile_ownership(
            conn,
            user_id=user_id,
            profile_id=str(active_profile_id),
        )
        lightweight_summary = collect_business_live_snapshot(
            conn,
            user_id=user_id,
            profile_id=str(active_profile_id),
        )
    else:
        lightweight_summary = _fetch_personal_lightweight_summary(
            conn,
            user_id=user_id,
            profile_id=str(active_profile_id) if active_profile_id else None,
        )

    return {
        **state,
        "isPersonalProfileComplete": is_personal_profile_complete,
        "hasCashAccount": has_cash_account,
        "currencyCode": currency_code,
        "lightweightSummary": lightweight_summary,
    }
