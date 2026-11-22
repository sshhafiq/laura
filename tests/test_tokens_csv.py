"""Token-usage CSV import: validation, partial success, error lines."""

from laura.ingest.tokens_csv import import_token_csv

HEADER = "date,engineer,tool,model,input_tokens,output_tokens,cost_usd"


def test_import_valid_rows(store, org):
    text = (HEADER + "\n"
            "2026-11-01,ada,claude-code,claude-sonnet-4.5,1000,300,0.42\n"
            "2026-11-01,ben,cursor,cursor-default,500,100,0.10\n")
    result = import_token_csv(store, org.id, text)
    assert result.ok and result.imported == 2
    rows = store.list_token_usage(org.id)
    assert rows[0].engineer_login == "ada" and rows[0].total_tokens == 1300
    assert rows[1].tool == "cursor"


def test_import_missing_columns(store, org):
    result = import_token_csv(store, org.id, "date,engineer\n2026-11-01,ada\n")
    assert not result.ok and result.imported == 0
    assert "missing required columns" in result.errors[0]


def test_import_bad_rows_skipped_with_line_numbers(store, org):
    text = (HEADER + "\n"
            "2026-11-01,ada,cursor,m,100,50,0.10\n"      # line 2 ok
            "not-a-date,ada,cursor,m,100,50,0.10\n"      # line 3 bad date
            "2026-11-01,,cursor,m,100,50,0.10\n"         # line 4 no engineer
            "2026-11-01,ada,cursor,m,-5,50,0.10\n"       # line 5 negative
            "2026-11-01,ada,cursor,m,abc,50,0.10\n")     # line 6 non-int
    result = import_token_csv(store, org.id, text)
    assert result.imported == 1
    assert len(result.errors) == 4
    assert all(e.startswith("line ") for e in result.errors)
    assert result.errors[0].startswith("line 3")
