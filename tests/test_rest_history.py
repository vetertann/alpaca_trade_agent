from agent.host.rest import Rest


def test_historical_option_trades_paginate_and_keep_symbols_separate(monkeypatch):
    rest = Rest.__new__(Rest)
    calls = []
    pages = iter([
        {"trades": {"A": [{"p": 1.0}], "B": [{"p": 2.0}]},
         "next_page_token": "next"},
        {"trades": {"A": [{"p": 1.1}]}, "next_page_token": None},
    ])

    def fake_get(base, path, bucket, **params):
        calls.append((base, path, params))
        return next(pages)

    monkeypatch.setattr(rest, "_get", fake_get)
    rows = rest.option_trades(["A", "B"], "start", "end")
    assert rows == {"A": [{"p": 1.0}, {"p": 1.1}], "B": [{"p": 2.0}]}
    assert calls[0][1] == "/v1beta1/options/trades"
    assert calls[1][2]["page_token"] == "next"


def test_orders_can_request_one_nested_chronological_page(monkeypatch):
    rest = Rest.__new__(Rest)
    observed = {}

    def fake_request(method, path, **params):
        observed.update({"method": method, "path": path, **params})
        return []

    monkeypatch.setattr(rest, "_execution_request", fake_request)
    assert rest.orders("all", nested=True, direction="asc", limit=500) == []
    assert observed == {"method": "GET", "path": "/v2/orders", "status": "all",
                        "nested": True, "direction": "asc", "limit": 500}


def test_complete_contract_catalogue_serves_bounded_scans_without_refetch(monkeypatch):
    rest = Rest.__new__(Rest)
    rest._contract_cache = {}
    calls = []

    def fake_paged(*args, **kwargs):
        calls.append(kwargs)
        return [{"expiration_date": "2026-09-03"},
                {"expiration_date": "2026-09-11"},
                {"expiration_date": "2028-12-15"}]

    monkeypatch.setattr(rest, "_paged", fake_paged)
    complete = rest.contracts("SPY", "2026-09-01", None)
    bounded = rest.contracts("SPY", "2026-09-01", "2026-09-11")

    assert len(complete) == 3
    assert bounded == complete[:2]
    assert len(calls) == 1
