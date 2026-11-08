import textwrap

import duckdb
import pytest
import yaml

from datacontracts.__main__ import main
from datacontracts.dbt_import import dbt_contract_yaml, import_dbt, map_type
from datacontracts.spec import ContractError, load_contract, parse_contract
from datacontracts.validate import validate


def schema(tmp_path, text, name="schema.yml"):
    path = tmp_path / name
    path.write_text(textwrap.dedent(text))
    return str(path)


def cols(raw):
    return {c["name"]: c for c in raw["columns"]}


def skipped(notes):
    return [n for n in notes if n.startswith("skipped")]


ORDERS = """
    version: 2
    models:
      - name: orders
        columns:
          - name: order_id
            data_type: integer
            data_tests: [not_null, unique]
          - name: status
            data_type: varchar
            data_tests:
              - accepted_values:
                  values: [paid, refunded]
          - name: amount
            data_type: double
"""


def test_not_null_unique_and_accepted_values_become_column_rules(tmp_path):
    raw, notes = import_dbt(schema(tmp_path, ORDERS))
    c = cols(raw)
    assert c["order_id"] == {"name": "order_id", "type": "int", "nullable": False, "unique": True}
    assert c["status"] == {"name": "status", "type": "string", "allowed": ["paid", "refunded"]}
    assert c["amount"] == {"name": "amount", "type": "float"}
    assert skipped(notes) == []


def test_defaults_dataset_version_and_owner(tmp_path):
    raw, _ = import_dbt(schema(tmp_path, ORDERS))
    assert raw["dataset"] == "orders"
    assert raw["version"] == "0.1.0"
    assert raw["owner"] == "TODO-owner@example.com"
    assert raw["allow_extra_columns"] is True
    assert "min_rows" not in raw  # a schema file says nothing about volume


def test_owner_from_model_meta_and_override(tmp_path):
    text = """
        models:
          - name: m
            meta: {owner: data@example.com}
            columns: [{name: a, data_type: int}]
    """
    assert import_dbt(schema(tmp_path, text))[0]["owner"] == "data@example.com"
    assert import_dbt(schema(tmp_path, text), owner="me@example.com")[0]["owner"] == "me@example.com"
    nested = """
        models:
          - name: m
            config: {meta: {owner: cfg@example.com}}
            columns: [{name: a, data_type: int}]
    """
    assert import_dbt(schema(tmp_path, nested))[0]["owner"] == "cfg@example.com"


def test_dataset_override(tmp_path):
    assert import_dbt(schema(tmp_path, ORDERS), dataset="orders_v2")[0]["dataset"] == "orders_v2"


def test_legacy_tests_key_and_arguments_form_are_both_read(tmp_path):
    text = """
        models:
          - name: m
            columns:
              - name: a
                data_type: int
                tests: [not_null]
              - name: s
                data_type: string
                data_tests:
                  - accepted_values:
                      arguments: {values: [x, y]}
    """
    c = cols(import_dbt(schema(tmp_path, text))[0])
    assert c["a"]["nullable"] is False
    assert c["s"]["allowed"] == ["x", "y"]


def test_tests_and_data_tests_on_one_column_are_merged(tmp_path):
    text = """
        models:
          - name: m
            columns:
              - name: a
                data_type: int
                tests: [not_null]
                data_tests: [unique]
    """
    a = cols(import_dbt(schema(tmp_path, text))[0])["a"]
    assert a["nullable"] is False and a["unique"] is True


def test_accepted_range_becomes_min_and_max_with_or_without_package_prefix(tmp_path):
    text = """
        models:
          - name: m
            columns:
              - name: h
                data_type: integer
                data_tests:
                  - dbt_utils.accepted_range: {min_value: 0, max_value: 23}
              - name: d
                data_type: double
                data_tests:
                  - accepted_range: {arguments: {min_value: 0}}
    """
    c = cols(import_dbt(schema(tmp_path, text))[0])
    assert (c["h"]["min"], c["h"]["max"]) == (0, 23)
    assert c["d"]["min"] == 0 and "max" not in c["d"]


def test_strict_range_is_imported_as_inclusive_and_says_so(tmp_path):
    text = """
        models:
          - name: m
            columns:
              - name: d
                data_type: double
                data_tests:
                  - accepted_range: {min_value: 0, max_value: 200, inclusive: false}
    """
    raw, notes = import_dbt(schema(tmp_path, text))
    assert (cols(raw)["d"]["min"], cols(raw)["d"]["max"]) == (0, 200)
    assert any("inclusive" in n and "d" in n for n in notes)
    assert skipped(notes) == []


def test_accepted_range_on_a_string_column_is_skipped(tmp_path):
    text = """
        models:
          - name: m
            columns:
              - name: s
                data_type: varchar
                data_tests:
                  - accepted_range: {min_value: 0}
    """
    raw, notes = import_dbt(schema(tmp_path, text))
    assert "min" not in cols(raw)["s"]
    assert any("accepted_range" in n for n in skipped(notes))


def test_relationships_are_skipped_and_reported_with_their_target(tmp_path):
    text = """
        models:
          - name: m
            columns:
              - name: zone
                data_type: integer
                data_tests:
                  - relationships:
                      arguments: {to: "ref('zones')", field: id}
    """
    raw, notes = import_dbt(schema(tmp_path, text))
    assert cols(raw)["zone"] == {"name": "zone", "type": "int"}
    (n,) = skipped(notes)
    assert "relationships" in n and "ref('zones')" in n and "zone" in n


def test_unknown_tests_are_skipped_and_reported_not_guessed(tmp_path):
    text = """
        models:
          - name: m
            columns:
              - name: a
                data_type: int
                data_tests:
                  - dbt_expectations.expect_column_values_to_be_between: {min_value: 1}
                  - my_custom_test
    """
    raw, notes = import_dbt(schema(tmp_path, text))
    assert cols(raw)["a"] == {"name": "a", "type": "int"}
    assert len(skipped(notes)) == 2


def test_where_filter_is_skipped_unless_it_only_excludes_nulls(tmp_path):
    text = """
        models:
          - name: m
            columns:
              - name: r
                data_type: varchar
                data_tests:
                  - accepted_values:
                      arguments: {values: [a, b]}
                      config: {where: "r IS  NOT NULL"}
              - name: q
                data_type: varchar
                data_tests:
                  - accepted_values:
                      arguments: {values: [a, b]}
                      config: {where: "created_at > '2024-01-01'"}
              - name: u
                data_type: int
                data_tests:
                  - unique:
                      config: {where: "kind = 'x'"}
    """
    raw, notes = import_dbt(schema(tmp_path, text))
    c = cols(raw)
    assert c["r"]["allowed"] == ["a", "b"]  # the contract already ignores nulls in allowed
    assert "allowed" not in c["q"]
    assert "unique" not in c["u"]
    assert len(skipped(notes)) == 2 and all("where" in n for n in skipped(notes))


def test_warn_severity_is_skipped_because_columns_have_no_severity(tmp_path):
    text = """
        models:
          - name: m
            columns:
              - name: a
                data_type: int
                data_tests:
                  - not_null:
                      config: {severity: warn}
                  - unique:
                      severity: warn
    """
    raw, notes = import_dbt(schema(tmp_path, text))
    assert cols(raw)["a"] == {"name": "a", "type": "int"}
    assert len(skipped(notes)) == 2 and all("warn" in n for n in skipped(notes))


def test_error_severity_and_other_config_keys_are_fine(tmp_path):
    text = """
        models:
          - name: m
            columns:
              - name: a
                data_type: int
                data_tests:
                  - not_null:
                      config: {severity: error, store_failures: true}
    """
    raw, notes = import_dbt(schema(tmp_path, text))
    assert cols(raw)["a"]["nullable"] is False
    assert skipped(notes) == []


def test_second_accepted_values_test_is_skipped_not_merged(tmp_path):
    text = """
        models:
          - name: m
            columns:
              - name: s
                data_type: string
                data_tests:
                  - accepted_values: {values: [a, b]}
                  - accepted_values: {values: [c]}
    """
    raw, notes = import_dbt(schema(tmp_path, text))
    assert cols(raw)["s"]["allowed"] == ["a", "b"]
    assert len(skipped(notes)) == 1


def test_model_level_tests_are_reported_as_skipped(tmp_path):
    text = """
        models:
          - name: m
            data_tests:
              - dbt_utils.unique_combination_of_columns:
                  combination_of_columns: [a, b]
            columns:
              - {name: a, data_type: int}
              - {name: b, data_type: int}
    """
    _, notes = import_dbt(schema(tmp_path, text))
    (n,) = skipped(notes)
    assert "model" in n and "unique_combination_of_columns" in n


def test_constraints_primary_key_not_null_unique(tmp_path):
    text = """
        models:
          - name: m
            columns:
              - name: id
                data_type: int
                constraints: [{type: primary_key}]
              - name: a
                data_type: int
                constraints: [{type: not_null}]
              - name: b
                data_type: int
                constraints: [{type: unique}]
              - name: c
                data_type: int
                constraints: [{type: foreign_key, to: "ref('x')"}, {type: check, expression: "c > 0"}]
    """
    raw, notes = import_dbt(schema(tmp_path, text))
    c = cols(raw)
    assert c["id"]["nullable"] is False and c["id"]["unique"] is True
    assert c["a"]["nullable"] is False and "unique" not in c["a"]
    assert c["b"]["unique"] is True and "nullable" not in c["b"]
    assert c["c"] == {"name": "c", "type": "int"}
    assert len(skipped(notes)) == 2


@pytest.mark.parametrize(
    "dbt_type, expected",
    [
        ("varchar", "string"),
        ("VARCHAR(255)", "string"),
        ("text", "string"),
        ("string", "string"),
        ("integer", "int"),
        ("INT", "int"),
        ("bigint", "int"),
        ("smallint", "int"),
        ("double", "float"),
        ("float64", "float"),
        ("numeric(10, 2)", "float"),
        ("decimal(18,3)", "float"),
        ("number", "float"),
        ("boolean", "bool"),
        ("bool", "bool"),
        ("date", "date"),
        ("timestamp", "timestamp"),
        ("timestamp_ntz", "timestamp"),
        ("TIMESTAMP WITH TIME ZONE", "timestamp"),
        ("datetime", "timestamp"),
        ("geography", None),
        ("array<string>", None),
        (None, None),
    ],
)
def test_map_type(dbt_type, expected):
    assert map_type(dbt_type) == expected


def test_column_without_data_type_is_skipped_and_reported(tmp_path):
    text = """
        models:
          - name: m
            columns:
              - name: typed
                data_type: int
              - name: untyped
                data_tests: [not_null]
    """
    raw, notes = import_dbt(schema(tmp_path, text))
    assert list(cols(raw)) == ["typed"]
    (n,) = skipped(notes)
    assert "untyped" in n and "data_type" in n and "--source" in n


def test_untyped_column_takes_its_type_from_a_source_file(tmp_path):
    path = tmp_path / "t.parquet"
    duckdb.connect().execute(f"COPY (SELECT 1::BIGINT AS id, 'x' AS s) TO '{path}' (FORMAT PARQUET)")
    text = """
        models:
          - name: m
            columns:
              - name: id
                data_tests: [not_null, unique]
              - name: s
              - name: gone
    """
    raw, notes = import_dbt(schema(tmp_path, text), source=str(path))
    c = cols(raw)
    assert c["id"] == {"name": "id", "type": "int", "nullable": False, "unique": True}
    assert c["s"]["type"] == "string"
    assert "gone" not in c
    assert any("gone" in n and "not in" in n for n in skipped(notes))


def test_declared_data_type_wins_over_the_source_file(tmp_path):
    path = tmp_path / "t.parquet"
    duckdb.connect().execute(f"COPY (SELECT 1::BIGINT AS id) TO '{path}' (FORMAT PARQUET)")
    text = "models:\n  - name: m\n    columns:\n      - {name: id, data_type: double}\n"
    assert cols(import_dbt(schema(tmp_path, text), source=str(path))[0])["id"]["type"] == "float"


def test_missing_source_file_is_a_file_not_found(tmp_path):
    text = "models:\n  - name: m\n    columns:\n      - {name: id}\n"
    with pytest.raises(FileNotFoundError):
        import_dbt(schema(tmp_path, text), source=str(tmp_path / "nope.parquet"))


def test_a_model_with_no_typed_column_is_an_error(tmp_path):
    text = "models:\n  - name: m\n    columns:\n      - {name: a}\n"
    with pytest.raises(ContractError, match="no column"):
        import_dbt(schema(tmp_path, text))


def test_model_without_columns_is_an_error(tmp_path):
    with pytest.raises(ContractError, match="no column"):
        import_dbt(schema(tmp_path, "models:\n  - name: m\n"))


MANY = """
    models:
      - name: first
        columns: [{name: a, data_type: int}]
      - name: second
        columns: [{name: b, data_type: int}]
"""


def test_several_models_need_a_choice(tmp_path):
    with pytest.raises(ContractError, match="first, second"):
        import_dbt(schema(tmp_path, MANY))
    raw, _ = import_dbt(schema(tmp_path, MANY), model="second")
    assert raw["dataset"] == "second" and list(cols(raw)) == ["b"]


def test_unknown_model_lists_the_known_ones(tmp_path):
    with pytest.raises(ContractError, match=r"nope.*first, second"):
        import_dbt(schema(tmp_path, MANY), model="nope")


@pytest.mark.parametrize(
    "text, msg",
    [
        ("just a string", "mapping"),
        ("version: 2\n", "models"),
        ("models: {a: 1}\n", "models"),
        ("models: []\n", "models"),
        ("models:\n  - columns: []\n", "name"),
        ("models:\n  - name: m\n    columns: [{data_type: int}]\n", "name"),
        ("models:\n  - name: m\n    columns: {a: 1}\n", "columns"),
    ],
)
def test_malformed_schema_files_are_contract_errors(tmp_path, text, msg):
    with pytest.raises(ContractError, match=msg):
        import_dbt(schema(tmp_path, text))


def test_accepted_values_infer_nothing_and_integers_stay_integers(tmp_path):
    text = """
        models:
          - name: m
            columns:
              - name: n
                data_type: integer
                data_tests:
                  - accepted_values: {values: [1, 2, 3]}
    """
    raw, _ = import_dbt(schema(tmp_path, text))
    assert cols(raw)["n"]["allowed"] == [1, 2, 3]


def test_result_parses_and_the_rendered_yaml_round_trips(tmp_path):
    path = schema(tmp_path, ORDERS)
    raw, notes = import_dbt(path)
    parse_contract(raw)
    text = dbt_contract_yaml(raw, path, "orders", notes)
    out = tmp_path / "c.yaml"
    out.write_text(text)
    assert yaml.safe_load(text) == raw
    assert load_contract(out).column("status").allowed == ("paid", "refunded")
    assert text.startswith("# Imported by `datacontracts dbt-import`")
    assert "schema.yml" in text.splitlines()[1] and "orders" in text.splitlines()[1]


def test_rendered_notes_appear_as_comments(tmp_path):
    text = """
        models:
          - name: m
            columns:
              - name: a
                data_type: int
                data_tests: [{relationships: {to: "ref('x')", field: id}}]
    """
    path = schema(tmp_path, text)
    raw, notes = import_dbt(path)
    out = dbt_contract_yaml(raw, path, "m", notes)
    assert any(line.startswith("# - skipped") and "relationships" in line for line in out.splitlines())
    assert yaml.safe_load(out) == raw


def test_string_values_that_look_like_yaml_survive_the_round_trip(tmp_path):
    text = """
        models:
          - name: m
            columns:
              - name: s
                data_type: varchar
                data_tests:
                  - accepted_values: {values: ["yes", "no", "1", "a: b", "#x"]}
    """
    path = schema(tmp_path, text)
    raw, notes = import_dbt(path)
    assert yaml.safe_load(dbt_contract_yaml(raw, path, "m", notes))["columns"][0]["allowed"] == [
        "yes",
        "no",
        "1",
        "a: b",
        "#x",
    ]


def make_parquet(tmp_path, select):
    path = tmp_path / "d.parquet"
    duckdb.connect().execute(f"COPY ({select}) TO '{path}' (FORMAT PARQUET)")
    return str(path)


RULES = """
    models:
      - name: m
        columns:
          - name: id
            data_type: integer
            data_tests: [not_null, unique]
          - name: status
            data_type: varchar
            data_tests:
              - accepted_values: {values: [paid, refunded]}
          - name: hour
            data_type: integer
            data_tests:
              - accepted_range: {min_value: 0, max_value: 23}
"""


def test_imported_contract_passes_clean_data(tmp_path):
    raw, _ = import_dbt(schema(tmp_path, RULES))
    data = make_parquet(
        tmp_path, "SELECT i::INTEGER AS id, 'paid' AS status, (i % 24)::INTEGER AS hour FROM range(50) t(i)"
    )
    assert validate(parse_contract(raw), data).ok


def test_imported_contract_fails_data_that_breaks_each_dbt_test(tmp_path):
    raw, _ = import_dbt(schema(tmp_path, RULES))
    data = make_parquet(
        tmp_path,
        "SELECT * FROM (VALUES (1::INTEGER,'paid',5::INTEGER), (1,'paid',5), (NULL,'paid',5), "
        "(2,'lost',5), (3,'paid',24)) t(id, status, hour)",
    )
    failed = {r.rule for r in validate(parse_contract(raw), data).failures}
    assert failed == {"not_null:id", "unique:id", "allowed:status", "max:hour"}


def test_a_null_in_an_accepted_values_column_passes_like_dbt(tmp_path):
    raw, _ = import_dbt(schema(tmp_path, RULES))
    data = make_parquet(tmp_path, "SELECT 1::INTEGER AS id, NULL::VARCHAR AS status, 1::INTEGER AS hour")
    assert validate(parse_contract(raw), data).ok


def test_cli_writes_the_contract_and_summarizes(tmp_path, capsys):
    path = schema(tmp_path, ORDERS)
    out = tmp_path / "c.yaml"
    assert main(["dbt-import", path, "--out", str(out)]) == 0
    assert load_contract(out).dataset == "orders"
    err = capsys.readouterr().err
    assert "imported" in err and "skipped 0" in err


def test_cli_prints_to_stdout_without_out(tmp_path, capsys):
    path = schema(tmp_path, ORDERS)
    assert main(["dbt-import", path, "--dataset", "x", "--owner", "o@e.com"]) == 0
    printed = yaml.safe_load(capsys.readouterr().out)
    assert printed["dataset"] == "x" and printed["owner"] == "o@e.com"


def test_cli_counts_skipped_tests(tmp_path, capsys):
    text = """
        models:
          - name: m
            columns:
              - name: a
                data_type: int
                data_tests: [not_null, my_custom, {relationships: {to: x, field: y}}]
    """
    assert main(["dbt-import", schema(tmp_path, text)]) == 0
    assert "imported 1 of 3 tests, skipped 2" in capsys.readouterr().err


def test_cli_model_flag_and_errors(tmp_path, capsys):
    path = schema(tmp_path, MANY)
    assert main(["dbt-import", path]) == 2
    assert "first, second" in capsys.readouterr().err
    assert main(["dbt-import", path, "--model", "first"]) == 0
    assert main(["dbt-import", str(tmp_path / "missing.yml")]) == 2
    assert "no such file" in capsys.readouterr().err


def test_cli_source_flag_supplies_types(tmp_path, capsys):
    data = make_parquet(tmp_path, "SELECT 1::BIGINT AS id")
    path = schema(tmp_path, "models:\n  - name: m\n    columns:\n      - {name: id, data_tests: [not_null]}\n")
    assert main(["dbt-import", path, "--source", data]) == 0
    assert yaml.safe_load(capsys.readouterr().out)["columns"][0]["type"] == "int"


@pytest.mark.parametrize("key", ["error_if", "warn_if", "limit", "fail_calc"])
def test_failure_thresholds_make_a_test_unimportable(tmp_path, key):
    text = f"""
        models:
          - name: m
            columns:
              - name: a
                data_type: int
                data_tests:
                  - not_null:
                      config: {{{key}: ">10"}}
    """
    raw, notes = import_dbt(schema(tmp_path, text))
    assert "nullable" not in cols(raw)["a"]
    (n,) = skipped(notes)
    assert "thresholds" in n


def test_second_range_test_is_skipped_not_merged(tmp_path):
    text = """
        models:
          - name: m
            columns:
              - name: a
                data_type: int
                data_tests:
                  - accepted_range: {min_value: 0, max_value: 10}
                  - accepted_range: {min_value: 5}
    """
    raw, notes = import_dbt(schema(tmp_path, text))
    assert (cols(raw)["a"]["min"], cols(raw)["a"]["max"]) == (0, 10)
    assert len(skipped(notes)) == 1


def test_not_null_with_a_null_guard_where_is_a_contradiction_and_is_skipped(tmp_path):
    text = """
        models:
          - name: m
            columns:
              - name: a
                data_type: int
                data_tests:
                  - not_null:
                      config: {where: "a is not null"}
    """
    raw, notes = import_dbt(schema(tmp_path, text))
    assert "nullable" not in cols(raw)["a"]
    assert len(skipped(notes)) == 1


def test_empty_accepted_values_list_is_skipped(tmp_path):
    text = """
        models:
          - name: m
            columns:
              - name: s
                data_type: string
                data_tests:
                  - accepted_values: {values: []}
    """
    raw, notes = import_dbt(schema(tmp_path, text))
    assert "allowed" not in cols(raw)["s"]
    assert len(skipped(notes)) == 1


def test_report_counts_tests_and_constraints_found_and_imported(tmp_path):
    from datacontracts.dbt_import import import_dbt_report

    text = """
        models:
          - name: m
            data_tests: [dbt_utils.recency]
            columns:
              - name: id
                data_type: int
                constraints: [{type: primary_key}, {type: check, expression: "id > 0"}]
                data_tests: [not_null, my_custom]
    """
    r = import_dbt_report(schema(tmp_path, text))
    assert (r.imported, r.found, r.model) == (2, 5, "m")
