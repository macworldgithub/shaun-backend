import pytest
from models import Client

def test_ready_for_delivery_all_met_no_trade_in():
    client = Client(
        name="Alice Walker",
        phone="0400111222",
        vehicle="BYD Atto 3",
        payment_complete=True,
        trade_in_flag=False,
        trade_in_attached=False,
        pdi_complete=True,
        registration_docs_complete=True
    )
    assert client.ready_for_delivery is True

def test_ready_for_delivery_missing_payment():
    client = Client(
        name="Bob Builder",
        phone="0400111223",
        vehicle="BYD Seal",
        payment_complete=False,
        trade_in_flag=False,
        trade_in_attached=False,
        pdi_complete=True,
        registration_docs_complete=True
    )
    assert client.ready_for_delivery is False

def test_ready_for_delivery_with_trade_in_pending_docs():
    # Trade-in attached but trade-in docs not completed
    client = Client(
        name="Charlie Chaplin",
        phone="0400111224",
        vehicle="BYD Dolphin",
        payment_complete=True,
        trade_in_flag=True,
        trade_in_attached=True,
        trade_in_docs_complete=False,
        trade_in_status="Pending",
        pdi_complete=True,
        registration_docs_complete=True
    )
    assert client.ready_for_delivery is False

def test_ready_for_delivery_with_trade_in_docs_returned():
    # Trade-in attached and trade-in docs completed
    client = Client(
        name="Charlie Chaplin",
        phone="0400111224",
        vehicle="BYD Dolphin",
        payment_complete=True,
        trade_in_flag=True,
        trade_in_attached=True,
        trade_in_docs_complete=True,
        pdi_complete=True,
        registration_docs_complete=True
    )
    assert client.ready_for_delivery is True

def test_ready_for_delivery_with_trade_in_settled_status():
    # Trade-in status settled counts as returned
    client = Client(
        name="David Warner",
        phone="0400111225",
        vehicle="BYD Sealion 7",
        payment_complete=True,
        trade_in_flag=True,
        trade_in_attached=True,
        trade_in_docs_complete=False,
        trade_in_status="Settled",
        pdi_complete=True,
        registration_docs_complete=True
    )
    assert client.ready_for_delivery is True

def test_ready_for_delivery_missing_pdi():
    client = Client(
        name="Eve Adams",
        phone="0400111226",
        vehicle="BYD Shark 6",
        payment_complete=True,
        trade_in_flag=False,
        pdi_complete=False,
        stage="Pre-Delivery Inspection",
        registration_docs_complete=True
    )
    assert client.ready_for_delivery is False

def test_ready_for_delivery_pdi_by_stage():
    client = Client(
        name="Eve Adams",
        phone="0400111226",
        vehicle="BYD Shark 6",
        payment_complete=True,
        trade_in_flag=False,
        pdi_complete=False,
        stage="Ready for Pickup",
        registration_docs_complete=True
    )
    assert client.ready_for_delivery is True

def test_ready_for_delivery_missing_rego_docs():
    client = Client(
        name="Frank Miller",
        phone="0400111227",
        vehicle="BYD Atto 2",
        payment_complete=True,
        trade_in_flag=False,
        pdi_complete=True,
        registration_docs_complete=False,
        registration_status="Awaiting registration documents"
    )
    assert client.ready_for_delivery is False

def test_ready_for_delivery_rego_by_status():
    client = Client(
        name="Frank Miller",
        phone="0400111227",
        vehicle="BYD Atto 2",
        payment_complete=True,
        trade_in_flag=False,
        pdi_complete=True,
        registration_docs_complete=False,
        registration_status="Complete"
    )
    assert client.ready_for_delivery is True
