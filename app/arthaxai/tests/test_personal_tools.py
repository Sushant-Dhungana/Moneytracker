from datetime import date

from app.arthaxai.tools.personal_tools import (
    PersonalDateScope,
    _build_personal_category_breakdown_reply,
    _build_personal_counterparty_reply,
    _build_personal_counterparty_position_reply,
    _build_personal_transaction_history_reply,
    _build_counterparty_position_matches,
    _build_retrieval_matches,
    _extract_personal_query_candidates,
)


def test_extract_personal_query_candidates_ignores_generic_history_words() -> None:
    candidates = _extract_personal_query_candidates("give me my total transaction history")
    assert candidates == []


def test_personal_retrieval_matches_counterparty_name_fuzzily() -> None:
    rows = [
        {
            "txn_type": "expense",
            "amount": 1500,
            "date": "2026-05-08",
            "category_name": "Friends",
            "description": "Paid back lunch share",
            "account_name": "Cash",
            "counterparty_name": "Sita Karki",
        },
        {
            "txn_type": "expense",
            "amount": 300,
            "date": "2026-05-07",
            "category_name": "Food",
            "description": "Snacks",
            "account_name": "Cash",
            "counterparty_name": "Ramesh",
        },
    ]

    matches = _build_retrieval_matches(rows, "show Sita Karki transaction history", limit=8)
    assert matches
    assert matches[0]["counterparty_name"] == "Sita Karki"
    assert matches[0]["match_confidence"] >= 0.9


def test_personal_transaction_history_reply_uses_requested_scope_label() -> None:
    rows = [
        {
            "txn_type": "expense",
            "amount": 200,
            "date": "2025-12-31",
            "category_name": "Food",
            "description": "Dinner",
            "account_name": "Cash",
            "counterparty_name": "Cafe",
        },
        {
            "txn_type": "income",
            "amount": 500,
            "date": "2025-01-02",
            "category_name": "Salary",
            "description": "Salary credit",
            "account_name": "Bank",
            "counterparty_name": "Employer",
        },
    ]

    reply = _build_personal_transaction_history_reply(
        rows,
        query="give me last year transaction history",
        scope=PersonalDateScope(label="last year", start=date(2025, 1, 1), end=date(2025, 12, 31)),
        response_language_mode="english",
    )

    assert reply is not None
    assert "Personal transaction history for last year:" in reply
    assert "2025-01-02 to 2025-12-31" in reply


def test_personal_counterparty_reply_summarizes_paid_amounts() -> None:
    matches = [
        {
            "txn_type": "expense",
            "amount": 1500,
            "date": "2026-05-08",
            "category": "Friends",
            "description": "Paid back lunch share",
            "counterparty_name": "Sita Karki",
        },
        {
            "txn_type": "expense",
            "amount": 500,
            "date": "2026-05-09",
            "category": "Travel",
            "description": "Bus ticket repayment",
            "counterparty_name": "Sita Karki",
        },
    ]

    reply = _build_personal_counterparty_reply(
        "how much should i pay to sita karki",
        matches,
        response_language_mode="english",
    )

    assert reply is not None
    assert "Transactions with Sita Karki:" in reply
    assert "NPR 2,000.00 paid to Sita Karki" in reply
    assert "recorded transaction history with this person" in reply


def test_personal_category_breakdown_reply_supports_expense_categories() -> None:
    reply = _build_personal_category_breakdown_reply(
        "give me expense categories",
        finance_context={
            "category_breakdown": {
                "income_by_category": [],
                "expense_by_category": [
                    {"category": "Food", "amount": 2500, "pct_of_total": 0.5},
                    {"category": "Transport", "amount": 1500, "pct_of_total": 0.3},
                ],
            }
        },
        scope=PersonalDateScope(label="this month", start=date(2026, 5, 1), end=date(2026, 5, 31)),
        response_language_mode="english",
    )

    assert reply is not None
    assert "Expense categories for this month:" in reply
    assert "- Food: NPR 2,500.00 (50.0%)" in reply


def test_personal_retrieval_matches_neplish_due_phrase_to_counterparty() -> None:
    rows = [
        {
            "txn_type": "expense",
            "amount": 1200,
            "date": "2026-05-08",
            "category_name": "Friends",
            "description": "Lunch split",
            "account_name": "Cash",
            "counterparty_name": "Bikash Thapa",
        }
    ]

    matches = _build_retrieval_matches(rows, "bikash thapa le how much tirnu cha", limit=8)
    assert matches
    assert matches[0]["counterparty_name"] == "Bikash Thapa"


def test_personal_counterparty_reply_handles_neplish_due_phrase() -> None:
    matches = [
        {
            "txn_type": "expense",
            "amount": 1200,
            "date": "2026-05-08",
            "category": "Friends",
            "description": "Lunch split",
            "counterparty_name": "Bikash Thapa",
        }
    ]

    reply = _build_personal_counterparty_reply(
        "bikash thapa le how much tirnu cha",
        matches,
        response_language_mode="neplish",
    )

    assert reply is not None
    assert "Bikash Thapa" in reply
    assert "NPR 1,200.00" in reply


def test_personal_counterparty_reply_uses_net_position_for_borrow_query() -> None:
    matches = [
        {
            "txn_type": "income",
            "amount": 3000,
            "date": "2026-05-08",
            "category": "Friends",
            "description": "Borrowed cash",
            "counterparty_name": "Sita Karki",
        },
        {
            "txn_type": "expense",
            "amount": 1000,
            "date": "2026-05-09",
            "category": "Friends",
            "description": "Partial return",
            "counterparty_name": "Sita Karki",
        },
    ]

    reply = _build_personal_counterparty_reply(
        "how much did i borrow from sita karki",
        matches,
        response_language_mode="english",
    )

    assert reply is not None
    assert "received NPR 2,000.00 more from Sita Karki than you have paid" in reply


def test_personal_counterparty_reply_uses_net_position_for_lend_query() -> None:
    matches = [
        {
            "txn_type": "expense",
            "amount": 2500,
            "date": "2026-05-08",
            "category": "Friends",
            "description": "Lent cash",
            "counterparty_name": "Bikash Thapa",
        },
        {
            "txn_type": "income",
            "amount": 500,
            "date": "2026-05-09",
            "category": "Friends",
            "description": "Partial return",
            "counterparty_name": "Bikash Thapa",
        },
    ]

    reply = _build_personal_counterparty_reply(
        "how much did i lend to bikash thapa",
        matches,
        response_language_mode="english",
    )

    assert reply is not None
    assert "paid NPR 2,000.00 more to Bikash Thapa than you have received" in reply


def test_personal_counterparty_reply_counts_loan_and_repayment_flows() -> None:
    matches = [
        {
            "txn_type": "loan_out",
            "amount": 5000,
            "date": "2026-05-08",
            "category": "Friends",
            "description": "Lent cash",
            "counterparty_name": "Bikash Thapa",
        },
        {
            "txn_type": "repayment_in",
            "amount": 1500,
            "date": "2026-05-09",
            "category": "Friends",
            "description": "Paid back part",
            "counterparty_name": "Bikash Thapa",
        },
    ]

    reply = _build_personal_counterparty_reply(
        "how much did i lend to bikash thapa",
        matches,
        response_language_mode="english",
    )

    assert reply is not None
    assert "paid NPR 3,500.00 more to Bikash Thapa than you have received" in reply
    assert "- Total received from Bikash Thapa: NPR 1,500.00" in reply
    assert "- Total paid to Bikash Thapa: NPR 5,000.00" in reply


def test_personal_counterparty_reply_counts_borrow_and_repayment_out_flows() -> None:
    matches = [
        {
            "txn_type": "loan_in",
            "amount": 6000,
            "date": "2026-05-08",
            "category": "Friends",
            "description": "Borrowed cash",
            "counterparty_name": "Sita Karki",
        },
        {
            "txn_type": "repayment_out",
            "amount": 2500,
            "date": "2026-05-09",
            "category": "Friends",
            "description": "Paid back part",
            "counterparty_name": "Sita Karki",
        },
    ]

    reply = _build_personal_counterparty_reply(
        "how much did i borrow from sita karki",
        matches,
        response_language_mode="english",
    )

    assert reply is not None
    assert "received NPR 3,500.00 more from Sita Karki than you have paid" in reply
    assert "- Total received from Sita Karki: NPR 6,000.00" in reply
    assert "- Total paid to Sita Karki: NPR 2,500.00" in reply


def test_counterparty_position_matches_exact_person_name() -> None:
    positions = [
        {
            "name": "Sita Karki",
            "relation_type": "person",
            "receivable": 0,
            "payable": 10500,
            "net_position": -10500,
        },
        {
            "name": "Bikash Thapa",
            "relation_type": "person",
            "receivable": 2000,
            "payable": 0,
            "net_position": 2000,
        },
    ]

    matches = _build_counterparty_position_matches(positions, "sita karki le kati tirnu cha", limit=8)

    assert matches
    assert matches[0]["name"] == "Sita Karki"


def test_personal_counterparty_position_reply_uses_payable_balance() -> None:
    reply = _build_personal_counterparty_position_reply(
        "sita karki le kati tirnu cha",
        [
            {
                "name": "Sita Karki",
                "receivable": 0,
                "payable": 10500,
                "net_position": -10500,
            }
        ],
        response_language_mode="english",
    )

    assert reply is not None
    assert "You owe Sita Karki NPR 10,500.00" in reply
    assert "- Payable: NPR 10,500.00" in reply


def test_personal_counterparty_position_reply_lists_people_i_lent_to() -> None:
    reply = _build_personal_counterparty_position_reply(
        "who are the people that i lend money to",
        [
            {"name": "Bikash Thapa", "receivable": 2000, "payable": 0, "net_position": 2000},
            {"name": "Sita Karki", "receivable": 0, "payable": 10500, "net_position": -10500},
        ],
        response_language_mode="english",
    )

    assert reply is not None
    assert "People you have lent to:" in reply
    assert "- Bikash Thapa: receivable NPR 2,000.00" in reply
    assert "Sita Karki" not in reply


def test_personal_counterparty_position_reply_lists_people_i_borrowed_from() -> None:
    reply = _build_personal_counterparty_position_reply(
        "who did i borrow from",
        [
            {"name": "Bikash Thapa", "receivable": 2000, "payable": 0, "net_position": 2000},
            {"name": "Sita Karki", "receivable": 0, "payable": 10500, "net_position": -10500},
        ],
        response_language_mode="english",
    )

    assert reply is not None
    assert "People you have borrowed from:" in reply
    assert "- Sita Karki: payable NPR 10,500.00" in reply
    assert "Bikash Thapa" not in reply
