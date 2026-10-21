# SPDX-License-Identifier: AGPL-3.0-or-later
"""A fake Square for the tests: it replays recorded exchanges (``fixtures/<name>.json``), each a request
this app makes and Square's response to it, and fails the test on any request no exchange expects.
``tools/validate_fixtures.py`` validates every recorded request and response against Square's published
OpenAPI document, so what the engine is tested against is what Square documents.

In a recorded request, the string ``"<any>"`` matches any value (idempotency keys, times derived from
"now")."""

import json
from pathlib import Path

ANY = "<any>"
FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
	return json.loads((FIXTURES / f"{name}.json").read_text())


def matches(expected, actual):
	if expected == ANY:
		return True
	if isinstance(expected, dict):
		return (
			isinstance(actual, dict)
			and expected.keys() == actual.keys()
			and all(matches(v, actual[k]) for k, v in expected.items())
		)
	if isinstance(expected, list):
		return (
			isinstance(actual, list)
			and len(expected) == len(actual)
			and all(matches(e, a) for e, a in zip(expected, actual, strict=True))
		)
	return expected == actual


class Response:
	def __init__(self, status, body):
		self.status_code = status
		self._body = body
		self.content = json.dumps(body).encode() if body is not None else b""

	def json(self):
		return self._body


class Replay:
	"""A transport (``requests.request``'s signature) serving recorded exchanges, each at most once."""

	def __init__(self, *exchanges):
		self.pending = [dict(e) for e in exchanges]
		self.sent = []

	@classmethod
	def of(cls, *fixture_names):
		return cls(*(e for name in fixture_names for e in load(name)["exchanges"]))

	def __call__(self, url, json=None, headers=None, timeout=None):
		"""``requests.post``'s signature: every call this gateway makes is a JSON POST."""
		assert timeout, "every Square call is bounded by a timeout"
		method, params = "POST", None
		path = "/" + url.split("/", 3)[3]
		call = {"method": method, "path": path, "request": json, "params": params}
		self.sent.append(call)
		for index, exchange in enumerate(self.pending):
			if (
				exchange["method"] == method
				and exchange["path"] == path
				and matches(exchange.get("params"), params)
				and matches(exchange.get("request"), json)
			):
				del self.pending[index]
				return Response(exchange.get("status", 200), exchange["response"])
		raise AssertionError(f"Unexpected Square call: {call}")

	def assert_done(self):
		assert not self.pending, f"Square calls expected but never made: {self.pending}"
