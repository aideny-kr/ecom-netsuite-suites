from uuid import uuid4

import pytest

from app.services.transaction_ops import order_actions, state_service
from tests.test_transaction_defaults import connections
from tests.test_transaction_tables import seed_orders


async def order_fixture(db, user, entity="Framework BV"):
    source, _ = await connections(db, user.tenant_id)
    order = (await seed_orders(db, user.tenant_id, ["R100120031"]))[0]
    order.source_connection_id = source.id
    order.raw_data = {"order": {"business_entity": entity}}
    await db.flush()
    return order


async def test_investigate_uses_backend_entity_scope_and_reuses_request(db, admin_user):
    user, _ = admin_user
    order = await order_fixture(db, user)
    key = uuid4()
    run = await order_actions.investigate_order(db, user, order.id, key)
    assert run.config_snapshot["subsidiary_id"] == "2"
    assert run.params_json["order_references"] == [order.order_number]
    assert run.origin == "manual"
    again = await order_actions.investigate_order(db, user, order.id, key)
    assert again.id == run.id


async def test_investigation_cannot_read_another_tenants_order(db, admin_user, admin_user_b):
    order = await order_fixture(db, admin_user_b[0])
    with pytest.raises(state_service.StateError, match="order_not_found"):
        await order_actions.investigate_order(db, admin_user[0], order.id, uuid4())


async def test_unknown_entity_is_not_sent_to_a_guessed_subsidiary(db, admin_user):
    user, _ = admin_user
    order = await order_fixture(db, user, "Unknown entity")
    with pytest.raises(state_service.StateError, match="order_scope_unavailable"):
        await order_actions.investigate_order(db, user, order.id, uuid4())


async def test_reader_cannot_bootstrap_backend_config(db, readonly_user):
    user, _ = readonly_user
    order = await order_fixture(db, user)
    with pytest.raises(state_service.StateError, match="permission_denied"):
        await order_actions.investigate_order(db, user, order.id, uuid4())
