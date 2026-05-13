from datetime import date

from app.arthaxai.services.ai_business_query_service import (
    _not_found_reply,
    parse_business_query_understanding,
    resolve_party_name_candidates,
    try_generate_deterministic_business_response,
)


def test_parse_this_month_report_neplish() -> None:
    understanding = parse_business_query_understanding(
        "malai yo mahina ko report dekhaunus",
        today=date(2026, 3, 10),
    )
    assert understanding.intent == "monthly_report"
    assert understanding.scope is not None
    assert understanding.scope.label == "this month"
    assert understanding.scope.start == date(2026, 3, 1)
    assert understanding.scope.end == date(2026, 3, 31)
    assert understanding.entity_type_hint in {"party", "unknown"}


def test_parse_last_month_report_with_personal_neplish_aliases() -> None:
    understanding = parse_business_query_understanding(
        "mero pichhlo mahina ko report dekhaunus",
        today=date(2026, 3, 10),
    )
    assert understanding.intent == "monthly_report"
    assert understanding.scope is not None
    assert understanding.scope.label == "last month"
    assert understanding.scope.start == date(2026, 2, 1)
    assert understanding.scope.end == date(2026, 2, 28)


def test_parse_dynamic_product_stock_quantity_intent() -> None:
    understanding = parse_business_query_understanding("wai wai ko stock kati cha?")
    assert understanding.intent == "stock_quantity_lookup"
    assert understanding.entity_type_hint == "product"
    assert "wai wai" in understanding.entity_text_candidates


def test_parse_product_possession_quantity_query_as_stock_lookup() -> None:
    understanding = parse_business_query_understanding("how many tyre do i have")
    assert understanding.intent == "stock_quantity_lookup"
    assert understanding.entity_type_hint == "product"
    assert "tyre" in understanding.entity_text_candidates
    assert understanding.response_language_mode == "english"


def test_parse_neplish_stock_query_marks_neplish_mode() -> None:
    understanding = parse_business_query_understanding("tyre ko stock kati cha")
    assert understanding.response_language_mode == "neplish"


def test_parse_dynamic_product_stock_existence_intent() -> None:
    understanding = parse_business_query_understanding("rice stock ma cha ki chaina?")
    assert understanding.intent == "stock_existence"
    assert understanding.entity_type_hint == "product"


def test_parse_generic_stock_overview_query() -> None:
    understanding = parse_business_query_understanding("what are the product that i have in stock")
    assert understanding.intent == "stock_existence"
    assert understanding.entity_type_hint == "product"
    assert "what are the product that i have in stock" not in understanding.entity_text_candidates


def test_parse_stock_report_query_as_generic_stock_overview() -> None:
    understanding = parse_business_query_understanding("give me stock report")
    assert understanding.intent == "stock_existence"
    assert understanding.entity_type_hint == "product"
    assert understanding.entity_text_candidates == []


def test_parse_dynamic_product_price_and_low_stock_intents() -> None:
    price_understanding = parse_business_query_understanding("soyabean ko price kati cha?")
    low_stock_understanding = parse_business_query_understanding("soyabean low stock ma cha?")

    assert price_understanding.intent == "product_price_lookup"
    assert low_stock_understanding.intent == "low_stock_check"
    assert price_understanding.entity_type_hint == "product"
    assert low_stock_understanding.entity_type_hint == "product"


def test_parse_customer_supplier_purchase_and_due_intents() -> None:
    customer_due = parse_business_query_understanding("Ram Traders ko due kati cha?")
    supplier_due = parse_business_query_understanding("Sharma Suppliers lai kati tirna baki cha?")
    customer_purchase = parse_business_query_understanding("Ram Traders le yo mahina kati purchase garyo?")
    supplier_purchase = parse_business_query_understanding("Sharma Suppliers bata yo mahina kati stock ayo?")
    customer_invoice_due = parse_business_query_understanding("Ram Traders ko invoice baki cha?")

    assert customer_due.intent == "customer_due"
    assert supplier_due.intent == "supplier_due"
    assert customer_purchase.intent == "customer_purchase_total"
    assert supplier_purchase.intent == "supplier_purchase_total"
    assert customer_invoice_due.intent == "customer_invoice_due_lookup"
    assert customer_purchase.scope is not None and customer_purchase.scope.label == "this month"
    assert supplier_purchase.scope is not None and supplier_purchase.scope.label == "this month"


def test_parse_supplier_items_phrase_maps_to_supplier_purchase_total() -> None:
    understanding = parse_business_query_understanding("7lks bata kk saman liye")
    assert understanding.intent == "supplier_purchase_total"
    assert understanding.entity_type_hint in {"supplier", "party"}
    assert "7lks" in understanding.entity_text_candidates


def test_party_transactions_defaults_to_all_time() -> None:
    understanding = parse_business_query_understanding("sushant sanga ko transaction")
    assert understanding.intent == "party_transactions"
    assert understanding.scope is not None
    assert understanding.scope.all_time is True
    assert understanding.scope.label == "all time"


def test_general_ledger_query_does_not_treat_generic_words_as_party_name() -> None:
    understanding = parse_business_query_understanding("show general ledger")
    assert understanding.intent == "party_transactions"
    assert understanding.entity_text_candidates == []


def test_total_transaction_history_query_does_not_treat_total_as_party_name() -> None:
    understanding = parse_business_query_understanding("give me my total transaction history")
    assert understanding.intent == "party_transactions"
    assert understanding.entity_text_candidates == []


def test_generic_transaction_history_falls_back_instead_of_not_found() -> None:
    result = try_generate_deterministic_business_response(
        None,
        user_id="user-1",
        profile_id="profile-1",
        user_query="show transaction history",
    )
    assert result.handled is False
    assert result.route == "llm_fallback"
    assert result.resolution_status == "fallback"


def test_dynamic_fuzzy_customer_resolution_with_typo() -> None:
    understanding = parse_business_query_understanding("Roshni Dhungna ko due kati cha?")
    resolution = resolve_party_name_candidates(
        understanding,
        customers=[
            {"id": "c-1", "name": "Sushant"},
            {"id": "c-2", "name": "Roshni Dhungana"},
        ],
        suppliers=[{"id": "s-1", "name": "ABC Supplier"}],
        products=[],
    )

    assert resolution.status == "resolved"
    assert resolution.selected is not None
    assert resolution.selected.kind == "customer"
    assert resolution.selected.id == "c-2"


def test_dynamic_fuzzy_product_resolution_with_partial_name() -> None:
    understanding = parse_business_query_understanding("soya ko stock kati cha?")
    resolution = resolve_party_name_candidates(
        understanding,
        customers=[],
        suppliers=[],
        products=[
            {"id": "p-1", "name": "Soyabeans"},
            {"id": "p-2", "name": "Rice"},
        ],
    )

    assert resolution.status == "resolved"
    assert resolution.selected is not None
    assert resolution.selected.kind == "product"
    assert resolution.selected.id == "p-1"


def test_dynamic_fuzzy_product_resolution_handles_descriptive_stock_question() -> None:
    understanding = parse_business_query_understanding("how many mineral water do i have in my data")
    resolution = resolve_party_name_candidates(
        understanding,
        customers=[],
        suppliers=[],
        products=[
            {"id": "p-1", "name": "Mineral Water 1L"},
            {"id": "p-2", "name": "Mineral Water 500ml"},
            {"id": "p-3", "name": "Rice"},
        ],
    )

    assert resolution.status == "resolved"
    assert resolution.selected is not None
    assert resolution.selected.kind == "product"
    assert resolution.selected.id == "p-1"


def test_product_family_stock_query_keeps_related_variants() -> None:
    understanding = parse_business_query_understanding("how many tyre do i have")
    resolution = resolve_party_name_candidates(
        understanding,
        customers=[],
        suppliers=[],
        products=[
            {"id": "p-1", "name": "Tyre", "qty_on_hand": 2},
            {"id": "p-2", "name": "Tyre 215", "qty_on_hand": 5},
            {"id": "p-3", "name": "Tyre 235", "qty_on_hand": 1},
            {"id": "p-4", "name": "Tube", "qty_on_hand": 9},
        ],
    )

    assert understanding.intent == "stock_quantity_lookup"
    assert resolution.selected is not None
    assert resolution.selected.kind == "product"


def test_ambiguous_same_name_customer_and_supplier_requires_clarification() -> None:
    understanding = parse_business_query_understanding("sushant ko baki kati cha?")
    resolution = resolve_party_name_candidates(
        understanding,
        customers=[{"id": "c-1", "name": "Sushant"}],
        suppliers=[{"id": "s-1", "name": "Sushant"}],
        products=[],
    )

    assert resolution.status == "ambiguous"
    assert resolution.selected is None
    assert resolution.ambiguous_same_name is True
    assert resolution.clarification_prompt is not None
    assert "customer ho ki supplier ho" in resolution.clarification_prompt


def test_ambiguous_same_name_english_query_returns_english_clarification() -> None:
    understanding = parse_business_query_understanding("How much due for sushant?")
    resolution = resolve_party_name_candidates(
        understanding,
        customers=[{"id": "c-1", "name": "Sushant"}],
        suppliers=[{"id": "s-1", "name": "Sushant"}],
        products=[],
    )

    assert resolution.status == "ambiguous"
    assert resolution.selected is None
    assert resolution.ambiguous_same_name is True
    assert resolution.clarification_prompt is not None
    assert "Please clarify whether you mean customer or supplier." in resolution.clarification_prompt


def test_supplier_items_phrase_resolves_dynamic_supplier_name() -> None:
    understanding = parse_business_query_understanding("7lks bata kk saman liye")
    resolution = resolve_party_name_candidates(
        understanding,
        customers=[],
        suppliers=[
            {"id": "s-1", "name": "7LKS Traders"},
            {"id": "s-2", "name": "ABC Suppliers"},
        ],
        products=[],
    )

    assert resolution.status == "resolved"
    assert resolution.selected is not None
    assert resolution.selected.kind == "supplier"
    assert resolution.selected.id == "s-1"


def test_supplier_product_query_resolves_both_supplier_and_product() -> None:
    understanding = parse_business_query_understanding("how much tyre did i purchase from sushant")
    resolution = resolve_party_name_candidates(
        understanding,
        customers=[],
        suppliers=[{"id": "s-1", "name": "Sushant"}],
        products=[{"id": "p-1", "name": "Tyre"}, {"id": "p-2", "name": "Tube"}],
    )

    assert understanding.intent == "supplier_purchase_total"
    assert resolution.selected is not None
    assert resolution.selected.kind == "supplier"
    assert resolution.selected.id == "s-1"
    assert resolution.product is not None
    assert resolution.product.kind == "product"
    assert resolution.product.id == "p-1"


def test_not_found_entity_returns_not_found_status() -> None:
    understanding = parse_business_query_understanding("unknownitem ko stock kati cha?")
    resolution = resolve_party_name_candidates(
        understanding,
        customers=[],
        suppliers=[],
        products=[
            {"id": "p-1", "name": "Detergent"},
            {"id": "p-2", "name": "Milk"},
        ],
    )

    assert resolution.status == "not_found"
    assert resolution.selected is None


def test_not_found_customer_uses_requested_business_message() -> None:
    understanding = parse_business_query_understanding("ram ko due kati cha?")
    reply = _not_found_reply(understanding, "customer name")
    assert reply == "You don't have ram in your business."


def test_not_found_supplier_uses_requested_business_message() -> None:
    understanding = parse_business_query_understanding("abc supplier lai kati tirna baki cha?")
    reply = _not_found_reply(understanding, "supplier name")
    assert reply == "You don't have abc supplier in your business."
