from app.arthaxai.chat.response_formatter import format_business_reply_like_personal


def test_business_formatter_strips_duplicate_ai_titles() -> None:
    reply = """
    ### arthaX Business AI

    arthaX Business AI
    Transaction history for this year:
    - Entries recorded: 12
    Summary: As of 2026-05-09.
    """

    formatted = format_business_reply_like_personal(reply)

    assert formatted.count("arthaX Business AI") <= 1
    assert "Entries recorded: 12" in formatted


def test_business_formatter_strips_trailing_zero_decimals_from_npr_amounts() -> None:
    reply = """
    Sales summary:
    - Total sales: NPR 3,000.00
    Summary: Profit NPR 3,000.00.
    """

    formatted = format_business_reply_like_personal(reply)

    assert "NPR 3,000.00" not in formatted
    assert "NPR 3,000" in formatted


def test_business_formatter_dedupes_repeated_supporting_lines() -> None:
    reply = """
    Stock report:
    - Total units on hand: 12.000
    Supporting details:
    - Total units on hand: 12.000
    - Product count: 3
    Summary: As of 2026-05-09.
    """

    formatted = format_business_reply_like_personal(reply)

    assert formatted.count("Total units on hand: 12.000") == 1


def test_business_formatter_localizes_supporting_labels_for_neplish() -> None:
    reply = """
    Tapai ko business ko summary:
    Yo mahina sales ramro cha.
    - Total sales: NPR 3,000.00
    Keep in mind: Credit collection ajhai baki cha.
    Summary: Tapai ko total sales NPR 3,000.00 cha.
    """

    formatted = format_business_reply_like_personal(reply)

    assert "**Supporting kura**" in formatted
    assert "**Dhyan dinuhos**" in formatted
    assert "**Supporting details**" not in formatted


def test_business_formatter_localizes_supporting_labels_for_nepali() -> None:
    reply = """
    व्यवसाय सारांश:
    यो महिनाको बिक्री राम्रो छ।
    - कुल बिक्री: NPR 3,000.00
    Keep in mind: केही रकम अझै उठ्न बाँकी छ।
    Summary: कुल बिक्री NPR 3,000.00 छ।
    """

    formatted = format_business_reply_like_personal(reply)

    assert "**सहयोगी विवरण**" in formatted
    assert "**ध्यान दिनुहोस्**" in formatted
