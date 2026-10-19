from pathlib import Path
import json
import os
import random
from PIL import Image, ImageDraw, ImageFont, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(os.environ["FORM_LAB_DATA_DIR"]) / "sample" if os.environ.get("FORM_LAB_DATA_DIR") else ROOT / "data" / "sample"
OUT.mkdir(parents=True, exist_ok=True)
FONT = next((path for path in ("/System/Library/Fonts/Supplemental/Arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf") if Path(path).exists()), None)
font = ImageFont.truetype(FONT, 25) if FONT else ImageFont.load_default()
large = ImageFont.truetype(FONT, 37) if FONT else ImageFont.load_default()

for n, values in enumerate([
    {"full_name":"Ada Lovelace", "date":"16 Oct 2026", "reference":"REF-1042", "contact_method":"email", "priority":"routine"},
    {"full_name":"Grace Hopper", "date":"20 Oct 2026", "reference":"REF-2048", "contact_method":"phone", "priority":"urgent"},
    {"full_name":"Alan Turing", "date":"05 Nov 2026", "reference":"REF-3021", "contact_method":"post", "priority":"routine"},
], 1):
    image = Image.new("RGB", (850, 1100), "#fcfbf7")
    draw = ImageDraw.Draw(image)
    draw.rectangle((26,26,824,1074), outline="#c8c6bf", width=3)
    draw.text((65,68), "INTAKE / 01", font=large, fill="#17292d")
    draw.text((66,135), "Please complete each field clearly.", font=font, fill="#606b69")
    for label, y, key in [("Full name",218,"full_name"),("Date",297,"date"),("Reference",377,"reference")]:
        draw.text((65,y+8),label,font=font,fill="#34464a")
        draw.line((185,y+46,760,y+46),fill="#a4aaa7",width=2)
        draw.text((194,y+5),values[key],font=font,fill="#1d3445")
    draw.text((65,455),"Preferred contact",font=font,fill="#34464a")
    draw.text((65,575),"Priority",font=font,fill="#34464a")
    for y, items in [(490,[(192,"email","Email"),(425,"phone","Phone"),(630,"post","Post")]),(610,[(192,"routine","Routine"),(425,"urgent","Urgent")])]:
        target = values["contact_method"] if y==490 else values["priority"]
        for x, key, label in items:
            draw.rectangle((x,y,x+28,y+28),outline="#48585b",width=2)
            draw.text((x+39,y-2),label,font=font,fill="#34464a")
            if key==target:
                draw.line((x+5,y+13,x+11,y+21,x+24,y+4),fill="#213c55",width=4,joint="curve")
    draw.text((65,745),"Reviewer notes",font=font,fill="#34464a")
    draw.line((65,815,760,815),fill="#a4aaa7",width=2)
    if n==3:
        image = image.rotate(1.4, fillcolor="white")
        image = image.filter(ImageFilter.GaussianBlur(0.5))
    pdf = OUT / f"intake-{n}.pdf"
    image.save(pdf,"PDF",resolution=150)
    (OUT / f"intake-{n}.json").write_text(json.dumps({"template":"intake","source":"synthetic", "pages":[{"number":1,"fields":values}]},indent=2))
print(f"Created 3 labeled scanned PDFs in {OUT}")
