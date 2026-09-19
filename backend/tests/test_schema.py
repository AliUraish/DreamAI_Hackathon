from app.schema import detect_renames, diff_schemas, hash_schema, infer_schema

V1 = {"orders": [{"id": "o1", "name": "Grace", "price": 25.5, "status": "paid"}], "total": 1}
V2 = {"orders": [{"id": "o1", "customer_name": "Grace", "amount": 2550, "currency": "usd", "status": "paid"}], "total": 1}


def test_values_do_not_change_the_hash():
    other = {"orders": [{"id": "zz", "name": "Linus", "price": 1, "status": "x"}], "total": 99}
    assert hash_schema(infer_schema(V1)) == hash_schema(infer_schema(other))


def test_v1_to_v2_diff_and_renames():
    changes = diff_schemas(infer_schema(V1), infer_schema(V2), "response")
    kinds = {(c["kind"], c["path"]) for c in changes}
    assert ("field-removed", "orders[].name") in kinds
    assert ("field-removed", "orders[].price") in kinds
    assert ("field-added", "orders[].customer_name") in kinds
    assert ("field-added", "orders[].amount") in kinds

    renames = {(r["before"], r["after"], r["confidence"]) for r in detect_renames(changes)}
    assert ("name", "customer_name", "name-and-type") in renames
    # price -> amount shares no name token; it is the only number left, so type-only.
    assert ("price", "amount", "type-only") in renames


def test_empty_array_changes_hash_but_is_not_drift():
    full, empty = infer_schema(V1), infer_schema({"orders": [], "total": 0})
    assert hash_schema(full) != hash_schema(empty)
    assert diff_schemas(full, empty, "response") == []


def test_optional_field_in_array_items():
    node = infer_schema([{"a": 1, "b": "x"}, {"a": 2}])
    props = node["items"]["properties"]
    assert props["a"]["required"] is True and props["b"]["required"] is False


def test_bool_is_not_a_number():
    assert infer_schema(True)["type"] == "boolean"
