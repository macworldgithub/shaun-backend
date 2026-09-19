import io
import os
import base64
from typing import Optional, List, Dict
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage, PageBreak
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from PIL import Image as PILImage

LOGO_PATH = os.path.join(os.path.dirname(__file__), 'byd_logo.png')


def _decode_image(src: Optional[str], max_w: float, max_h: float) -> Optional[RLImage]:
    if not src:
        return None
    try:
        if src.startswith('data:image'):
            raw = src.split(',')[1]
            img_bytes = base64.b64decode(raw)
        elif os.path.exists(src):
            with open(src, 'rb') as f:
                img_bytes = f.read()
        else:
            return None
        
        pil_img = PILImage.open(io.BytesIO(img_bytes))
        w, h = pil_img.size
        if w == 0 or h == 0:
            return None
        
        scale = min(max_w / w, max_h / h)
        final_w = w * scale
        final_h = h * scale
        
        return RLImage(io.BytesIO(img_bytes), width=final_w, height=final_h)
    except Exception as e:
        return None


def generate_inspection_pdf(arg1: dict, arg2: dict) -> bytes:
    # Accept (inspection_data, client_data) or (client_data, inspection_data)
    if 'checklist' in arg1 or 'photos' in arg1 or 'customer_signature' in arg1 or 'fitted_accessories' in arg1:
        inspection_data, client_data = arg1 or {}, arg2 or {}
    elif 'checklist' in arg2 or 'photos' in arg2 or 'customer_signature' in arg2 or 'fitted_accessories' in arg2:
        inspection_data, client_data = arg2 or {}, arg1 or {}
    else:
        inspection_data, client_data = arg1 or {}, arg2 or {}

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=10 * mm,
        rightMargin=10 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm
    )

    page_width = 595.27 - 20 * mm  # ~538.6 pt

    styles = getSampleStyleSheet()

    dealership_regular = ParagraphStyle(
        'DealershipRegular',
        fontName='Helvetica',
        fontSize=7.2,
        leading=9.5,
        alignment=2,
        textColor=colors.HexColor('#000000')
    )

    inv_deliv_reg = ParagraphStyle(
        'InvDelivReg',
        fontName='Helvetica',
        fontSize=7.5,
        leading=10.5,
        textColor=colors.HexColor('#000000')
    )

    title_style = ParagraphStyle(
        'MainTitle',
        fontName='Helvetica-Bold',
        fontSize=12,
        leading=15,
        alignment=2,
        textColor=colors.HexColor('#000000')
    )

    veh_label_style = ParagraphStyle(
        'VehLabel',
        fontName='Helvetica',
        fontSize=8,
        leading=10,
        textColor=colors.HexColor('#555555')
    )
    veh_val_style = ParagraphStyle(
        'VehVal',
        fontName='Helvetica-Bold',
        fontSize=8,
        leading=10,
        textColor=colors.HexColor('#000000')
    )

    section_hdr_style = ParagraphStyle(
        'SectionHdr',
        fontName='Helvetica-Bold',
        fontSize=9,
        leading=11,
        textColor=colors.HexColor('#000000'),
        spaceBefore=10,
        spaceAfter=3
    )

    item_lbl_style = ParagraphStyle(
        'ItemLbl',
        fontName='Helvetica',
        fontSize=8,
        leading=11,
        textColor=colors.HexColor('#000000')
    )
    item_status_style = ParagraphStyle(
        'ItemStatus',
        fontName='Helvetica',
        fontSize=8,
        leading=11,
        alignment=2,
        textColor=colors.HexColor('#008000')
    )

    decl_style = ParagraphStyle(
        'DeclarationStyle',
        fontName='Helvetica',
        fontSize=7.5,
        leading=10.5,
        textColor=colors.HexColor('#000000')
    )
    sig_lbl_style = ParagraphStyle(
        'SigLblStyle',
        fontName='Helvetica',
        fontSize=7.5,
        leading=9,
        textColor=colors.HexColor('#333333')
    )
    sig_val_style = ParagraphStyle(
        'SigValStyle',
        fontName='Helvetica',
        fontSize=8,
        leading=10,
        alignment=1,
        textColor=colors.HexColor('#000000')
    )

    elements = []

    # 1. Dealership header
    dealership_name = inspection_data.get('dealership_name') or 'HARMONY NEW ENERGY AUTO SERVICE (WSP) PTY LTD'
    dealership_address = inspection_data.get('dealership_address') or '415 Heidelberg Rd\nFairfield VIC 3078'
    dealership_abn = inspection_data.get('dealership_abn') or '73 675 630 841'
    dealership_lic = inspection_data.get('dealership_lic') or '0012833'
    dealership_phone = inspection_data.get('dealership_phone') or '03 4110 8888'
    dealership_email = inspection_data.get('dealership_email') or 'fairfield@bydfairfield.com.au'
    dealership_web = inspection_data.get('dealership_web') or 'bydfairfield.com.au'

    dealership_lines = [
        f"<b>{dealership_name}</b>",
        dealership_address.replace('\n', '<br/>'),
        f"<b>ABN:</b> {dealership_abn}",
        f"<b>Lic No.</b> {dealership_lic}",
        "<br/>",
        f"<b>T:</b> {dealership_phone}",
        f"<b>E:</b> {dealership_email}",
        f"<b>W:</b> {dealership_web}"
    ]
    dealership_html = "<br/>".join(dealership_lines)

    logo_elem = None
    if os.path.exists(LOGO_PATH):
        logo_elem = RLImage(LOGO_PATH, width=108, height=25.92)

    top_header_table = Table(
        [[logo_elem or "", Paragraph(dealership_html, dealership_regular)]],
        colWidths=[page_width - 230, 230]
    )
    top_header_table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('LEFTPADDING', (0,0), (-1,-1), 0),
        ('RIGHTPADDING', (0,0), (-1,-1), 0),
        ('TOPPADDING', (0,0), (-1,-1), 0),
        ('BOTTOMPADDING', (0,0), (-1,-1), 0),
    ]))
    elements.append(top_header_table)
    elements.append(Spacer(1, 14))

    # 2. Invoice / Deliver / Title
    cust_name = client_data.get('name') or inspection_data.get('customer_name') or ''
    fleet_comp = client_data.get('fleet_company') or inspection_data.get('company_name') or ''
    address_str = client_data.get('address') or client_data.get('location') or inspection_data.get('address') or ''
    phone_str = client_data.get('phone') or inspection_data.get('phone') or ''
    abn_str = client_data.get('abn') or client_data.get('fleet_reference') or ''
    
    attn_name = cust_name
    primary_name = fleet_comp if fleet_comp else cust_name

    if '\n' in address_str:
        addr_parts = address_str.split('\n')
        addr_line1 = addr_parts[0].strip()
        addr_line2 = addr_parts[1].strip() if len(addr_parts) > 1 else ''
    else:
        parts = address_str.split(',')
        if len(parts) >= 3:
            addr_line1 = f"{parts[0].strip()}, {parts[1].strip()},"
            addr_line2 = ", ".join(p.strip() for p in parts[2:])
        elif len(parts) == 2:
            addr_line1 = parts[0].strip()
            addr_line2 = parts[1].strip()
        else:
            addr_line1 = address_str
            addr_line2 = ''

    inv_html = f"<b>INVOICE TO</b><br/>Attn: {attn_name}<br/><b>{primary_name}</b><br/>{addr_line1}<br/>{addr_line2}"
    if abn_str:
        inv_html += f"<br/><b>ABN</b> {abn_str}"
    if phone_str:
        inv_html += f"<br/><b>M</b> {phone_str}"

    deliv_html = f"<b>DELIVER TO</b><br/>Attn: {attn_name}<br/><b>{primary_name}</b><br/>{addr_line1}<br/>{addr_line2}"
    if abn_str:
        deliv_html += f"<br/><b>ABN</b> {abn_str}"
    if phone_str:
        deliv_html += f"<br/><b>M</b> {phone_str}"

    order_no = inspection_data.get('order_no') or client_data.get('vy_order_id') or '6A96646FEC2D1'
    salesperson = inspection_data.get('salesperson') or client_data.get('salesperson') or 'Lachlan North'
    
    title_html = (
        f"<b>CUSTOMER HANDOVER CHECKLIST</b><br/>"
        f"<font size='8'>Order # {order_no}<br/>"
        f"Salesperson: {salesperson}</font>"
    )

    inv_table = Table(
        [[Paragraph(inv_html, inv_deliv_reg), Paragraph(deliv_html, inv_deliv_reg), Paragraph(title_html, title_style)]],
        colWidths=[135, 135, page_width - 270]
    )
    inv_table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('LEFTPADDING', (0,0), (-1,-1), 0),
        ('RIGHTPADDING', (0,0), (-1,-1), 0),
        ('TOPPADDING', (0,0), (-1,-1), 0),
        ('BOTTOMPADDING', (0,0), (-1,-1), 0),
    ]))
    elements.append(inv_table)
    elements.append(Spacer(1, 12))

    # 3. Vehicle details card
    veh_model = client_data.get('vehicle') or inspection_data.get('vehicle') or 'BYD SEALION 7 Premium'
    rego_val = client_data.get('rego') or inspection_data.get('rego_stock') or client_data.get('vy_stock_id') or '13806'
    vin_val = client_data.get('vin') or inspection_data.get('vin') or 'LGXCH4CDXT2237263'

    veh_inner_1 = Table(
        [[Paragraph("Vehicle", veh_label_style), Paragraph(veh_model, veh_val_style)]],
        colWidths=[55, page_width - 55 - 12]
    )
    veh_inner_1.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('BACKGROUND', (1,0), (1,0), colors.HexColor('#FFFFFF')),
        ('BOX', (1,0), (1,0), 0.5, colors.HexColor('#DFDFDF')),
        ('TOPPADDING', (1,0), (1,0), 3),
        ('BOTTOMPADDING', (1,0), (1,0), 3),
        ('LEFTPADDING', (1,0), (1,0), 5),
        ('RIGHTPADDING', (1,0), (1,0), 5),
    ]))

    veh_inner_2 = Table(
        [
            [
                Paragraph("Rego/Stock#", veh_label_style),
                Paragraph(rego_val, veh_val_style),
                Paragraph("VIN", veh_label_style),
                Paragraph(vin_val, veh_val_style)
            ]
        ],
        colWidths=[65, 80, 28, page_width - (65 + 80 + 28) - 12]
    )
    veh_inner_2.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('BACKGROUND', (1,0), (1,0), colors.HexColor('#FFFFFF')),
        ('BOX', (1,0), (1,0), 0.5, colors.HexColor('#DFDFDF')),
        ('BACKGROUND', (3,0), (3,0), colors.HexColor('#FFFFFF')),
        ('BOX', (3,0), (3,0), 0.5, colors.HexColor('#DFDFDF')),
        ('TOPPADDING', (0,0), (-1,-1), 3),
        ('BOTTOMPADDING', (0,0), (-1,-1), 3),
        ('LEFTPADDING', (1,0), (1,0), 5),
        ('RIGHTPADDING', (1,0), (1,0), 5),
        ('LEFTPADDING', (3,0), (3,0), 5),
        ('RIGHTPADDING', (3,0), (3,0), 5),
    ]))

    veh_card = Table([[veh_inner_1], [veh_inner_2]], colWidths=[page_width])
    veh_card.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#EDEDED')),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ('LEFTPADDING', (0,0), (-1,-1), 6),
        ('RIGHTPADDING', (0,0), (-1,-1), 6),
    ]))
    elements.append(veh_card)
    elements.append(Spacer(1, 10))

    # 4. Checklist row builder
    def make_section_table(title: Optional[str], items: List[str]):
        table_rows = []
        if title:
            table_rows.append([Paragraph(f"<b>{title}</b>", section_hdr_style), Paragraph("", item_status_style)])
        for item_txt in items:
            table_rows.append([
                Paragraph(item_txt, item_lbl_style),
                Paragraph("Complete", item_status_style)
            ])
        t = Table(table_rows, colWidths=[page_width - 60, 60])
        t.setStyle(TableStyle([
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ('LEFTPADDING', (0,0), (-1,-1), 0),
            ('RIGHTPADDING', (0,0), (-1,-1), 0),
            ('TOPPADDING', (0,0), (-1,-1), 3.6),
            ('BOTTOMPADDING', (0,0), (-1,-1), 3.6),
        ]))
        return t

    # Page 1 Sections:
    elements.append(make_section_table("Prior to customer arrival", [
        "Car is clean inside and out"
    ]))
    elements.append(Spacer(1, 4))

    elements.append(make_section_table("With Customer", [
        "Welcome and congratulate customer on their new BYD",
        "Introduce yourself as a BYD delivery specialist",
        "Confirm all payments are made",
        "Confirm the customer has comprehensive insurance"
    ]))
    elements.append(Spacer(1, 4))

    elements.append(make_section_table("Exterior", [
        "Show customer how to access the car with keys, NFC card",
        "Show customer how to access the boot and explain electronic close, lock and set height",
        "Show customer included charging cable and how to use",
        "Show customer included V2L cable and how to use",
        "Show customer how to open bonnet and fill washer fluid"
    ]))
    elements.append(Spacer(1, 4))

    elements.append(make_section_table("Interior", [
        "Completely satisfied with the condition of interior",
        "Explanation of infotainment/navigation system/ wireless charger",
        "Explanation of safety features"
    ]))
    elements.append(Spacer(1, 4))

    elements.append(make_section_table("Seating", [
        "Show customer rear seats and split fold",
        "Show customer Isofix and tether points",
        "Show customer how to adjust driver and front passenger seats",
        "Show customer how to use sunroof and blind (if fitted)"
    ]))
    elements.append(Spacer(1, 4))

    elements.append(make_section_table("Technology", [
        "Explain wireless phone charging and advise against storing NFC cards, credit cards etc. between phone and wireless charging pads while in use.",
        "Pair Bluetooth and explain connection to Apple Car Play (USB cable) and Android Auto (wireless)",
        "Explain OTA update procedure and how the 2GB data limit does not apply to this"
    ]))

    # PAGE 2 BREAK
    elements.append(PageBreak())

    # Technology Part 2
    elements.append(make_section_table(None, [
        "Explain that SIM card activation and BYD app registration will be activated in the next couple of days.",
        "Explain \"Hi BYD\"",
        "Explain Digital and FM radio, and other entertainment features",
        "Explain vehicle controls (located on centre console, steering wheel etc.)",
        "Explain how to select gears (including Neutral)",
        "Explain automatic park brake engagement when in Park",
        "Ask permission to download the BYD app for the customer to their phone",
        "App features explained"
    ]))
    elements.append(Spacer(1, 6))

    elements.append(make_section_table("Driving", [
        "Explain lane keeping features",
        "Explain adaptive cruise control",
        "Explain wipers, blinkers and distance to empty gauge",
        "Explain 3 driving modes - Eco, Normal, Sport",
        "Explain regenerative braking and settings"
    ]))
    elements.append(Spacer(1, 6))

    # Accessories
    fitted_accs = inspection_data.get('fitted_accessories') or [a.get('name') for a in client_data.get('accessories', []) if a.get('name')] or [
        "Floor Mats Moulded (Deep Dish)", "Ceramic Window Tint (2x Front)"
    ]
    site_loc = client_data.get('site_location') or 'Fairfield'
    acc_items = ["Explain use and care of any accessories fitted"] + fitted_accs + [f"Pickup from BYD {site_loc}"]
    elements.append(make_section_table("Accessories", acc_items))
    elements.append(Spacer(1, 6))

    elements.append(make_section_table("Service and Support", [
        "Show service sticker, and explain how to book online",
        "Explain service intervals and details",
        "Show customer how to view Owner's manual online"
    ]))
    elements.append(Spacer(1, 6))

    elements.append(make_section_table("Battery Health", [
        "Discharging the vehicle to 10-20% State of Charge (SOC) at least once every three to six months, and then fully recharging to 100% using an AC charger.",
        "AC charging is preferable over DC charging, and one single charge up to 100% SOC is preferred over multiple smaller charges.",
        "If the vehicle is intended not to be used for 3 months or longer, keep the SOC between 40-60% to prevent the battery from over-discharging."
    ]))
    elements.append(Spacer(1, 6))

    elements.append(make_section_table("Customer Experience", [
        "Present gift in front of car and ask for permission to take photo of the customer and share on our platforms",
        "Ask for customers signature to take delivery of the vehicle",
        "Explain to the customer that they will receive a survey in their BYD app to share feedback in a month"
    ]))

    # PAGE 3 BREAK
    elements.append(PageBreak())

    elements.append(Paragraph("<b>Notes</b>", section_hdr_style))
    elements.append(Spacer(1, 4))
    
    notes_val = inspection_data.get('notes') or ''
    notes_cell = Paragraph(notes_val, item_lbl_style)
    notes_box = Table([[notes_cell]], colWidths=[page_width], rowHeights=[68])
    notes_box.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#FFFFFF')),
        ('BOX', (0,0), (-1,-1), 0.5, colors.HexColor('#DFDFDF')),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('LEFTPADDING', (0,0), (-1,-1), 6),
        ('RIGHTPADDING', (0,0), (-1,-1), 6),
        ('TOPPADDING', (0,0), (-1,-1), 6),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
    ]))
    elements.append(notes_box)
    
    # Space pushing declaration to bottom
    elements.append(Spacer(1, 565))

    decl_text = (
        "I hereby verify that the above items have been duly clarified to me and I have personally inspected and received "
        "the delivered vehicle. I acknowledge the condition I have received the vehicle in is as described by the below images."
    )
    elements.append(Paragraph(decl_text, decl_style))
    elements.append(Spacer(1, 10))

    # Signatures card
    date_val = inspection_data.get('customer_signature_date') or inspection_data.get('signature_date') or datetime.now().strftime('%d/%m/%Y')
    cust_sig = _decode_image(inspection_data.get('customer_signature'), max_w=120, max_h=36)
    spec_name = inspection_data.get('salesperson_name') or inspection_data.get('specialist_name') or client_data.get('handover_specialist') or 'Jamie-Lua Sofe'

    box_w = (page_width - 24) / 3

    col1_table = Table([[Paragraph(date_val, sig_val_style)], [Paragraph("Date", sig_lbl_style)]], colWidths=[box_w])
    col1_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (0,0), colors.HexColor('#FFFFFF')),
        ('BOX', (0,0), (0,0), 0.5, colors.HexColor('#DFDFDF')),
        ('ALIGN', (0,0), (0,0), 'CENTER'),
        ('VALIGN', (0,0), (0,0), 'MIDDLE'),
        ('ROWBACKGROUNDS', (0,0), (-1,-1), [colors.HexColor('#FFFFFF'), colors.transparent]),
        ('TOPPADDING', (0,0), (0,0), 12),
        ('BOTTOMPADDING', (0,0), (0,0), 12),
        ('TOPPADDING', (0,1), (0,1), 4),
    ]))

    col2_table = Table([[cust_sig or Paragraph("", sig_val_style)], [Paragraph("Signature of the Purchaser", sig_lbl_style)]], colWidths=[box_w])
    col2_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (0,0), colors.HexColor('#FFFFFF')),
        ('BOX', (0,0), (0,0), 0.5, colors.HexColor('#DFDFDF')),
        ('ALIGN', (0,0), (0,0), 'CENTER'),
        ('VALIGN', (0,0), (0,0), 'MIDDLE'),
        ('TOPPADDING', (0,0), (0,0), 3),
        ('BOTTOMPADDING', (0,0), (0,0), 3),
        ('TOPPADDING', (0,1), (0,1), 4),
    ]))

    col3_table = Table([[Paragraph(spec_name, sig_val_style)], [Paragraph("Salesperson", sig_lbl_style)]], colWidths=[box_w])
    col3_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (0,0), colors.HexColor('#FFFFFF')),
        ('BOX', (0,0), (0,0), 0.5, colors.HexColor('#DFDFDF')),
        ('ALIGN', (0,0), (0,0), 'CENTER'),
        ('VALIGN', (0,0), (0,0), 'MIDDLE'),
        ('TOPPADDING', (0,0), (0,0), 12),
        ('BOTTOMPADDING', (0,0), (0,0), 12),
        ('TOPPADDING', (0,1), (0,1), 4),
    ]))

    sig_card = Table([[col1_table, col2_table, col3_table]], colWidths=[box_w + 4, box_w + 4, box_w + 4])
    sig_card.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#F7F7F7')),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('TOPPADDING', (0,0), (-1,-1), 6),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ('LEFTPADDING', (0,0), (-1,-1), 4),
        ('RIGHTPADDING', (0,0), (-1,-1), 4),
    ]))
    elements.append(sig_card)

    # PAGE 4 BREAK (Photos Grid)
    elements.append(PageBreak())
    elements.append(Paragraph("<b>VEHICLE CONDITION UPON DELIVERY</b>", ParagraphStyle(
        'CondHeader',
        fontName='Helvetica-Bold',
        fontSize=10,
        leading=13,
        textColor=colors.HexColor('#000000'),
        spaceAfter=6
    )))

    photos = inspection_data.get('photos') or {}

    def get_photo_img(key_primary, key_alt):
        src = photos.get(key_primary) or photos.get(key_alt)
        return _decode_image(src, max_w=252, max_h=180)

    photo_col_w = (page_width - 8) / 2

    def make_photo_cell(title, img):
        card_content = [
            Paragraph(f"<b>{title}</b>", ParagraphStyle('PhotoHdr', fontName='Helvetica-Bold', fontSize=7.5, leading=9, textColor=colors.HexColor('#000000'))),
            Spacer(1, 2)
        ]
        if img:
            card_content.append(img)
        else:
            card_content.append(Table([[Paragraph("<font color='#888888'>[No photo uploaded]</font>", ParagraphStyle('NoPhoto', fontName='Helvetica', fontSize=7, alignment=1))]], colWidths=[photo_col_w - 8], rowHeights=[180]))
        
        t = Table([[c] for c in card_content], colWidths=[photo_col_w])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#F7F7F7')),
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ('TOPPADDING', (0,0), (-1,-1), 3),
            ('BOTTOMPADDING', (0,0), (-1,-1), 3),
            ('LEFTPADDING', (0,0), (-1,-1), 3),
            ('RIGHTPADDING', (0,0), (-1,-1), 3),
        ]))
        return t

    p_front = make_photo_cell("Front", get_photo_img('front', 'front_view'))
    p_driver = make_photo_cell("Drivers Side", get_photo_img('drivers_side', 'driver_side'))
    p_rear = make_photo_cell("Rear", get_photo_img('rear', 'rear_view'))
    p_pass = make_photo_cell("Passenger Side", get_photo_img('passenger_side', 'pass_side'))
    p_fuel = make_photo_cell("Fuel / Electricity Level", get_photo_img('fuel_electricity', 'fuel_or_charge'))
    p_env = make_photo_cell("Delivery Environment", get_photo_img('environment', 'bay_environment'))

    grid_p4 = Table(
        [
            [p_front, p_driver],
            [p_rear, p_pass],
            [p_fuel, p_env]
        ],
        colWidths=[photo_col_w + 4, photo_col_w + 4]
    )
    grid_p4.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('LEFTPADDING', (0,0), (-1,-1), 0),
        ('RIGHTPADDING', (0,0), (-1,-1), 0),
        ('TOPPADDING', (0,0), (-1,-1), 1),
        ('BOTTOMPADDING', (0,0), (-1,-1), 1),
    ]))
    elements.append(grid_p4)

    # PAGE 5 BREAK (Boot & Gift)
    elements.append(PageBreak())
    p_boot = make_photo_cell("Boot & Gift", get_photo_img('boot_gift', 'boot_and_gift'))
    grid_p5 = Table([[p_boot, Paragraph("", item_lbl_style)]], colWidths=[photo_col_w + 4, photo_col_w + 4])
    grid_p5.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('LEFTPADDING', (0,0), (-1,-1), 0),
        ('RIGHTPADDING', (0,0), (-1,-1), 0),
        ('TOPPADDING', (0,0), (-1,-1), 1),
        ('BOTTOMPADDING', (0,0), (-1,-1), 1),
    ]))
    elements.append(grid_p5)

    doc.build(elements)
    return buffer.getvalue()
