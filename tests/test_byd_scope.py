from models import ClientBase, ClientUpdate


def test_scope_sale_types_and_activation_status_are_present():
    client = ClientBase(
        name='Test Client',
        phone='0400 000 000',
        vehicle='BYD Sealion 7',
        sale_type='Fleet',
        fleet_company='Smartleasing',
        registered_operator_type='Company',
        trade_in_attached=True,
        trade_in_sale_date='2026-09-01',
        trade_in_valid_until='2026-10-01',
        trade_in_status='Expiring',
        activation_status='Ready',
    )

    assert client.sale_type == 'Fleet'
    assert client.fleet_company == 'Smartleasing'
    assert client.registered_operator_type == 'Company'
    assert client.trade_in_attached is True
    assert client.activation_status == 'Ready'
    assert client.activation_ready is True


def test_client_update_accepts_byd_scope_fields():
    payload = ClientUpdate(
        sale_type='Government',
        trade_in_attached=False,
        activation_status='Blocked',
    )

    assert payload.sale_type == 'Government'
    assert payload.trade_in_attached is False
    assert payload.activation_status == 'Blocked'


def test_trade_in_deadline_is_calculated_from_sale_date():
    client = ClientBase(
        name='Trade Client',
        phone='0400 000 001',
        vehicle='BYD ATTO 2 Premium',
        trade_in_attached=True,
        trade_in_sale_date='2026-09-05',
    )

    assert client.sale_type == 'Retail'
    assert client.trade_in_valid_until == '2026-10-05'
