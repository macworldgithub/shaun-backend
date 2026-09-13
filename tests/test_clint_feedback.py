import pytest
from datetime import datetime, timezone, timedelta
from models import User, ClientBase, Client, DocumentType, UserCreate

def test_document_types():
    # Verify new document types exist in schema
    docs = ['Medicare card', 'Transfer form', 'Acquisition police strip', 'Customer trade-in checklist']
    for doc in docs:
        assert doc in DocumentType.__args__

def test_user_team_and_site():
    u = User(email="agent@byd.com", name="Test Agent", team="Booking Team", active_site="Melbourne")
    assert u.team == "Booking Team"
    assert u.active_site == "Melbourne"

def test_fairfield_trade_in_policy():
    # Fairfield site trade-in valuation holds for 30 days regardless of valuation
    sale_date = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    client = Client(
        name="John Doe",
        phone="0400000000",
        vehicle="BYD Atto 3",
        trade_in_attached=True,
        trade_in_sale_date=sale_date,
        trade_in_valuation=40000,
        site_location="Fairfield"
    )
    expected_valid_until = (datetime.strptime(sale_date, '%Y-%m-%d').date() + timedelta(days=30)).isoformat()
    assert client.trade_in_valid_until == expected_valid_until

def test_non_fairfield_trade_in_policy_over_30k():
    # Non-Fairfield site holding > $30k holds for 7 days
    sale_date = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    client = Client(
        name="Jane Smith",
        phone="0411111111",
        vehicle="BYD Seal",
        trade_in_attached=True,
        trade_in_sale_date=sale_date,
        trade_in_valuation=35000,
        site_location="Melbourne"
    )
    expected_valid_until = (datetime.strptime(sale_date, '%Y-%m-%d').date() + timedelta(days=7)).isoformat()
    assert client.trade_in_valid_until == expected_valid_until

def test_non_fairfield_trade_in_policy_under_30k():
    # Non-Fairfield site holding <= $30k holds for 14 days
    sale_date = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    client = Client(
        name="Bob Brown",
        phone="0422222222",
        vehicle="BYD Dolphin",
        trade_in_attached=True,
        trade_in_sale_date=sale_date,
        trade_in_valuation=25000,
        site_location="Sydney"
    )
    expected_valid_until = (datetime.strptime(sale_date, '%Y-%m-%d').date() + timedelta(days=14)).isoformat()
    assert client.trade_in_valid_until == expected_valid_until

def test_multi_salesperson_and_staff_roles():
    client = Client(
        name="Tyson Lopez",
        phone="+61401556609",
        vehicle="BYD Atto 3 Premium",
        site_location="Fairfield",
        address="123 Example St, Melbourne VIC",
        salesperson="Kahlia Duncan",
        secondary_salesperson="Marcus Vance",
        delivery_consultant="Sarah Connor",
        handover_specialist="David Miller"
    )
    assert client.salesperson == "Kahlia Duncan"
    assert client.secondary_salesperson == "Marcus Vance"
    assert client.delivery_consultant == "Sarah Connor"
    assert client.handover_specialist == "David Miller"
    assert client.site_location == "Fairfield"
    assert client.address == "123 Example St, Melbourne VIC"
