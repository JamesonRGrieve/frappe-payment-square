# SPDX-License-Identifier: AGPL-3.0-or-later
"""Validates every recorded Square exchange the tests replay (payment_square/tests/fixtures/*.json) against
Square's published OpenAPI document for the API version this app speaks, pinned to a commit of
square/connect-api-specification: each request body against its operation's request schema, each query
parameter against the operation's parameters, each response against its 200 schema, and each webhook
event body against its event schema. A fixture can't invent a field, an operation or a parameter.

``python3 -m tools.validate_fixtures`` (stdlib only). The document is cached in .cache/specs."""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

from tools.openapi import ApiSpec, OpenApiValidator

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "payment_square" / "tests" / "fixtures"
# Square API 2026-09-16 (square/connect-api-specification, "Release 2026-09-16"), the Square-Version
# square_client.py sends.
SPEC_URL = (
	"https://raw.githubusercontent.com/square/connect-api-specification/"
	"79ccc3f89e1527f7b943cc2f6628b6098572583e/api.json"
)
SPEC_CACHE = ROOT / ".cache" / "specs" / "connect-api-2026-09-16.json"
FETCH_TIMEOUT_SECONDS = 120


def spec_document() -> dict[str, Any]:
	if not SPEC_CACHE.is_file():
		with urllib.request.urlopen(SPEC_URL, timeout=FETCH_TIMEOUT_SECONDS) as response:  # noqa: S310 - pinned https URL
			body = response.read()
		SPEC_CACHE.parent.mkdir(parents=True, exist_ok=True)
		SPEC_CACHE.write_bytes(body)
	document = json.loads(SPEC_CACHE.read_text())
	if not isinstance(document, dict):
		raise TypeError("The Square API document is not an object.")
	return document


def check_exchange(
	spec: ApiSpec, validator: OpenApiValidator, where: str, exchange: dict[str, Any]
) -> list[str]:
	method, path = exchange["method"], exchange["path"]
	if spec.operation(method, path) is None:
		return [f"{where}: {method} {path} is not a Square operation"]
	errors: list[str] = []
	request = exchange.get("request")
	if request is not None:
		schema = spec.request_schema(method, path)
		if schema is None:
			errors.append(f"{where}: {method} {path} takes no JSON body")
		else:
			errors += [f"{where} request: {e}" for e in validator.validate(request, schema)]
	parameters = spec.query_parameters(method, path)
	for name, value in (exchange.get("params") or {}).items():
		if name not in parameters:
			errors.append(f"{where} params: {name} is not a parameter of {method} {path}")
		else:
			errors += [
				f"{where} params: {e}" for e in validator.validate(value, parameters[name], f"${name}")
			]
	response_schema = spec.response_schema(method, path)
	if response_schema is None:
		errors.append(f"{where}: {method} {path} has no 200 JSON response")
	else:
		errors += [
			f"{where} response: {e}" for e in validator.validate(exchange["response"], response_schema)
		]
	return errors


def main() -> int:
	document = spec_document()
	spec = ApiSpec.of(document, "")
	validator = OpenApiValidator(spec.schemas, spec.ref_prefix, complete_objects=False)
	errors: list[str] = []
	count = 0
	for fixture in sorted(FIXTURES.glob("*.json")):
		doc = json.loads(fixture.read_text())
		for index, exchange in enumerate(doc["exchanges"]):
			errors += check_exchange(spec, validator, f"{fixture.name}[{index}]", exchange)
			count += 1
		for index, event in enumerate(doc.get("events") or []):
			schema = spec.schemas.get(event["schema"])
			if schema is None:
				errors.append(f"{fixture.name} event {index}: no schema {event['schema']}")
				continue
			errors += [
				f"{fixture.name} event {index}: {e}" for e in validator.validate(event["body"], schema)
			]
			count += 1
	for error in errors:
		sys.stderr.write(error + "\n")
	sys.stdout.write(
		f"{count} recorded Square requests, responses and events checked, {len(errors)} errors\n"
	)
	return 1 if errors else 0


if __name__ == "__main__":
	raise SystemExit(main())
