import io
import os
import base64
from typing import Optional, List, Dict
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage, PageBreak, KeepTogether, HRFlowable
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm, inch
from PIL import Image as PILImage


PRIMARY_RED = colors.HexColor('#E11B22')
TEXT_DARK = colors.HexColor('#1E293B')
TEXT_MUTED = colors.HexColor('#64748B')
BG_LIGHT = colors.HexColor('#F8FAFC')
BORDER_COLOR = colors.HexColor('#CBD5E1')
CHECK_GREEN = colors.HexColor('#059669')


def generate_inspection_pdf(inspection_data: dict, client_data: dict) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm
    )

    styles = getSampleStyleSheet()
    
    # Custom styles
    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Heading1'],
        fontName='Helvetica-Bold',
        fontSize=15,
        leading=18,
        textColor=PRIMARY_RED,
        spaceAfter=2
    )
    
    subtitle_style = ParagraphStyle(
        'DocSubtitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=10,
        leading=13,
        textColor=TEXT_DARK,
        spaceAfter=4
    )

    header_dealership_style = ParagraphStyle(
        'DealershipHeader',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=7.5,
        leading=10,
        textColor=TEXT_MUTED
    )

    section_header_style = ParagraphStyle(
        'SectionHeader',
        parent=styles['Heading2'],
        fontName='Helvetica-Bold',
        fontSize=9.5,
        leading=12,
        textColor=PRIMARY_RED,
        spaceBefore=5,
        spaceAfter=2
    )

    item_label_style = ParagraphStyle(
        'ItemLabel',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8,
        leading=10.5,
        textColor=TEXT_DARK
    )

    item_status_style = ParagraphStyle(
        'ItemStatus',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8,
        leading=10,
        textColor=CHECK_GREEN,
        alignment=2
    )

    item_status_pending_style = ParagraphStyle(
        'ItemStatusPending',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8,
        leading=10,
        textColor=TEXT_MUTED,
        alignment=2
    )

    meta_label_style = ParagraphStyle(
        'MetaLabel',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8,
        leading=10.5,
        textColor=TEXT_MUTED
    )

    meta_val_style = ParagraphStyle(
        'MetaVal',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8.5,
        leading=11,
        textColor=TEXT_DARK
    )

    meta_val_bold = ParagraphStyle(
        'MetaValBold',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9,
        leading=11.5,
        textColor=TEXT_DARK
    )

    declaration_style = ParagraphStyle(
        'Declaration',
        parent=styles['Normal'],
        fontName='Helvetica-Oblique',
        fontSize=8,
        leading=11,
        textColor=TEXT_DARK
    )

    elements = []

    # Dealership header
    dealership_text = (
        "<b>HARMONY NEW ENERGY AUTO SERVICE (WSP) PTY LTD</b><br/>"
        "415 Heidelberg Rd, Fairfield VIC 3078 &nbsp;|&nbsp; "
        "ABN: 73 675 630 841 &nbsp;|&nbsp; Lic No. 0012833<br/>"
        "T: 03 4110 8888 &nbsp;|&nbsp; E: fairfield@bydfairfield.com.au &nbsp;|&nbsp; W: bydfairfield.com.au"
    )
    elements.append(Paragraph(dealership_text, header_dealership_style))
    elements.append(Spacer(1, 3 * mm))
    elements.append(HRFlowable(width="100%", thickness=1, color=PRIMARY_RED, spaceAfter=4 * mm))

    # Document Title & Order Details
    order_no = inspection_data.get('order_no') or client_data.get('vy_order_id') or '—'
    salesperson = inspection_data.get('salesperson') or client_data.get('salesperson') or '—'
    
    header_table_data = [
        [
            Paragraph("CUSTOMER HANDOVER CHECKLIST &amp; DELIVERY INSPECTION", title_style),
            Paragraph(f"<b>Order #:</b> {order_no}<br/><b>Salesperson:</b> {salesperson}", meta_val_style)
        ]
    ]
    header_table = Table(header_table_data, colWidths=[120 * mm, 66 * mm])
    header_table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('ALIGN', (1,0), (1,0), 'RIGHT'),
    ]))
    elements.append(header_table)
    elements.append(Spacer(1, 2 * mm))

    # Customer & Vehicle Summary Box
    cust_name = client_data.get('name') or inspection_data.get('customer_name') or '—'
    comp_name = client_data.get('fleet_company') or inspection_data.get('company_name') or ''
    address = client_data.get('address') or client_data.get('location') or inspection_data.get('address') or '—'
    phone = client_data.get('phone') or inspection_data.get('phone') or '—'
    email = client_data.get('email') or '—'
    
    vehicle = client_data.get('vehicle') or inspection_data.get('vehicle') or '—'
    rego = client_data.get('rego') or inspection_data.get('rego_stock') or client_data.get('vy_stock_id') or '—'
    vin = client_data.get('vin') or inspection_data.get('vin') or '—'

    info_box_data = [
        [
            Paragraph("<b>CUSTOMER / INVOICE TO</b>", meta_label_style),
            Paragraph("<b>VEHICLE DETAILS</b>", meta_label_style)
        ],
        [
            Paragraph(
                f"<b>{cust_name}</b>" + (f"<br/>{comp_name}" if comp_name else "") +
                f"<br/>{address}<br/>M: {phone} &nbsp;|&nbsp; E: {email}",
                meta_val_style
            ),
            Paragraph(
                f"<b>Vehicle:</b> {vehicle}<br/>"
                f"<b>Rego / Stock #:</b> {rego}<br/>"
                f"<b>VIN:</b> <font name='Courier'>{vin}</font>",
                meta_val_style
            )
        ]
    ]
    info_table = Table(info_box_data, colWidths=[93 * mm, 93 * mm])
    info_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), BG_LIGHT),
        ('BOX', (0,0), (-1,-1), 0.5, BORDER_COLOR),
        ('INNERGRID', (0,0), (-1,-1), 0.5, BORDER_COLOR),
        ('TOPPADDING', (0,0), (-1,-1), 3),
        ('BOTTOMPADDING', (0,0), (-1,-1), 3),
        ('LEFTPADDING', (0,0), (-1,-1), 4),
        ('RIGHTPADDING', (0,0), (-1,-1), 4),
    ]))
    elements.append(info_table)
    elements.append(Spacer(1, 3 * mm))

    # Checklist Items Breakdown
    checklist = inspection_data.get('checklist') or {}

    def render_checklist_section(title: str, items: List[tuple]):
        rows = []
        rows.append([Paragraph(f"<b>{title}</b>", section_header_style), Paragraph("", item_status_style)])
        for item_id, item_label in items:
            is_checked = bool(checklist.get(item_id, False))
            status_p = Paragraph("Complete" if is_checked else "Pending", item_status_style if is_checked else item_status_pending_style)
            rows.append([Paragraph(item_label, item_label_style), status_p])
        
        sec_table = Table(rows, colWidths=[156 * mm, 30 * mm])
        sec_table.setStyle(TableStyle([
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ('LINEBELOW', (0,0), (1,0), 0.5, PRIMARY_RED),
            ('BOTTOMPADDING', (0,0), (-1,-1), 1.5),
            ('TOPPADDING', (0,0), (-1,-1), 1.5),
            ('LEFTPADDING', (0,0), (-1,-1), 2),
            ('RIGHTPADDING', (0,0), (-1,-1), 2),
        ]))
        return sec_table

    # Sections 1 - 4
    elements.append(render_checklist_section("Prior to customer arrival", [
        ('clean_inside_out', 'Car is clean inside and out')
    ]))
    elements.append(Spacer(1, 1.5 * mm))

    elements.append(render_checklist_section("With Customer", [
        ('welcome_congratulate', 'Welcome and congratulate customer on their new BYD'),
        ('introduce_specialist', 'Introduce yourself as a BYD delivery specialist'),
        ('confirm_payments', 'Confirm all payments are made'),
        ('confirm_insurance', 'Confirm the customer has comprehensive insurance')
    ]))
    elements.append(Spacer(1, 1.5 * mm))

    elements.append(render_checklist_section("Exterior", [
        ('access_keys_nfc', 'Show customer how to access the car with keys, NFC card'),
        ('access_boot_power', 'Show customer how to access the boot and explain electronic close, lock and set height'),
        ('charging_cable_usage', 'Show customer included charging cable and how to use'),
        ('v2l_cable_usage', 'Show customer included V2L cable and how to use'),
        ('bonnet_washer_fluid', 'Show customer how to open bonnet and fill washer fluid')
    ]))
    elements.append(Spacer(1, 1.5 * mm))

    elements.append(render_checklist_section("Interior", [
        ('condition_interior', 'Completely satisfied with the condition of interior'),
        ('infotainment_navigation', 'Explanation of infotainment/navigation system/ wireless charger'),
        ('safety_features', 'Explanation of safety features')
    ]))
    elements.append(Spacer(1, 1.5 * mm))

    elements.append(render_checklist_section("Seating", [
        ('rear_seats_split', 'Show customer rear seats and split fold'),
        ('isofix_tether', 'Show customer Isofix and tether points'),
        ('seat_adjustment', 'Show customer how to adjust driver and front passenger seats'),
        ('sunroof_blind', 'Show customer how to use sunroof and blind (if fitted)')
    ]))
    elements.append(Spacer(1, 1.5 * mm))

    elements.append(render_checklist_section("Technology", [
        ('wireless_charging_warning', 'Explain wireless phone charging and advise against storing NFC/credit cards on charging pads'),
        ('bluetooth_carplay_androidauto', 'Pair Bluetooth and explain connection to Apple CarPlay (USB) and Android Auto (wireless)'),
        ('ota_update_procedure', 'Explain OTA update procedure and how data limits do not apply to updates'),
        ('sim_app_registration', 'Explain that SIM card activation and BYD app registration will activate in next couple of days'),
        ('hi_byd_voice', 'Explain "Hi BYD" voice assistant'),
        ('digital_fm_radio', 'Explain Digital and FM radio, and other entertainment features'),
        ('vehicle_controls', 'Explain vehicle controls (located on centre console, steering wheel etc.)'),
        ('gear_selection_neutral', 'Explain how to select gears (including Neutral)'),
        ('park_brake_auto', 'Explain automatic park brake engagement when in Park'),
        ('download_byd_app', 'Ask permission to download the BYD app for the customer to their phone'),
        ('app_features_explained', 'App features explained')
    ]))
    elements.append(Spacer(1, 1.5 * mm))

    elements.append(render_checklist_section("Driving", [
        ('lane_keeping', 'Explain lane keeping features'),
        ('adaptive_cruise', 'Explain adaptive cruise control'),
        ('wipers_blinkers_distance', 'Explain wipers, blinkers and distance to empty gauge'),
        ('driving_modes', 'Explain 3 driving modes - Eco, Normal, Sport'),
        ('regenerative_braking', 'Explain regenerative braking and settings')
    ]))
    elements.append(Spacer(1, 1.5 * mm))

    # Fitted Accessories (dynamically including accessories from client / inspection)
    acc_items = [('acc_care', 'Explain use and care of any accessories fitted')]
    fitted = inspection_data.get('fitted_accessories') or [a.get('name') for a in client_data.get('accessories', [])] or ['Floor Mats Moulded', 'Ceramic Window Tint']
    for idx, acc in enumerate(fitted):
        acc_items.append((f'acc_fitted_{idx}', f"{acc}"))
    acc_items.append(('pickup_site', f"Pickup from BYD {client_data.get('site_location') or 'Fairfield'}"))
    elements.append(render_checklist_section("Accessories", acc_items))
    elements.append(Spacer(1, 1.5 * mm))

    elements.append(render_checklist_section("Service and Support", [
        ('service_sticker_online', 'Show service sticker, and explain how to book online'),
        ('service_intervals', 'Explain service intervals and details'),
        ('online_owners_manual', 'Show customer how to view Owner\'s manual online')
    ]))
    elements.append(Spacer(1, 1.5 * mm))

    elements.append(render_checklist_section("Battery Health", [
        ('soc_discharge_cycle', 'Discharging the vehicle to 10-20% SOC at least once every 3-6 months and fully recharging to 100% on AC'),
        ('ac_preferred_over_dc', 'AC charging is preferable over DC charging; single charge up to 100% preferred over multiple smaller charges'),
        ('long_term_storage_soc', 'If not used for 3 months or longer, keep SOC between 40-60% to prevent over-discharging')
    ]))
    elements.append(Spacer(1, 1.5 * mm))

    elements.append(render_checklist_section("Customer Experience", [
        ('gift_presentation_photo', 'Present gift in front of car and ask for permission to take photo of customer'),
        ('signature_taken', 'Ask for customer signature to take delivery of the vehicle'),
        ('app_survey_feedback', 'Explain to customer they will receive a survey in their BYD app to share feedback in a month')
    ]))
    elements.append(Spacer(1, 3 * mm))

    # Notes & Signatures Section
    notes_text = inspection_data.get('notes') or 'Vehicle handed over in pristine condition. All keys, books and accessories provided.'
    notes_box = [
        [Paragraph("<b>Notes:</b>", meta_label_style)],
        [Paragraph(notes_text, meta_val_style)]
    ]
    notes_table = Table(notes_box, colWidths=[186 * mm])
    notes_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), BG_LIGHT),
        ('BOX', (0,0), (-1,-1), 0.5, BORDER_COLOR),
        ('TOPPADDING', (0,0), (-1,-1), 3),
        ('BOTTOMPADDING', (0,0), (-1,-1), 3),
        ('LEFTPADDING', (0,0), (-1,-1), 4),
        ('RIGHTPADDING', (0,0), (-1,-1), 4),
    ]))
    elements.append(notes_table)
    elements.append(Spacer(1, 3 * mm))

    declaration_text = (
        "I hereby verify that the above items have been duly clarified to me and I have personally inspected and received "
        "the delivered vehicle. I acknowledge the condition I have received the vehicle in is as described by the condition records and images."
    )
    elements.append(Paragraph(declaration_text, declaration_style))
    elements.append(Spacer(1, 3 * mm))

    # Signatures
    sig_date = inspection_data.get('signature_date') or datetime.now().strftime('%d/%m/%Y')
    cust_sig_data = inspection_data.get('customer_signature')
    sig_element = Paragraph(f"<b>{cust_name}</b> (Digitally Signed)", meta_val_bold)
    
    if cust_sig_data and cust_sig_data.startswith('data:image'):
        try:
            raw = cust_sig_data.split(',')[1]
            img_bytes = base64.b64decode(raw)
            img_io = io.BytesIO(img_bytes)
            sig_element = RLImage(img_io, width=45 * mm, height=15 * mm)
        except Exception:
            pass

    sig_table_data = [
        [
            Paragraph(f"<b>Date:</b> {sig_date}", meta_val_style),
            Paragraph("<b>Signature of the Purchaser:</b>", meta_val_style),
            Paragraph(f"<b>Salesperson / Delivery Specialist:</b> {salesperson}", meta_val_style)
        ],
        [
            Paragraph("", meta_val_style),
            sig_element,
            Paragraph("Verified & Completed in Delivery Centre", meta_status_style if 'meta_status_style' in locals() else meta_val_style)
        ]
    ]
    sig_table = Table(sig_table_data, colWidths=[40 * mm, 73 * mm, 73 * mm])
    sig_table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING', (0,0), (-1,-1), 2),
        ('BOTTOMPADDING', (0,0), (-1,-1), 2),
    ]))
    elements.append(sig_table)

    # Condition Photos Page (Page 2/3)
    photos = inspection_data.get('photos') or {}
    if photos:
        elements.append(PageBreak())
        elements.append(Paragraph("VEHICLE CONDITION UPON DELIVERY", title_style))
        elements.append(Paragraph("Photographic inspection record completed at vehicle handover bay", subtitle_style))
        elements.append(Spacer(1, 3 * mm))

        photo_slots = [
            ('front', 'Front View'),
            ('drivers_side', 'Driver’s Side'),
            ('rear', 'Rear View'),
            ('passenger_side', 'Passenger Side'),
            ('fuel_electricity', 'Fuel / Electricity (SOC) Level'),
            ('environment', 'Delivery Environment'),
            ('boot_gift', 'Boot & Gift')
        ]

        photo_cells = []
        for slot_key, slot_title in photo_slots:
            img_src = photos.get(slot_key)
            slot_content = [Paragraph(f"<b>{slot_title}</b>", meta_label_style)]
            
            if img_src and img_src.startswith('data:image'):
                try:
                    raw_p = img_src.split(',')[1]
                    p_bytes = base64.b64decode(raw_p)
                    p_io = io.BytesIO(p_bytes)
                    slot_content.append(RLImage(p_io, width=80 * mm, height=50 * mm))
                except Exception:
                    slot_content.append(Paragraph("[Image attached]", meta_val_style))
            else:
                slot_content.append(Paragraph("<font color='#94A3B8'>[Photo record logged on file]</font>", meta_val_style))
            
            photo_cells.append(slot_content)

        # Pair into 2 columns
        grid_data = []
        for i in range(0, len(photo_cells), 2):
            col1 = photo_cells[i]
            col2 = photo_cells[i+1] if i+1 < len(photo_cells) else [Paragraph("", meta_val_style)]
            grid_data.append([col1, col2])

        p_table = Table(grid_data, colWidths=[93 * mm, 93 * mm])
        p_table.setStyle(TableStyle([
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ('BOX', (0,0), (-1,-1), 0.5, BORDER_COLOR),
            ('INNERGRID', (0,0), (-1,-1), 0.5, BORDER_COLOR),
            ('TOPPADDING', (0,0), (-1,-1), 4),
            ('BOTTOMPADDING', (0,0), (-1,-1), 4),
            ('LEFTPADDING', (0,0), (-1,-1), 4),
            ('RIGHTPADDING', (0,0), (-1,-1), 4),
        ]))
        elements.append(p_table)

    doc.build(elements)
    return buffer.getvalue()
