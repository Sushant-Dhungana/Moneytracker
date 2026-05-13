from app.arthaxai.services.ai_personal_query_service import parse_personal_query_understanding


def test_parse_personal_balance_lookup_intent() -> None:
    understanding = parse_personal_query_understanding("how much money do i have?")
    assert understanding.intent == "balance_lookup"


def test_parse_personal_balance_lookup_neplish_intent() -> None:
    understanding = parse_personal_query_understanding("mero balance kati cha?")
    assert understanding.intent == "balance_lookup"


def test_parse_personal_category_breakdown_intent_for_expense_categories() -> None:
    understanding = parse_personal_query_understanding("give me expense categories")
    assert understanding.intent == "category_breakdown"


def test_parse_personal_counterparty_due_intent_for_neplish_phrase() -> None:
    understanding = parse_personal_query_understanding("bikash thapa le how much tirnu cha")
    assert understanding.intent == "counterparty_query"


def test_parse_personal_counterparty_lend_borrow_intent() -> None:
    understanding = parse_personal_query_understanding("how much did i borrow from sita karki")
    assert understanding.intent == "counterparty_query"


def test_parse_personal_counterparty_people_list_intent() -> None:
    understanding = parse_personal_query_understanding("who are the people that i lend money to")
    assert understanding.intent == "counterparty_query"
