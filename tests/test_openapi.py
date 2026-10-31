"""The published OpenAPI spec is generated, complete and cannot drift from the app.

`openapi/trustee-v1.json` is what SDK generation and the Postman collection read, so a stale or incomplete
file breaks integrators silently. These tests fail loudly instead: regenerate with
`scripts/export_openapi.py` when the API changes.
"""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = ROOT / "openapi" / "trustee-v1.json"
PUBLIC_PREFIXES = ("/v1/", "/.well-known/", "/a2a")
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


def _load_exporter() -> Any:
    path = ROOT / "scripts" / "export_openapi.py"
    module_spec = importlib.util.spec_from_file_location("export_openapi", path)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def exporter() -> Any:
    return _load_exporter()


@pytest.fixture(scope="module")
def committed() -> dict[str, Any]:
    import json

    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))


def _operations(spec: dict[str, Any]):
    for path, item in spec["paths"].items():
        for method, op in item.items():
            if method in HTTP_METHODS:
                yield path, method, op


def _refs(node: Any):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref":
                yield value
            else:
                yield from _refs(value)
    elif isinstance(node, list):
        for item in node:
            yield from _refs(item)


def test_committed_spec_matches_a_fresh_export(exporter: Any) -> None:
    fresh = exporter.render(exporter.build_spec())
    committed_text = SPEC_PATH.read_text(encoding="utf-8")
    assert committed_text == fresh, "openapi/trustee-v1.json is stale; run scripts/export_openapi.py"


def test_only_public_platform_paths_are_exported(committed: dict[str, Any]) -> None:
    assert committed["paths"], "spec has no paths"
    for path in committed["paths"]:
        assert path.startswith(PUBLIC_PREFIXES), f"{path} is not part of the public platform surface"
    assert "/healthz" not in committed["paths"]
    assert not any(p.startswith("/api/") for p in committed["paths"])


def test_every_operation_has_a_unique_id_and_a_tag(committed: dict[str, Any]) -> None:
    ids: list[str] = []
    for path, method, op in _operations(committed):
        assert op.get("operationId"), f"{method.upper()} {path} has no operationId"
        assert op.get("tags"), f"{method.upper()} {path} has no tag"
        assert op.get("summary"), f"{method.upper()} {path} has no summary"
        ids.append(op["operationId"])
    assert len(ids) == len(set(ids)), "operationId values must be unique"
    for op_id in ids:
        assert op_id == op_id.lower() and " " not in op_id, f"operationId {op_id!r} is not snake_case"


def test_the_a2a_endpoint_is_documented_under_its_own_tag(committed: dict[str, Any]) -> None:
    op = committed["paths"]["/a2a"]["post"]
    assert op["tags"] == ["A2A"] and op["operationId"] == "a2a_jsonrpc"
    request_schema = op["requestBody"]["content"]["application/json"]["schema"]
    assert "SendMessage" in request_schema["properties"]["method"]["enum"]
    assert "message/send" in request_schema["properties"]["method"]["description"]
    assert set(op["responses"]) == {"200"}  # protocol errors are JSON-RPC errors inside an HTTP 200


def test_every_tag_used_is_declared(committed: dict[str, Any]) -> None:
    declared = {tag["name"] for tag in committed["tags"]}
    used = {tag for _, _, op in _operations(committed) for tag in op["tags"]}
    assert used <= declared
    assert declared <= used, f"declared tags with no operation: {sorted(declared - used)}"


def test_every_reference_resolves(committed: dict[str, Any]) -> None:
    schemas = committed.get("components", {}).get("schemas", {})
    for ref in _refs(committed):
        assert ref.startswith("#/components/schemas/"), f"unexpected $ref {ref}"
        assert ref.rsplit("/", 1)[-1] in schemas, f"dangling $ref {ref}"


def test_document_metadata(committed: dict[str, Any]) -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert committed["openapi"].startswith("3.1")
    assert committed["info"]["title"] == "Trustee Platform API"
    assert committed["info"]["version"] == pyproject["project"]["version"]
    # The signing scheme is the one thing an integrator cannot infer from the paths, so it must be in the spec.
    assert "Ed25519" in committed["info"]["description"]
    assert "canonical JSON" in committed["info"]["description"]
    assert [s["url"] for s in committed["servers"]][0].startswith("https://")


def test_money_and_refusal_paths_document_their_error_codes(committed: dict[str, Any]) -> None:
    expected = {
        ("/v1/deals", "post"): {"401", "403", "409", "502"},
        ("/v1/deals/{deal_id}/attest", "post"): {"401", "409", "502"},
        ("/v1/deals/{deal_id}/delivery", "post"): {"401", "404", "409"},
        ("/v1/mandates", "post"): {"401", "403", "409"},
        ("/v1/principals", "post"): {"401", "409"},
    }
    for (path, method), codes in expected.items():
        documented = set(committed["paths"][path][method]["responses"])
        assert codes <= documented, f"{method.upper()} {path} is missing {sorted(codes - documented)}"


def test_error_responses_use_the_detail_shape(committed: dict[str, Any]) -> None:
    for path, method, op in _operations(committed):
        for code, response in op["responses"].items():
            if code.startswith("4") or code.startswith("5"):
                schema = response["content"]["application/json"]["schema"]
                if schema.get("$ref", "").endswith("/HTTPValidationError"):
                    continue  # FastAPI's generated 422 for request-validation failures
                assert "detail" in schema.get("required", []), f"{method.upper()} {path} {code} lacks detail"


def test_spec_is_valid_openapi_31(committed: dict[str, Any]) -> None:
    # The validator is a test-only dependency. Structural checks above run regardless.
    validate = pytest.importorskip("openapi_spec_validator").validate
    validate(committed)


# ----------------------------------------------------------------------------- Postman collection


def _load_postman_collection() -> dict[str, Any]:
    import json

    return json.loads((ROOT / "postman" / "Trustee.postman_collection.json").read_text(encoding="utf-8"))


def _collection_requests(collection: dict[str, Any]):
    for folder in collection["item"]:
        for item in folder["item"]:
            yield folder["name"], item


def _spec_path_matches(template: str, concrete: str) -> bool:
    import re

    pattern = re.escape(template).replace(r"\{", "{").replace(r"\}", "}")
    pattern = re.sub(r"\{[^}]+\}", "[^/]+", pattern)
    return re.fullmatch(pattern, concrete) is not None


def test_postman_files_are_generated_and_not_stale() -> None:
    import json

    maker = _load_script("make_postman")
    collection_text = (ROOT / "postman" / "Trustee.postman_collection.json").read_text(encoding="utf-8")
    environment_text = (ROOT / "postman" / "Trustee.postman_environment.json").read_text(encoding="utf-8")
    assert collection_text == maker.render(maker.build_collection()), "run scripts/make_postman.py"
    assert environment_text == maker.render(maker.build_environment()), "run scripts/make_postman.py"
    assert json.loads(collection_text)["info"]["schema"].endswith("collection.json")


def test_postman_requests_map_to_documented_operations(committed: dict[str, Any]) -> None:
    operations = {(method, path) for path, method, _ in _operations(committed)}
    for _folder, item in _collection_requests(_load_postman_collection()):
        request = item["request"]
        concrete = "/" + "/".join(request["url"]["path"])
        method = request["method"].lower()
        if not concrete.startswith(PUBLIC_PREFIXES):
            continue  # /healthz and /openapi.json are checked by their own tests
        matched = any(m == method and _spec_path_matches(p, concrete) for m, p in operations)
        assert matched, f"{item['name']}: {method.upper()} {concrete} is not in the OpenAPI spec"


def test_every_postman_request_asserts_its_response() -> None:
    for folder, item in _collection_requests(_load_postman_collection()):
        scripts = [e["script"]["exec"] for e in item.get("event", []) if e["listen"] == "test"]
        assert scripts and any("pm.test" in line for line in scripts[0]), f"{folder} / {item['name']} asserts nothing"


def test_postman_environment_holds_no_secret_values() -> None:
    import json

    environment = json.loads((ROOT / "postman" / "Trustee.postman_environment.json").read_text(encoding="utf-8"))
    values = {v["key"]: v for v in environment["values"]}
    assert values["operatorToken"]["type"] == "secret"
    assert values["operatorToken"]["value"] == ""


def _load_script(name: str) -> Any:
    path = ROOT / "scripts" / f"{name}.py"
    module_spec = importlib.util.spec_from_file_location(name, path)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module
