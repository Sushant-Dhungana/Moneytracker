from app.arthaxai.chat.neplish import detect_response_language_mode


def test_detect_response_language_mode_marks_personal_due_question_as_neplish() -> None:
    assert detect_response_language_mode("maile manish lai kati tirnu cha ?") == "neplish"


def test_detect_response_language_mode_marks_business_top_customers_query_as_neplish() -> None:
    assert detect_response_language_mode("mero top customers") == "neplish"


def test_detect_response_language_mode_keeps_plain_english_as_english() -> None:
    assert detect_response_language_mode("show me my top customers") == "english"
