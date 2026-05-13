from app.arthaxai.chat.prompt_builder import build_personal_prompt


def test_build_personal_prompt_includes_retrieved_personal_facts_rules() -> None:
    prompt = build_personal_prompt(
        user_query="how much do i have to pay sita karki",
        recent_history="",
        evidence={
            "normalized_query": "how much do i have to pay sita karki",
            "response_language_mode": "english",
            "counterparty_positions": [
                {"name": "Sita Karki", "receivable": 0, "payable": 10500, "net_position": -10500}
            ],
            "matched_counterparty_positions": [
                {"name": "Sita Karki", "receivable": 0, "payable": 10500, "net_position": -10500}
            ],
            "matched_transactions": [],
            "recent_transactions": [],
            "retrieved_personal_facts": [
                {"source_kind": "counterparty_position", "content": "You owe this person NPR 10500.00."}
            ],
            "finance_context": {},
        },
    )

    assert "evidence.counterparty_positions first" in prompt
    assert "evidence.retrieved_personal_facts is contextual support only" in prompt
    assert "retrieved_personal_facts" in prompt
