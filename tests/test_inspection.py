import pytest
from datetime import datetime, timezone
from models import Client, DeliveryInspection, DeliveryInspectionUpdate
from services.inspection_pdf import generate_inspection_pdf

def test_delivery_inspection_model_defaults():
    inspection = DeliveryInspection(
        client_id="client-123",
        customer_name="John Doe",
        vehicle_model="BYD Sealion 6",
        vin="LC0123456789ABCDE",
        registration_no="BYD-888",
    )
    assert inspection.status == "draft"
    assert inspection.checklist == {}
    assert inspection.photos == {}
    assert "satisfactory condition" in inspection.verification_declaration

def test_pdf_generation_without_errors():
    client_data = {
        "id": "client-123",
        "name": "Jane Smith",
        "vehicle": "BYD Seal Premium",
        "vin": "LC099988877766554",
        "rego": "VIC-999",
        "delivery_date": "2026-09-17",
        "salesperson": "David Attenborough",
    }
    inspection_data = {
        "checklist": {
            "car_washed_vacuumed_windows": True,
            "plates_fitted_securely": True,
            "build_plate_checked": True,
            "compliance_plate_checked": True,
            "roadside_card_in_glovebox": True,
            "manual_in_glovebox": True,
            "logbook_manuals_in_vehicle": True,
            "keys_and_emergency_key": True,
            "battery_charged_100_or_fuel": True,
            "software_updated_latest": True,
            "walk_around_vehicle": True,
            "point_out_keys_emergency": True,
            "show_lock_unlock": True,
            "nfc_card_setup_demo": True,
            "touchscreen_voice_control": True,
            "byd_app_pairing_login": True,
            "bluetooth_nav_radio_setup": True,
            "fitted_accessories_confirmed": True,
            "service_intervals_booking_explained": True,
            "warranty_terms_coverage_explained": True,
            "soc_confirmed_at_delivery": True,
            "intro_dealership_service_team": True,
        },
        "fitted_accessories": ["Floor Mats", "Dash Cam"],
        "notes": "Customer was very pleased with vehicle orientation and app setup.",
        "customer_signature": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
        "salesperson_signature": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
    }

    pdf_bytes = generate_inspection_pdf(client_data, inspection_data)
    assert isinstance(pdf_bytes, bytes)
    assert len(pdf_bytes) > 1000
    assert pdf_bytes.startswith(b'%PDF')

def test_inspection_completion_sets_pdi_and_checklist_status():
    client_dict = {
        "id": "c-456",
        "name": "Mark Taylor",
        "phone": "0411222333",
        "vehicle": "BYD Atto 3 Extended",
        "payment_complete": True,
        "trade_in_flag": False,
        "trade_in_attached": False,
        "registration_docs_complete": True,
        "pdi_complete": False,
        "handover_checklist_status": "Not issued",
    }
    client = Client(**client_dict)
    assert client.ready_for_delivery is False

    # Simulate inspection completion action
    client_dict["pdi_complete"] = True
    client_dict["handover_checklist_status"] = "Signed copy on file"
    completed_client = Client(**client_dict)

    # Now all requirements are satisfied
    assert completed_client.ready_for_delivery is True
