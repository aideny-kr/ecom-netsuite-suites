async def test_non_admin_forbidden(client, member_user):
    _, headers = member_user
    resp = await client.post(
        "/api/v1/metrics",
        json={
            "key": "net_margin",
            "display_name": "Net Margin",
            "definition": "x",
            "unit": "percent",
            "source_kind": "expression",
            "expression": "net_income / gross_revenue",
            "depends_on": ["net_income", "gross_revenue"],
        },
        headers=headers,
    )
    assert resp.status_code == 403


async def test_admin_can_author_tenant_metric(client, admin_user):
    _, headers = admin_user
    resp = await client.post(
        "/api/v1/metrics",
        json={
            "key": "net_margin",
            "display_name": "Net Margin",
            "definition": "x",
            "unit": "percent",
            "source_kind": "expression",
            "expression": "net_income / gross_revenue",
            "depends_on": ["net_income", "gross_revenue"],
        },
        headers=headers,
    )
    assert resp.status_code == 201
    assert resp.json()["key"] == "net_margin"
