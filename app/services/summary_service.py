from datetime import date, timedelta

from psycopg import Connection

from app.core.errors import ApiError

_SUMMARY_RELATIONS_CACHE: dict[str, object] | None = None


def _first_existing_relation(conn: Connection, candidates: list[str]) -> str | None:
    for relation in candidates:
        with conn.cursor() as cur:
            cur.execute("select to_regclass(%(relation)s) as rel", {"relation": relation})
            row = cur.fetchone() or {}
        if row.get("rel"):
            return relation
    return None


def _validate_business_profile_ownership(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            select id
            from public.profiles
            where id = %(profile_id)s::uuid
              and user_id = %(user_id)s::uuid
              and profile_type = 'business'
            limit 1
            """,
            {"profile_id": profile_id, "user_id": user_id},
        )
        row = cur.fetchone()
    if not row:
        raise ApiError(
            status_code=403,
            code="invalid_business_profile",
            message="Provided profile_id is not an owned business profile.",
        )


def _resolve_date_range(
    *,
    period: str | None,
    from_date: date | None,
    to_date: date | None,
) -> tuple[date | None, date | None, str]:
    normalized = str(period or "all").strip().lower()
    today = date.today()

    if from_date or to_date:
        start = from_date or to_date
        end = to_date or from_date
        if start and end and start > end:
            start, end = end, start
        normalized_with_bounds = (
            normalized if normalized in {"day", "week", "month", "year", "range"} else "range"
        )
        return start, end, normalized_with_bounds

    if normalized in {"all", "all_time"}:
        return None, None, "all"

    if normalized in {"day", "today"}:
        return today, today, "day"

    if normalized == "week":
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)
        return start, end, "week"

    if normalized == "month":
        start = today.replace(day=1)
        if start.month == 12:
            next_month = start.replace(year=start.year + 1, month=1, day=1)
        else:
            next_month = start.replace(month=start.month + 1, day=1)
        end = next_month - timedelta(days=1)
        return start, end, "month"

    if normalized == "year":
        return date(today.year, 1, 1), date(today.year, 12, 31), "year"

    if normalized == "range":
        if not from_date:
            raise ApiError(
                status_code=400,
                code="missing_from_date",
                message="from date is required when period=range.",
            )
        end = to_date or from_date
        if from_date > end:
            from_date, end = end, from_date
        return from_date, end, "range"

    raise ApiError(
        status_code=400,
        code="invalid_period",
        message="period must be one of all, day, week, month, year, or range.",
    )


def fetch_personal_summary(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    period: str | None,
    from_date: date | None,
    to_date: date | None,
) -> dict:
    resolved_from, resolved_to, normalized_period = _resolve_date_range(
        period=period,
        from_date=from_date,
        to_date=to_date,
    )

    where_parts = [
        "tfv.user_id = %(user_id)s::uuid",
        "tfv.profile_id = %(profile_id)s::uuid",
        "tfv.txn_type in ('income', 'expense')",
    ]
    bind = {"user_id": user_id, "profile_id": profile_id}
    if resolved_from:
        where_parts.append("tfv.date >= %(from_date)s")
        bind["from_date"] = resolved_from
    if resolved_to:
        where_parts.append("tfv.date <= %(to_date)s")
        bind["to_date"] = resolved_to

    with conn.cursor() as cur:
        cur.execute(
            f"""
            select
              coalesce(sum(case when tfv.txn_type = 'income' then tfv.amount else 0 end), 0) as income,
              coalesce(sum(case when tfv.txn_type = 'expense' then tfv.amount else 0 end), 0) as expense,
              coalesce(count(*), 0) as transaction_count
            from public.transaction_feed_view tfv
            where {' and '.join(where_parts)}
            """,
            bind,
        )
        row = cur.fetchone() or {}

    income = float(row.get("income") or 0)
    expense = float(row.get("expense") or 0)
    return {
        "profile_id": profile_id,
        "period": normalized_period,
        "from_date": resolved_from.isoformat() if resolved_from else None,
        "to_date": resolved_to.isoformat() if resolved_to else None,
        "income": income,
        "expense": expense,
        "net": round(income - expense, 2),
        "transaction_count": int(row.get("transaction_count") or 0),
    }


def compile_business_summary_payload(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    period: str | None,
    from_date: date | None,
    to_date: date | None,
    include_due_snapshot: bool = False,
    emit_diagnostics: bool = False,
    validate_profile: bool = True,
) -> dict:
    if validate_profile:
        _validate_business_profile_ownership(conn, user_id=user_id, profile_id=profile_id)
    resolved_from, resolved_to, normalized_period = _resolve_date_range(
        period=period,
        from_date=from_date,
        to_date=to_date,
    )

    income = 0.0
    expense = 0.0
    sales = 0.0
    relations = _resolve_business_summary_relations(conn)
    relation_pairs = list(relations["relation_pairs"])
    invoices_relation = str(relations["invoices_relation"] or "") or None

    for postings_relation, entries_relation in relation_pairs:
        date_filter = ""
        bind = {"user_id": user_id, "profile_id": profile_id}
        if resolved_from:
            date_filter += " and le.date >= %(from_date)s"
            bind["from_date"] = resolved_from
        if resolved_to:
            date_filter += " and le.date <= %(to_date)s"
            bind["to_date"] = resolved_to

        with conn.cursor() as cur:
            cur.execute(
                f"""
                select
                  coalesce(
                    sum(
                      case
                        when lp.leg_type in ('sales_revenue', 'income_bucket')
                          and lp.direction = 'credit' then lp.amount
                        when lp.leg_type = 'receivable'
                          and lp.direction = 'credit'
                          and le.txn_type = 'receivable_collection' then lp.amount
                        else 0
                      end
                    ),
                    0
                  ) as income_total,
                  coalesce(
                    sum(
                      case
                        when lp.leg_type = 'expense_bucket' and lp.direction = 'debit' then lp.amount
                        when lp.leg_type = 'inventory_asset'
                          and lp.direction = 'debit'
                          and le.txn_type in ('inventory_in', 'stock_in', 'purchase') then lp.amount
                        else 0
                      end
                    ),
                    0
                  ) as expense_total
                from {postings_relation} lp
                join {entries_relation} le on le.id = lp.entry_id
                where lp.user_id = %(user_id)s::uuid
                  and lp.profile_id = %(profile_id)s::uuid
                  {date_filter}
                """,
                bind,
            )
            row = cur.fetchone() or {}
            income += float(row.get("income_total") or 0)
            expense += float(row.get("expense_total") or 0)

    if invoices_relation:
        bind = {"user_id": user_id, "profile_id": profile_id}
        where_parts = [
            "i.user_id = %(user_id)s::uuid",
            "i.profile_id = %(profile_id)s::uuid",
        ]
        if resolved_from:
            where_parts.append("i.date >= %(from_date)s")
            bind["from_date"] = resolved_from
        if resolved_to:
            where_parts.append("i.date <= %(to_date)s")
            bind["to_date"] = resolved_to

        with conn.cursor() as cur:
            cur.execute(
                f"""
                select
                  coalesce(sum(coalesce(i.total, 0)), 0) as sales_total
                from {invoices_relation} i
                where {' and '.join(where_parts)}
                """,
                bind,
            )
            row = cur.fetchone() or {}
            sales = float(row.get("sales_total") or 0)

    if emit_diagnostics and __debug__:
        print(
            "[Perf] business.summary.source=live_ledger",
            {
                "profile_id": profile_id,
                "period": normalized_period,
                "from_date": resolved_from.isoformat() if resolved_from else None,
                "to_date": resolved_to.isoformat() if resolved_to else None,
            },
        )

    if emit_diagnostics and _business_daily_summary_exists(conn):
        try:
            bind = {"profile_id": profile_id}
            where_parts = ["profile_id = %(profile_id)s::uuid"]
            if resolved_from:
                where_parts.append("summary_date >= %(from_date)s")
                bind["from_date"] = resolved_from
            if resolved_to:
                where_parts.append("summary_date <= %(to_date)s")
                bind["to_date"] = resolved_to
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    select
                      coalesce(sum(sales), 0) as sales_total,
                      coalesce(sum(income), 0) as income_total,
                      coalesce(sum(expense), 0) as expense_total
                    from public.business_daily_summary
                    where {' and '.join(where_parts)}
                    """,
                    bind,
                )
                row = cur.fetchone() or {}
            daily_sales = float(row.get("sales_total") or 0)
            daily_income = float(row.get("income_total") or 0)
            daily_expense = float(row.get("expense_total") or 0)
            delta = max(
                abs(daily_sales - sales),
                abs(daily_income - income),
                abs(daily_expense - expense),
            )
            if delta > 0.01:
                print(
                    "[Perf] business.summary.daily_mismatch",
                    {
                        "profile_id": profile_id,
                        "period": normalized_period,
                        "live": {
                            "sales": sales,
                            "income": income,
                            "expense": expense,
                        },
                        "daily": {
                            "sales": daily_sales,
                            "income": daily_income,
                            "expense": daily_expense,
                        },
                    },
                )
        except Exception as exc:
            print(f"[Perf] business.summary.daily_compare_failed: {exc}")

    receivable_due = 0.0
    payable_due = 0.0
    if include_due_snapshot:
        from app.arthaxai.services.ai_business_vector_service import collect_business_live_snapshot

        snapshot = collect_business_live_snapshot(
            conn,
            user_id=user_id,
            profile_id=profile_id,
        )
        receivable_due = float(snapshot.get("receivable_due_total") or 0)
        payable_due = float(snapshot.get("payable_due_total") or 0)

    return {
        "profile_id": profile_id,
        "period": normalized_period,
        "from_date": resolved_from.isoformat() if resolved_from else None,
        "to_date": resolved_to.isoformat() if resolved_to else None,
        "sales": sales,
        "income": income,
        "expense": expense,
        "net": round(income - expense, 2),
        "receivable_due": receivable_due,
        "payable_due": payable_due,
    }


def fetch_business_summary(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
    period: str | None,
    from_date: date | None,
    to_date: date | None,
    include_due_snapshot: bool = False,
) -> dict:
    return compile_business_summary_payload(
        conn,
        user_id=user_id,
        profile_id=profile_id,
        period=period,
        from_date=from_date,
        to_date=to_date,
        include_due_snapshot=include_due_snapshot,
        emit_diagnostics=True,
        validate_profile=True,
    )


def fetch_business_due_summary(
    conn: Connection,
    *,
    user_id: str,
    profile_id: str,
) -> dict:
    _validate_business_profile_ownership(conn, user_id=user_id, profile_id=profile_id)
    from app.arthaxai.services.ai_business_vector_service import collect_business_live_snapshot

    snapshot = collect_business_live_snapshot(
        conn,
        user_id=user_id,
        profile_id=profile_id,
    )
    return {
        "profile_id": profile_id,
        "receivable_due_total": float(snapshot.get("receivable_due_total") or 0),
        "payable_due_total": float(snapshot.get("payable_due_total") or 0),
        "outstanding_invoice_count": int(snapshot.get("outstanding_invoice_count") or 0),
        "outstanding_invoice_due_total": float(snapshot.get("outstanding_invoice_due_total") or 0),
        "customer_due_count": int(snapshot.get("customer_due_count") or 0),
        "supplier_due_count": int(snapshot.get("supplier_due_count") or 0),
        "customer_due_breakdown": list(snapshot.get("customer_due_breakdown") or []),
        "supplier_due_breakdown": list(snapshot.get("supplier_due_breakdown") or []),
    }


def _business_daily_summary_exists(conn: Connection) -> bool:
    with conn.cursor() as cur:
        cur.execute("select to_regclass('public.business_daily_summary') as rel")
        row = cur.fetchone() or {}
    return bool(row.get("rel"))


def _resolve_business_summary_relations(conn: Connection) -> dict[str, object]:
    global _SUMMARY_RELATIONS_CACHE

    if _SUMMARY_RELATIONS_CACHE is not None:
        return _SUMMARY_RELATIONS_CACHE

    relation_pairs: list[tuple[str, str]] = []
    for postings_candidate, entries_candidate in [
        ("business.ledger_postings", "business.ledger_entries"),
        ("public.ledger_postings", "public.ledger_entries"),
    ]:
        postings_relation = _first_existing_relation(conn, [postings_candidate])
        entries_relation = _first_existing_relation(conn, [entries_candidate])
        if not postings_relation or not entries_relation:
            continue
        if any(
            existing_postings == postings_relation and existing_entries == entries_relation
            for existing_postings, existing_entries in relation_pairs
        ):
            continue
        relation_pairs.append((postings_relation, entries_relation))

    _SUMMARY_RELATIONS_CACHE = {
        "relation_pairs": tuple(relation_pairs),
        "invoices_relation": _first_existing_relation(
            conn, ["business.invoices", "public.invoices"]
        ),
    }
    return _SUMMARY_RELATIONS_CACHE
