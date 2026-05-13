from app.arthaxai.services.accounting_statement_service import (
    ParsedDateScope,
    build_business_accounting_context,
    classify_business_accounting_focus,
    get_personal_to_business_handoff_message,
    is_business_question_for_personal_chat,
)


def test_personal_chat_detects_business_statement_questions() -> None:
    assert is_business_question_for_personal_chat("Show my business balance sheet")
    assert is_business_question_for_personal_chat("What is my company cash flow this month?")


def test_personal_chat_does_not_redirect_normal_personal_questions() -> None:
    assert not is_business_question_for_personal_chat("How much did I spend on food this month?")
    assert not is_business_question_for_personal_chat("Show my personal expense summary")


def test_business_focus_classifier_detects_core_financial_statements() -> None:
    assert classify_business_accounting_focus("Show my balance sheet") == "balance_sheet"
    assert classify_business_accounting_focus("Generate income statement for this month") == "income_statement"
    assert classify_business_accounting_focus("How is cash flow this month?") == "cash_flow"
    assert classify_business_accounting_focus("Give me receivable aging report") == "aging"
    assert classify_business_accounting_focus("Analyze current ratio and net margin") == "ratio_analysis"
    assert classify_business_accounting_focus("Compare this month vs last month expenses") == "trend_analysis"


def test_personal_to_business_handoff_message_is_stable() -> None:
    assert (
        get_personal_to_business_handoff_message()
        == "This is business accounting data. Please switch to Business profile for detailed analysis."
    )


def test_business_accounting_context_includes_general_ledger_activity(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.arthaxai.services.accounting_statement_service.collect_business_live_snapshot",
        lambda conn, *, user_id, profile_id: {
            "receivable_due_total": 0,
            "payable_due_total": 0,
            "customer_due_breakdown": [],
            "supplier_due_breakdown": [],
        },
    )
    monkeypatch.setattr(
        "app.arthaxai.services.accounting_statement_service._fetch_payment_accounts",
        lambda conn, *, user_id, profile_id: [],
    )
    monkeypatch.setattr(
        "app.arthaxai.services.accounting_statement_service._fetch_inventory_snapshot",
        lambda conn, *, user_id, profile_id: {
            "inventory_value_cost": 0,
            "inventory_units_total": 0,
            "product_count": 0,
        },
    )
    monkeypatch.setattr(
        "app.arthaxai.services.accounting_statement_service._build_business_posting_totals",
        lambda conn, *, user_id, profile_id, start_date, end_date: {
            "sales_revenue": 0,
            "other_income": 0,
            "operating_expense": 0,
            "inventory_added": 0,
        },
    )
    monkeypatch.setattr(
        "app.arthaxai.services.accounting_statement_service._fetch_supplier_advance_total",
        lambda conn, *, user_id, profile_id: 0,
    )
    monkeypatch.setattr(
        "app.arthaxai.services.accounting_statement_service._fetch_cash_movements_by_type",
        lambda conn, *, user_id, profile_id, start_date, end_date: [],
    )
    monkeypatch.setattr(
        "app.arthaxai.services.accounting_statement_service._fetch_general_ledger_activity",
        lambda conn, *, user_id, profile_id, start_date, end_date, limit=20: {
            "available": True,
            "scope_summary": {
                "entries_count": 2,
                "gross_amount": 3000.0,
                "first_entry_date": "2026-05-01",
                "last_entry_date": "2026-05-09",
            },
            "transaction_type_totals": [
                {"txn_type": "sale", "entry_count": 1, "total_amount": 2000.0},
                {"txn_type": "expense", "entry_count": 1, "total_amount": 1000.0},
            ],
            "recent_entries": [
                {
                    "entry_id": "entry-1",
                    "date": "2026-05-09",
                    "txn_type": "sale",
                    "description": "Invoice payment",
                    "amount": 2000.0,
                    "debit_total": 2000.0,
                    "credit_total": 2000.0,
                    "leg_types": ["account", "sales_revenue"],
                }
            ],
        },
    )

    context = build_business_accounting_context(
        None,
        user_id="user-1",
        profile_id="profile-1",
        query="show general ledger",
        scope=ParsedDateScope(label="all time", start=None, end=None, all_time=True),
    )

    assert context["general_ledger"]["available"] is True
    assert context["general_ledger"]["scope_summary"]["entries_count"] == 2
    assert context["general_ledger"]["recent_entries"][0]["txn_type"] == "sale"
    assert context["general_ledger"]["transaction_type_totals"][0]["total_amount"] == 2000.0
