"""Deterministic, one-page synthetic bank form for layout and OCR testing."""
from __future__ import annotations

import io
import json
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SIZE = (850, 1100)
HAND_FONT = "/System/Library/Fonts/Supplemental/Bradley Hand Bold.ttf"
PRINT_FONT = "/System/Library/Fonts/Supplemental/Arial.ttf"
TEXT = [
    ("full_name", "Full legal name", (48, 166, 510, 207)),
    ("date_of_birth", "Date of birth", (530, 166, 802, 207)),
    ("parent_name", "Parent / spouse name", (48, 225, 510, 266)),
    ("mobile", "Mobile number", (530, 225, 802, 266)),
    ("email", "Email address", (48, 284, 510, 325)),
    ("tax_id", "Tax ID / PAN", (530, 284, 802, 325)),
    ("address", "Residential address", (48, 343, 802, 384)),
    ("city", "City / town", (48, 402, 294, 443)),
    ("postal_code", "Postal code", (312, 402, 536, 443)),
    ("occupation", "Occupation", (554, 402, 802, 443)),
    ("branch", "Preferred branch", (48, 646, 510, 687)),
    ("initial_deposit", "Initial deposit", (530, 646, 802, 687)),
    ("nominee", "Nominee full name", (48, 886, 510, 927)),
    ("nominee_relation", "Relationship", (530, 886, 802, 927)),
    ("signature", "Applicant signature", (48, 972, 510, 1013)),
    ("signed_date", "Date signed", (530, 972, 802, 1013)),
]
CHOICES = [
    ("account_type", "ACCOUNT TYPE", 493, [("savings", "Savings", 265), ("current", "Current", 425), ("salary", "Salary", 575), ("student", "Student", 715)]),
    ("ownership", "OPERATING MODE", 543, [("single", "Single", 265), ("joint", "Joint", 425), ("either", "Either / survivor", 575)]),
    ("kyc_document", "ID ATTACHED", 593, [("passport", "Passport", 265), ("national_id", "National ID", 425), ("driving_licence", "Driving licence", 575)]),
    ("debit_card", "DEBIT CARD", 735, [("yes", "Yes", 265), ("no", "No", 425)]),
    ("statements", "STATEMENTS", 785, [("email", "Email", 265), ("post", "Post", 425), ("both", "Both", 575)]),
    ("alerts", "ACCOUNT ALERTS", 835, [("sms", "SMS", 265), ("email", "Email", 425), ("none", "None", 575)]),
    ("declaration", "DECLARATION", 945, [("agree", "Agree", 490), ("decline", "Decline", 630)]),
]
PRESETS = [
    {"full_name":"Maya Desai","date_of_birth":"14 Mar 1992","parent_name":"Ravi Desai","mobile":"98765 42018","email":"maya.desai@example.test","tax_id":"ABCPD2146K","address":"24 Lake View Road, Flat 3B","city":"Pune","postal_code":"411001","occupation":"Architect","branch":"Camp, Pune","initial_deposit":"15,000","nominee":"Isha Desai","nominee_relation":"Sister","signature":"Maya Desai","signed_date":"16 Oct 2026","account_type":"savings","ownership":"single","kyc_document":"passport","debit_card":"yes","statements":"email","alerts":"sms","declaration":"agree"},
    {"full_name":"Arjun Mehta","date_of_birth":"07 Aug 1988","parent_name":"Neela Mehta","mobile":"98210 33764","email":"arjun.mehta@example.test","tax_id":"BCQPM7824R","address":"18 Cedar Street, Unit 204","city":"Mumbai","postal_code":"400001","occupation":"Consultant","branch":"Fort, Mumbai","initial_deposit":"25,500","nominee":"Neela Mehta","nominee_relation":"Mother","signature":"Arjun Mehta","signed_date":"18 Oct 2026","account_type":"current","ownership":"joint","kyc_document":"national_id","debit_card":"no","statements":"both","alerts":"email","declaration":"agree"},
    {"full_name":"Leena Shah","date_of_birth":"22 Jan 1999","parent_name":"Kiran Shah","mobile":"98900 62145","email":"leena.shah@example.test","tax_id":"CDHPS3008F","address":"7 Palm Avenue, House 12","city":"Nagpur","postal_code":"440001","occupation":"Teacher","branch":"Civil Lines, Nagpur","initial_deposit":"8,750","nominee":"Kiran Shah","nominee_relation":"Father","signature":"Leena Shah","signed_date":"21 Oct 2026","account_type":"salary","ownership":"either","kyc_document":"driving_licence","debit_card":"yes","statements":"post","alerts":"none","declaration":"agree"},
]


def template():
    fields = [{"name": key, "label": label, "kind": "text", "region": [box[0], box[1]+15, box[2], box[3]-2], "category": "handwriting"} for key, label, box in TEXT]
    for key, label, y, options in CHOICES:
        fields.append({"name": key, "label": label, "kind": "choice", "options": [{"value": value, "label": title, "region": [x, y, x+22, y+22]} for value, title, x in options]})
    return {"id": "bank_account", "name": "Bank account application · dense", "page_size": list(SIZE), "pages": [{"fields": fields}]}


def validate(values):
    allowed = {key for key, _, _ in TEXT} | {key for key, _, _, _ in CHOICES}
    if set(values) != allowed:
        raise ValueError("Provide every bank form field exactly once")
    cleaned = {}
    for key, _, _ in TEXT:
        value = str(values[key]).strip()
        if not value or len(value) > 55 or any(ord(c) < 32 for c in value):
            raise ValueError(f"Invalid value for {key}")
        cleaned[key] = value
    for key, _, _, options in CHOICES:
        if values[key] not in {option[0] for option in options}:
            raise ValueError(f"Invalid selection for {key}")
        cleaned[key] = values[key]
    return cleaned


def render(values, seed=0):
    values = validate(values)
    scale = 2
    image = Image.new("RGB", (SIZE[0]*scale, SIZE[1]*scale), "#fbfaf6")
    draw = ImageDraw.Draw(image)
    def font(path, size):
        for candidate in (path, "DejaVuSans.ttf"):
            try:
                return ImageFont.truetype(candidate, size*scale)
            except OSError:
                continue
        return ImageFont.load_default(size=size*scale)
    def line(box, fill="#b9bfba", width=1):
        draw.line(tuple(round(v*scale) for v in box), fill=fill, width=width*scale)
    def rectangle(box, **kwargs):
        draw.rectangle(tuple(round(v*scale) for v in box), **kwargs)
    def text(x, y, value, size=12, color="#243b38", face=PRINT_FONT):
        draw.text((x*scale,y*scale), value, font=font(face,size), fill=color)
    randomizer = random.Random(seed)
    rectangle((24,24,826,1076), outline="#53746a", width=2*scale)
    rectangle((24,24,826,111), fill="#e9efeb")
    text(48,37,"NORTHFIELD BANK",20,"#173c33")
    text(49,75,"ACCOUNT OPENING APPLICATION  /  INDIVIDUAL",10,"#49665d")
    text(700,42,"FORM  A-01",11,"#173c33")
    text(695,76,"FOR OFFICE USE",9,"#697b73")
    text(48,119,"Complete each field clearly. Mark one box in each row.",11,"#596b63")
    def section(y, number, title):
        rectangle((36,y,814,y+23), fill="#dfe8e2")
        text(48,y+4,number,10,"#376652")
        text(81,y+3,title,11,"#173c33")
    section(140,"01","APPLICANT DETAILS")
    section(460,"02","ACCOUNT & IDENTITY")
    section(701,"03","SERVICE OPTIONS")
    section(862,"04","NOMINEE & DECLARATION")
    for key, label, (x1,y1,x2,y2) in TEXT:
        text(x1,y1+1,label.upper(),9,"#68776f")
        line((x1,y2,x2,y2),"#9fac9f")
        value = values[key]
        handwriting_size = 16 if len(value)<29 else 14
        hand = font(HAND_FONT if Path(HAND_FONT).exists() else PRINT_FONT, handwriting_size)
        max_width = (x2-x1-12)*scale
        while draw.textbbox((0,0),value,font=hand)[2] > max_width and handwriting_size > 10:
            handwriting_size -= 1
            hand = font(HAND_FONT if Path(HAND_FONT).exists() else PRINT_FONT, handwriting_size)
        if draw.textbbox((0,0),value,font=hand)[2] > max_width:
            raise ValueError(f"{label} is too long for its box")
        draw.text(((x1+5)*scale,(y1+17+randomizer.choice([-1,0,1]))*scale),value,font=hand,fill="#243e61")
    for key, label, y, options in CHOICES:
        text(48,y+3,label,10,"#344d43")
        for value, title, x in options:
            rectangle((x,y,x+22,y+22),outline="#657b70",width=2*scale)
            text(x+28,y+3,title,10,"#345047")
            if values[key] == value:
                points=[((x+4)*scale,(y+10)*scale),((x+9)*scale,(y+17)*scale),((x+20)*scale,(y+3)*scale)]
                draw.line(points,fill="#233d60",width=3*scale,joint="curve")
    text(48,932,"I confirm the information above is accurate and agree to account terms.",9,"#5d6d64")
    line((36,1033,814,1033),"#b9c7ba")
    text(48,1041,"SYNTHETIC TEST DOCUMENT  ·  NO REAL CUSTOMER DATA",9,"#7b8c81")
    text(754,1041,"1 / 1",9,"#7b8c81")
    return image


def export(values, seed=0):
    image = render(values, seed)
    png = io.BytesIO(); image.save(png, format="PNG")
    pdf = io.BytesIO(); image.save(pdf, format="PDF", resolution=150)
    truth = {"template":"bank_account","source":"synthetic_handwriting_style","pages":[{"number":1,"fields":validate(values)}]}
    return png.getvalue(), pdf.getvalue(), json.dumps(truth,indent=2).encode()
