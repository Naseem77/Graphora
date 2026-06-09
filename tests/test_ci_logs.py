from app.ci.logs import extract_failed_tests, extract_paths, summarize_failure_logs


def test_extract_failed_tests_from_pytest_output():
    log = """
    FAILED tests/test_billing.py::test_refund_status - AssertionError
    FAILED tests/test_orders.py::TestOrders::test_cancel - ValueError
    """

    assert extract_failed_tests(log) == [
        "tests/test_billing.py::test_refund_status",
        "tests/test_orders.py::TestOrders::test_cancel",
    ]


def test_extract_paths_deduplicates_source_paths():
    log = "Traceback in app/refunds.py then tests/test_billing.py and app/refunds.py"

    assert extract_paths(log) == ["app/refunds.py", "tests/test_billing.py"]


def test_summarize_failure_logs_keeps_error_lines():
    summary = summarize_failure_logs("ok\nERROR app/refunds.py failed\nmore")

    assert "ERROR app/refunds.py failed" in summary
    assert "ok" not in summary
