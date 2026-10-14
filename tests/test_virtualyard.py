import pytest
from models import User
from routers.virtualyard import list_deals, get_deal, get_overview_stats
import db as db_module


@pytest.fixture
def mock_admin():
    return User(
        id='test-admin',
        email='admin@test.com',
        name='Admin User',
        role='super_admin',
        active=True,
    )


@pytest.fixture(autouse=True)
def reset_db_client():
    db_module.close()
    yield
    db_module.close()


@pytest.mark.anyio
async def test_list_deals(mock_admin):
    res = await list_deals(
        q=None,
        site=None,
        status=None,
        settlement_status=None,
        payment_method=None,
        has_payments=None,
        page=1,
        limit=10,
        sort_by='date',
        sort_order='desc',
        user=mock_admin,
    )
    assert 'items' in res
    assert 'total' in res
    assert 'stats' in res
    assert res['total'] > 0
    assert len(res['items']) <= 10
    first = res['items'][0]
    assert 'invoice_id' in first
    assert 'deal_total' in first
    assert 'payment_total' in first
    assert 'settlement_status' in first


@pytest.mark.anyio
async def test_get_deal_detail(mock_admin):
    res = await list_deals(
        q=None,
        site=None,
        status=None,
        settlement_status=None,
        payment_method=None,
        has_payments=True,
        page=1,
        limit=5,
        sort_by='date',
        sort_order='desc',
        user=mock_admin,
    )
    assert len(res['items']) > 0
    sample_invoice = res['items'][0]['invoice_id']

    deal = await get_deal(sample_invoice, user=mock_admin)
    assert deal['invoice_id'] == sample_invoice
    assert 'payments' in deal
    assert isinstance(deal['payments'], list)
    assert len(deal['payments']) > 0
    payment = deal['payments'][0]
    assert 'amount' in payment
    assert 'method' in payment


@pytest.mark.anyio
async def test_get_overview_stats(mock_admin):
    stats = await get_overview_stats(user=mock_admin)
    assert 'totals' in stats
    assert 'locations' in stats
    assert 'payment_methods' in stats
    assert 'settlement_statuses' in stats
    assert len(stats['locations']) > 0
    assert len(stats['payment_methods']) > 0


@pytest.mark.anyio
async def test_search_and_filters(mock_admin):
    # Search by known yard
    res_cs = await list_deals(
        q=None,
        site='BYD Caroline Springs',
        status=None,
        settlement_status=None,
        payment_method=None,
        has_payments=None,
        page=1,
        limit=10,
        sort_by='date',
        sort_order='desc',
        user=mock_admin,
    )
    assert res_cs['total'] > 0
    for item in res_cs['items']:
        assert 'Caroline Springs' in item['location_name']
