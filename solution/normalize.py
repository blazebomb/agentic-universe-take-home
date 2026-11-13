import csv
import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
SCHEMA_DIR = Path(__file__).resolve().parents[1] / "assignment-a-data" / "schemas"
MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()


def load_schemas():
    schemas = {}
    for path in sorted(SCHEMA_DIR.glob("*.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        schemas[schema["workflow_id"]] = schema["fields"]
    return schemas


def load_dispositions():
    with (SCHEMA_DIR / "dispositions.csv").open(encoding="utf-8", newline="") as source:
        return [
            (row["workflow_id"], row["code"], row["is_terminal"] == "true",
             row["is_success"] == "true")
            for row in csv.DictReader(source)
        ]


def header(value):
    return re.sub(r"[_\s]+", " ", value.strip().lower())


def phone(value):
    digits = re.sub(r"[^0-9]", "", value)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if re.fullmatch(r"[6-9][0-9]{9}", digits):
        return "+91" + digits
    raise ValueError("invalid_phone")


def calendar_date(value):
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return date.fromisoformat(value).isoformat()
    match = re.fullmatch(r"([0-9]{2})([-/])([0-9]{2}|[a-z]{3})\2([0-9]{4})", value.lower())
    if not match:
        raise ValueError("invalid date")
    day, separator, month, year = match.groups()
    if month.isalpha():
        if separator != "-":
            raise ValueError("invalid date")
        month = MONTHS.index(month) + 1
    return date(int(year), int(month), int(day)).isoformat()


def amount(value):
    number = re.sub(r"^(?:₹|rs\.?|inr)\s*", "", value, flags=re.IGNORECASE)
    if not re.fullmatch(r"\+?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?|\+?\.[0-9]+", number):
        raise ValueError("invalid decimal")
    result = Decimal(number.replace(",", ""))
    return str(result.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def field_value(field, value):
    match field["type"]:
        case "string":
            return value
        case "phone":
            return phone(value)
        case "date":
            return calendar_date(value)
        case "decimal":
            return amount(value)
        case "enum":
            if value.casefold() in {option.casefold() for option in field["values"]}:
                return value.lower()
            raise ValueError("invalid enum")
        case other:
            raise RuntimeError(f"Unsupported field type {other}")


def timestamp(value):
    if not isinstance(value, str) or not re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ]", value):
        raise ValueError("invalid timestamp")
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=IST)
    return instant.astimezone(timezone.utc).isoformat(timespec="microseconds")


def lead_rows(path, fields):
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.reader(source, strict=True)
        columns = [header(name) for name in next(reader, [])]
        indexes = {}
        for field in fields:
            aliases = {header(alias) for alias in (field["name"], *field.get("aliases", []))}
            indexes[field["name"]] = [i for i, name in enumerate(columns) if name in aliases]
        rows = (row for row in reader if any(cell.strip() for cell in row))
        for number, row in enumerate(rows, start=1):
            yield number, parse_lead(row, fields, indexes)


def parse_lead(row, fields, indexes):
    values = {}
    warnings = 0
    for field in fields:
        name = field["name"]
        value = next(
            (row[i].strip() for i in indexes.get(name, []) if i < len(row) and row[i].strip()),
            "",
        )
        if not value:
            if field.get("required"):
                return {}, 0, f"missing_required:{name}"
            continue
        try:
            values[name] = field_value(field, value)
        except (ValueError, InvalidOperation):
            if field["type"] == "phone":
                return {}, 0, "invalid_phone"
            if field.get("required"):
                return {}, 0, f"invalid_required:{name}"
            warnings += 1
    return values, warnings, None
