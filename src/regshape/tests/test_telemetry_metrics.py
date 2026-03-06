#!/usr/bin/env python3

"""Tests for PerformanceMetrics dataclass."""

from regshape.libs.decorators.metrics import PerformanceMetrics


class TestPerformanceMetricsRecordRequest:
    """Tests for PerformanceMetrics.record_request()."""

    def test_single_request_increments_count(self):
        m = PerformanceMetrics()
        m.record_request(status_code=200)
        assert m.total_requests == 1

    def test_single_request_records_bytes(self):
        m = PerformanceMetrics()
        m.record_request(status_code=200, bytes_sent=100, bytes_received=500)
        assert m.total_bytes_sent == 100
        assert m.total_bytes_received == 500

    def test_single_request_records_elapsed(self):
        m = PerformanceMetrics()
        m.record_request(status_code=200, elapsed=0.123)
        assert abs(m.total_elapsed - 0.123) < 1e-9

    def test_single_request_records_status_code(self):
        m = PerformanceMetrics()
        m.record_request(status_code=200)
        assert m.status_code_counts == {200: 1}

    def test_multiple_requests_accumulate(self):
        m = PerformanceMetrics()
        m.record_request(status_code=200, bytes_sent=100, bytes_received=500, elapsed=0.1)
        m.record_request(status_code=200, bytes_sent=200, bytes_received=300, elapsed=0.2)
        m.record_request(status_code=404, bytes_sent=0, bytes_received=50, elapsed=0.05)
        assert m.total_requests == 3
        assert m.total_bytes_sent == 300
        assert m.total_bytes_received == 850
        assert abs(m.total_elapsed - 0.35) < 1e-9
        assert m.status_code_counts == {200: 2, 404: 1}

    def test_error_counted_for_4xx(self):
        m = PerformanceMetrics()
        m.record_request(status_code=400)
        assert m.errors == 1

    def test_error_counted_for_5xx(self):
        m = PerformanceMetrics()
        m.record_request(status_code=500)
        assert m.errors == 1

    def test_no_error_for_2xx(self):
        m = PerformanceMetrics()
        m.record_request(status_code=200)
        assert m.errors == 0

    def test_no_error_for_3xx(self):
        m = PerformanceMetrics()
        m.record_request(status_code=301)
        assert m.errors == 0

    def test_retry_flag(self):
        m = PerformanceMetrics()
        m.record_request(status_code=200, is_retry=True)
        assert m.retries == 1
        assert m.total_requests == 1

    def test_retry_not_counted_by_default(self):
        m = PerformanceMetrics()
        m.record_request(status_code=200)
        assert m.retries == 0

    def test_fresh_instance_is_zeroed(self):
        m = PerformanceMetrics()
        assert m.total_requests == 0
        assert m.total_bytes_sent == 0
        assert m.total_bytes_received == 0
        assert m.retries == 0
        assert m.errors == 0
        assert m.status_code_counts == {}
        assert m.total_elapsed == 0.0
