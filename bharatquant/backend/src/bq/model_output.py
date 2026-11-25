import json
from decimal import Context, Decimal, InvalidOperation, localcontext
from typing import NoReturn

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry
from referencing.exceptions import Unresolvable

from bq.context import MAX_ITEMS
from bq.task_registry import TaskDefinition

MAX_OUTPUT_BYTES = 1_048_576
DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"
MAX_NUMBER_CHARS = 512
MAX_ADJUSTED_EXPONENT = 308


class ModelOutputError(ValueError):
    pass


def _reject_constant(_: str) -> NoReturn:
    raise ModelOutputError("Model output contains a non-finite number.")


def _integer(value: str) -> int:
    if len(value) > MAX_NUMBER_CHARS:
        raise ModelOutputError("JSON number exceeds the supported precision.")
    return int(value)


def _decimal(value: str) -> int | Decimal:
    if len(value) > MAX_NUMBER_CHARS:
        raise ModelOutputError("JSON number exceeds the supported precision.")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ModelOutputError("JSON number exceeds the supported exponent.") from None
    if abs(number.adjusted()) > MAX_ADJUSTED_EXPONENT:
        raise ModelOutputError("JSON number exceeds the supported exponent.")
    if number == number.to_integral_value():
        return int(number)
    return number


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ModelOutputError("Model output contains duplicate object keys.")
        result[key] = value
    return result


def _check_dialects(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "$schema" and item != DRAFT_2020_12:
                raise ModelOutputError("Task schema uses an unsupported dialect.")
            _check_dialects(item)
    elif isinstance(value, list):
        for item in value:
            _check_dialects(item)


def _check_unicode(value: object) -> None:
    if isinstance(value, str):
        value.encode("utf-8")
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_unicode(key)
            _check_unicode(item)
    elif isinstance(value, list):
        for item in value:
            _check_unicode(item)


def validate_model_output(task: TaskDefinition, output: str) -> object:
    """Parse bounded JSON and validate it against a trusted task's offline schema."""
    if not isinstance(task, TaskDefinition) or not isinstance(output, str):
        raise ModelOutputError("A task definition and JSON output text are required.")
    try:
        if (
            len(output) > MAX_OUTPUT_BYTES
            or len(output.encode("utf-8")) > MAX_OUTPUT_BYTES
        ):
            raise ModelOutputError("Model output exceeds the byte limit.")
        # Cover bounded input digits and exponent differences in decimal remainders.
        with localcontext(Context(prec=2048)):
            schema = json.loads(
                task.output_schema_json, parse_int=_integer, parse_float=_decimal
            )
            _check_dialects(schema)
            Draft202012Validator.check_schema(schema)
            parsed: object = json.loads(
                output,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
                parse_int=_integer,
                parse_float=_decimal,
            )
            _check_unicode(parsed)
            Draft202012Validator(schema, registry=Registry()).validate(parsed)
    except SchemaError:
        raise ModelOutputError("Task output schema is invalid.") from None
    except ValidationError:
        raise ModelOutputError("Model output does not match the task schema.") from None
    except Unresolvable:
        raise ModelOutputError(
            "Task output schema has an unresolvable reference."
        ) from None
    except RecursionError:
        raise ModelOutputError(
            "Model output or task schema is too deeply nested."
        ) from None
    except (
        UnicodeEncodeError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        if isinstance(exc, ModelOutputError):
            raise
        raise ModelOutputError("Model output or task schema is invalid JSON.") from None
    return parsed


def extract_record_citations(
    parsed: object, *, task: TaskDefinition
) -> tuple[tuple[str, str], ...]:
    if not isinstance(task, TaskDefinition) or task.citation_path is None:
        raise ModelOutputError("A task citation path is required.")
    value = parsed
    for key in task.citation_path:
        if not isinstance(value, dict) or key not in value:
            raise ModelOutputError("Model output is missing the citation path.")
        value = value[key]
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_ITEMS:
        raise ModelOutputError("Model output must contain a bounded citation list.")
    citations: list[tuple[str, str]] = []
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != {"source_id", "version_id"}
            or not isinstance(item["source_id"], str)
            or not item["source_id"]
            or not isinstance(item["version_id"], str)
            or not item["version_id"]
        ):
            raise ModelOutputError("Model output has an invalid citation identity.")
        citations.append((item["source_id"], item["version_id"]))
    if len(set(citations)) != len(citations):
        raise ModelOutputError("Model output has duplicate citation identities.")
    return tuple(citations)
