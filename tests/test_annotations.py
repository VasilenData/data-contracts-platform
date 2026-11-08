import duckdb

from datacontracts.__main__ import main
from datacontracts.annotations import escape_data, escape_property, github_annotations, locate
from datacontracts.validate import Report, RuleResult

CONTRACT = """dataset: t
version: 1.0.0
owner: o
min_rows: 3
columns:
  - {name: a, type: int, nullable: false, min: 0}
  - {name: b, type: string}
checks:
  - {name: a_small, expr: "a < 10", severity: warn}
"""


def report(*results):
    return Report("t", "1.0.0", 10, 0.1, list(results))


def test_escaping_follows_github_workflow_command_rules():
    assert escape_data("50%\r\nnext") == "50%25%0D%0Anext"
    assert escape_property("a:b,c%\n") == "a%3Ab%2Cc%25%0A"
    # data keeps ':' and ',' literal, properties do not
    assert escape_data("a:b,c") == "a:b,c"


def test_locate_finds_the_yaml_line_of_each_rule_kind():
    assert locate(CONTRACT, "min:a") == 6
    assert locate(CONTRACT, "not_null:a") == 6
    assert locate(CONTRACT, "unique:b") == 7
    assert locate(CONTRACT, "check:a_small") == 9
    assert locate(CONTRACT, "min_rows") == 4
    assert locate(CONTRACT, "nope:zzz") is None


def test_locate_does_not_match_a_longer_column_name():
    text = "columns:\n  - {name: ab, type: int}\n  - {name: a, type: int}\n"
    assert locate(text, "min:a") == 3


def test_passing_rules_produce_no_annotations():
    ok = RuleResult("min:a", "error", 0, 10)
    assert github_annotations(report(ok), "c.yaml", CONTRACT) == []


def test_failed_rule_becomes_error_with_file_line_and_title():
    bad = RuleResult("min:a", "error", 3, 10, 0.0, "value below 0")
    (line,) = github_annotations(report(bad), "contracts/c.yaml", CONTRACT)
    assert line.startswith("::error file=contracts/c.yaml,line=6,title=min%3Aa::")
    # % is escaped as %25 in workflow commands; GitHub decodes it
    assert "3 violations of 10 rows (30.0000%25)" in line
    assert "allowed 0.0000%25" in line
    assert "value below 0" in line


def test_warn_severity_becomes_warning_not_error():
    warn = RuleResult("check:a_small", "warn", 5, 10, 0.0, "a < 10")
    (line,) = github_annotations(report(warn), "c.yaml", CONTRACT)
    assert line.startswith("::warning ")


def test_rule_without_a_yaml_line_omits_line_property():
    bad = RuleResult("nope:zzz", "error", 1, 1, 0.0, "x")
    (line,) = github_annotations(report(bad), "c.yaml", CONTRACT)
    assert line.startswith("::error file=c.yaml,title=nope%3Azzz::")


def test_detail_with_newlines_cannot_inject_a_second_command():
    bad = RuleResult("check:a_small", "error", 1, 1, 0.0, "x\n::error::injected")
    lines = github_annotations(report(bad), "c.yaml", CONTRACT)
    assert len(lines) == 1 and "\n" not in lines[0]


def test_samples_are_not_printed():
    bad = RuleResult("min:a", "error", 1, 1, 0.0, "d", samples=[{"a": -999}])
    (line,) = github_annotations(report(bad), "c.yaml", CONTRACT)
    assert "-999" not in line


def test_one_annotation_per_failed_rule_in_report_order():
    r1 = RuleResult("min:a", "error", 1, 10)
    r2 = RuleResult("not_null:a", "error", 2, 10)
    r3 = RuleResult("min:b", "error", 0, 10)
    lines = github_annotations(report(r1, r3, r2), "c.yaml", CONTRACT)
    assert [x.split("title=")[1].split("::")[0] for x in lines] == ["min%3Aa", "not_null%3Aa"]


def test_cli_format_github_prints_annotations_and_keeps_exit_code(tmp_path, capsys):
    path = tmp_path / "t.parquet"
    con = duckdb.connect()
    con.execute("CREATE TABLE t (a BIGINT, b VARCHAR)")
    con.execute("INSERT INTO t VALUES (1, 'x'), (-2, 'y'), (-3, 'z')")
    con.execute(f"COPY t TO '{path}' (FORMAT PARQUET)")
    c = tmp_path / "c.yaml"
    c.write_text(CONTRACT)
    code = main(["validate", str(c), str(path), "--format", "github"])
    out = capsys.readouterr().out
    assert code == 1
    assert f"::error file={escape_property(str(c))},line=6,title=min%3Aa::" in out
    assert "2 violations of 3 rows" in out
    assert "::warning" not in out  # a < 10 holds for every row


def test_cli_format_github_passes_cleanly(tmp_path, capsys):
    path = tmp_path / "t.parquet"
    con = duckdb.connect()
    con.execute("CREATE TABLE t (a BIGINT, b VARCHAR)")
    con.execute("INSERT INTO t VALUES (1, 'x'), (2, 'y'), (3, 'z')")
    con.execute(f"COPY t TO '{path}' (FORMAT PARQUET)")
    c = tmp_path / "c.yaml"
    c.write_text(CONTRACT)
    assert main(["validate", str(c), str(path), "--format", "github"]) == 0
    assert "::error" not in capsys.readouterr().out


def test_locate_handles_block_style_yaml():
    text = "columns:\n  - name: ab\n    type: int\n  - name: a\n    type: int\n  - name: 'c'\n"
    assert locate(text, "min:a") == 4
    assert locate(text, "min:c") == 6
