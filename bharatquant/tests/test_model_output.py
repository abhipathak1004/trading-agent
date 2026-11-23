import socket
from dataclasses import replace
from decimal import Decimal, Subnormal, localcontext
from pathlib import Path
from typing import NoReturn

import pytest
from test_task_registry import definition

from bq.infra import database
from bq.infra.task_registry import TaskRegistry
from bq.model_output import MAX_OUTPUT_BYTES, ModelOutputError, validate_model_output


def test_output_validation_uses_task_schema_and_local_references() -> None:
    task = replace(
        definition(),
        output_schema_json=(
            '{"$defs":{"answer":{"minimum":1,"type":"integer"}},'
            '"additionalProperties":false,"properties":{"answer":{"$ref":"#/$defs/answer"}},'
            '"required":["answer"],"type":"object"}'
        ),
    )
    assert validate_model_output(task, '{"answer":2}') == {"answer": 2}
    for output in ("{}", '{"answer":0}', '{"answer":2,"extra":1}'):
        with pytest.raises(ModelOutputError, match="does not match"):
            validate_model_output(task, output)


@pytest.mark.parametrize(
    "output",
    [
        '{"x":1,"x":2}',
        '{"x":{"y":1,"y":2}}',
        '{"x":NaN}',
        '{"x":Infinity}',
        '{"x":1e9999}',
        '{"x":',
        "\ud800",
        r'{"x":"\ud800"}',
        r'{"\ud800":1}',
        " " * (MAX_OUTPUT_BYTES + 1),
        '"' + "\u00e9" * (MAX_OUTPUT_BYTES // 2) + '"',
        "[" * 1100 + "]" * 1100,
    ],
)
def test_invalid_or_unbounded_output_fails(output: str) -> None:
    with pytest.raises(ModelOutputError):
        validate_model_output(definition(), output)


def test_invalid_schema_and_unsupported_dialect_fail_at_use(tmp_path: Path) -> None:
    invalid = replace(definition(), output_schema_json='{"type":"not-a-type"}')
    path = tmp_path / "bq.db"
    database.migrate(path, create=True)
    registry = TaskRegistry(path)
    registry.save(invalid)
    invalid = registry.load("task", "v1")
    with pytest.raises(ModelOutputError, match="schema is invalid"):
        validate_model_output(invalid, "{}")
    unsupported = replace(
        definition(),
        output_schema_json='{"$schema":"http://json-schema.org/draft-07/schema#"}',
    )
    with pytest.raises(ModelOutputError, match="unsupported dialect"):
        validate_model_output(unsupported, "{}")
    nested = replace(
        definition(),
        output_schema_json='{"$defs":{"x":{"$schema":"http://example.test/other"}}}',
    )
    with pytest.raises(ModelOutputError, match="unsupported dialect"):
        validate_model_output(nested, "{}")


def test_validation_error_does_not_include_model_output() -> None:
    with pytest.raises(ModelOutputError) as error:
        validate_model_output(definition(), '"sensitive-output"')
    assert "sensitive-output" not in str(error.value)


def test_format_is_an_annotation() -> None:
    task = replace(definition(), output_schema_json='{"format":"date","type":"string"}')
    assert validate_model_output(task, '"not-a-date"') == "not-a-date"


def test_numeric_constraints_use_exact_decimal_values() -> None:
    task = replace(
        definition(),
        output_schema_json='{"maximum":9007199254740992,"multipleOf":0.1,"type":"number"}',
    )
    result = validate_model_output(task, "0.3")
    assert isinstance(result, Decimal) and result == Decimal("0.3")
    for output in ("0.31", "9007199254740993.0"):
        with pytest.raises(ModelOutputError, match="does not match"):
            validate_model_output(task, output)
    integer = replace(definition(), output_schema_json='{"type":"integer"}')
    assert validate_model_output(integer, "1.0") == 1
    with pytest.raises(ModelOutputError, match="does not match"):
        validate_model_output(integer, "1.00000000000000001")


def test_numeric_constraints_remain_exact_through_local_references() -> None:
    task = replace(
        definition(),
        output_schema_json=(
            '{"$defs":{"n":{"multipleOf":0.1,"type":"number"}},'
            '"$ref":"#/$defs/n",'
            '"$schema":"https://json-schema.org/draft/2020-12/schema"}'
        ),
    )
    assert validate_model_output(task, "0.3") == Decimal("0.3")
    with pytest.raises(ModelOutputError, match="does not match"):
        validate_model_output(task, "0.30000000000000001")


@pytest.mark.parametrize("output", ["1e309", "1e-309", "1" * 513])
def test_json_numbers_are_bounded(output: str) -> None:
    with pytest.raises(ModelOutputError, match="number"):
        validate_model_output(definition(), output)


def test_decimal_precision_covers_supported_number_boundaries() -> None:
    task = replace(
        definition(), output_schema_json='{"multipleOf":1e-308,"type":"number"}'
    )
    assert validate_model_output(task, "9" * 512) == int("9" * 512)
    assert validate_model_output(task, "1e-308") == Decimal("1e-308")
    with pytest.raises(ModelOutputError, match="does not match"):
        validate_model_output(task, "1.1e-308")


def test_decimal_validation_is_independent_of_caller_context() -> None:
    task = replace(
        definition(), output_schema_json='{"multipleOf":1e-10,"type":"number"}'
    )
    with localcontext() as context:
        context.prec = 1
        context.Emax = 9
        context.Emin = -9
        context.traps[Subnormal] = True
        assert validate_model_output(task, "1e10") == 10**10
        with pytest.raises(ModelOutputError, match="does not match"):
            validate_model_output(task, "1.1e-10")


def test_remote_references_fail_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def network_forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("Network access must not occur.")

    monkeypatch.setattr(socket, "socket", network_forbidden)
    task = replace(
        definition(),
        output_schema_json='{"$ref":"https://example.test/remote-schema"}',
    )
    with pytest.raises(ModelOutputError, match="unresolvable reference"):
        validate_model_output(task, "{}")
