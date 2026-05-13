from app.arthaxai.tools.business_tools import _is_manage_category_query


def test_manage_category_query_detection_for_business_categories() -> None:
    assert _is_manage_category_query("show me business categories")
    assert _is_manage_category_query("expense categories")
    assert _is_manage_category_query("what categories are available")
