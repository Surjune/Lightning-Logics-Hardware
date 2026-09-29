"""Generates hardware/schematic.svg for the sonar transmitter analog front end.

    python hardware/tools/make_schematic.py

Edit the part values or layout here and re-run; the SVG is written next to the README.
"""

from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "schematic.svg"
INK, SOFT, TP, NET = "#1b2a38", "#4a5a68", "#c8561e", "#0e7c7b"
W, H = 1880, 940
parts = []


def add(s):
    parts.append(s)


def text(x, y, s, size=15, anchor="start", weight="normal", color=INK):
    add(f'<text x="{x}" y="{y}" font-size="{size}" text-anchor="{anchor}" font-weight="{weight}" fill="{color}">{s}</text>')


def wire(*pts):
    add('<polyline fill="none" stroke="%s" stroke-width="2" points="%s"/>' % (INK, " ".join(f"{x},{y}" for x, y in pts)))


def dot(x, y):
    add(f'<circle cx="{x}" cy="{y}" r="4.5" fill="{INK}"/>')


def res_h(x1, x2, y, ref, val):
    c = (x1 + x2) / 2
    wire((x1, y), (c - 30, y)); wire((c + 30, y), (x2, y))
    add(f'<rect x="{c-30}" y="{y-10}" width="60" height="20" fill="#ffffff" stroke="{INK}" stroke-width="2"/>')
    text(c, y - 17, ref, 14, "middle", "bold"); text(c, y + 30, val, 14, "middle")


def res_v(x, y1, y2, ref, val, side="right"):
    c = (y1 + y2) / 2
    wire((x, y1), (x, c - 30)); wire((x, c + 30), (x, y2))
    add(f'<rect x="{x-10}" y="{c-30}" width="20" height="60" fill="#ffffff" stroke="{INK}" stroke-width="2"/>')
    dx, anchor = (18, "start") if side == "right" else (-18, "end")
    text(x + dx, c - 4, ref, 14, anchor, "bold"); text(x + dx, c + 14, val, 14, anchor)


def cap_h(x1, x2, y, ref, val, labels="above", polar=False):
    c = (x1 + x2) / 2
    wire((x1, y), (c - 5, y)); wire((c + 5, y), (x2, y))
    add(f'<line x1="{c-5}" y1="{y-14}" x2="{c-5}" y2="{y+14}" stroke="{INK}" stroke-width="3"/>')
    add(f'<line x1="{c+5}" y1="{y-14}" x2="{c+5}" y2="{y+14}" stroke="{INK}" stroke-width="3"/>')
    if polar:
        text(c - 14, y - 16, "+", 14, "middle", "bold")
    if labels == "above":
        text(c, y - 22, f"{ref}  {val}", 14, "middle")
    else:
        text(c, y + 34, f"{ref}  {val}", 14, "middle")


def cap_v(x, y1, y2, ref, val, side="right"):
    c = (y1 + y2) / 2
    wire((x, y1), (x, c - 5)); wire((x, c + 5), (x, y2))
    add(f'<line x1="{x-14}" y1="{c-5}" x2="{x+14}" y2="{c-5}" stroke="{INK}" stroke-width="3"/>')
    add(f'<line x1="{x-14}" y1="{c+5}" x2="{x+14}" y2="{c+5}" stroke="{INK}" stroke-width="3"/>')
    dx, anchor = (20, "start") if side == "right" else (-20, "end")
    text(x + dx, c - 2, ref, 14, anchor, "bold"); text(x + dx, c + 15, val, 14, anchor)


def gnd(x, y):
    wire((x, y), (x, y + 8))
    for i, w in enumerate((24, 16, 8)):
        add(f'<line x1="{x-w/2}" y1="{y+8+i*5}" x2="{x+w/2}" y2="{y+8+i*5}" stroke="{INK}" stroke-width="2"/>')


def opamp(x, yp, name, pins):
    """+ input at (x, yp), - input at (x, yp+50), output at (x+100, yp+25)."""
    add(f'<polygon points="{x},{yp-25} {x},{yp+75} {x+100},{yp+25}" fill="#ffffff" stroke="{INK}" stroke-width="2"/>')
    text(x + 9, yp + 6, "+", 18, "start", "bold"); text(x + 10, yp + 56, "−", 18, "start", "bold")
    text(x + 36, yp + 31, name, 14, "start", "bold")
    p, n, o = pins
    text(x - 4, yp - 5, p, 12, "end", color=SOFT); text(x - 4, yp + 45, n, 12, "end", color=SOFT)
    text(x + 104, yp + 18, o, 12, "start", color=SOFT)


def testpoint(x, y, label, above=True):
    add(f'<circle cx="{x}" cy="{y}" r="7" fill="#ffffff" stroke="{TP}" stroke-width="2.5"/>')
    text(x, y - 14 if above else y + 26, label, 14, "middle", "bold", TP)


def netlabel(x, y, s, anchor="middle"):
    text(x, y, s, 14, anchor, "bold", NET)


def unity_feedback(x, yp, xo, down=45, left=15):
    """- input (x, yp+50) looped under the op-amp to the output node (xo, yp+25)."""
    wire((x, yp + 50), (x - left, yp + 50), (x - left, yp + 50 + down), (xo, yp + 50 + down), (xo, yp + 25))


# ---- title ---------------------------------------------------------------------------------------
text(30, 42, "Sonar transmitter analog front end", 26, weight="bold")
text(30, 70, "One quad rail-to-rail op-amp on +5 V (U1 = OPA4350, alt. MCP6294). DAC 2 MSPS → buffer → "
     "4th-order Butterworth low-pass, 600 kHz → driver → dummy load", 15, color=SOFT)

# ---- signal path ------------------------------------------------------------------------------------
text(30, 292, "ESP32", 16, weight="bold"); text(30, 312, "GPIO25 (DAC)", 14, color=SOFT)
wire((130, 300), (200, 300)); testpoint(160, 300, "TP1")

# buffer U1A
text(250, 240, "Buffer", 15, "middle", "bold", SOFT)
opamp(200, 300, "U1A", ("3", "2", "1"))
wire((300, 325), (360, 325)); dot(320, 325)
unity_feedback(200, 300, 320, down=45, left=20)
testpoint(340, 325, "TP2")

# stage 1
text(360, 180, "Stage 1 · Sallen-Key, f0 602 kHz, Q 0.54", 15, weight="bold", color=SOFT)
res_h(360, 460, 325, "R1", "1.30 kΩ")
wire((460, 325), (480, 325)); dot(480, 325)
res_h(480, 570, 325, "R2", "511 Ω")
wire((570, 325), (640, 325)); dot(590, 325)
cap_v(590, 325, 440, "C2", "270 pF", side="left"); gnd(590, 440)
wire((480, 325), (480, 240)); cap_h(480, 760, 240, "C1", "390 pF"); wire((760, 240), (760, 350))
opamp(640, 325, "U1B", ("5", "6", "7"))
wire((740, 350), (790, 350)); dot(760, 350)
unity_feedback(640, 325, 760, down=45, left=15)

# stage 2
text(790, 180, "Stage 2 · Sallen-Key, f0 604 kHz, Q 1.31", 15, weight="bold", color=SOFT)
res_h(790, 890, 350, "R3", "1.74 kΩ")
wire((890, 350), (910, 350)); dot(910, 350)
res_h(910, 1000, 350, "R4", "715 Ω")
wire((1000, 350), (1070, 350)); dot(1020, 350)
cap_v(1020, 350, 465, "C4", "82 pF", side="left"); gnd(1020, 465)
wire((910, 350), (910, 265)); cap_h(910, 1190, 265, "C3", "680 pF"); wire((1190, 265), (1190, 375))
opamp(1070, 350, "U1C", ("10", "9", "8"))
wire((1170, 375), (1230, 375)); dot(1190, 375)
unity_feedback(1070, 350, 1190, down=45, left=15)
testpoint(1212, 375, "TP3")

# driver U1D: AC-coupled non-inverting, biased at VMID, gain 1 + R7/R6
text(1230, 180, "Driver · gain 1.51, AC-coupled", 15, weight="bold", color=SOFT)
cap_h(1230, 1290, 375, "C5", "1 µF", labels="below")
wire((1290, 375), (1360, 375)); dot(1310, 375)
res_v(1310, 375, 275, "R5", "100 kΩ", side="left"); netlabel(1310, 262, "VMID")
opamp(1360, 375, "U1D", ("12", "13", "14"))
wire((1360, 425), (1340, 425), (1340, 480)); dot(1340, 480)
res_v(1340, 480, 590, "R6", "10 kΩ", side="left"); netlabel(1340, 608, "VMID")
wire((1340, 480), (1375, 480)); res_h(1375, 1485, 480, "R7", "5.1 kΩ"); wire((1485, 480), (1485, 400))
wire((1460, 400), (1500, 400)); dot(1485, 400)
res_h(1500, 1590, 400, "R8", "100 Ω")
cap_h(1590, 1660, 400, "C6", "10 µF", labels="below", polar=True)
wire((1660, 400), (1750, 400)); dot(1690, 400)
res_v(1690, 400, 520, "R9", "1 kΩ load"); gnd(1690, 520)
testpoint(1750, 400, "TP4")
text(1766, 395, "OUT", 15, weight="bold"); text(1766, 414, "scope CH1", 13, color=SOFT)

# ---- supply and inputs ---------------------------------------------------------------------------------
add(f'<line x1="30" y1="650" x2="{W-30}" y2="650" stroke="#c9d1cc" stroke-width="2" stroke-dasharray="8 6"/>')
text(30, 685, "Supply, mid-rail bias and inputs", 17, weight="bold", color=SOFT)

text(40, 734, "ESP32 5V (V5) pin", 14, weight="bold")
wire((180, 730), (720, 730)); netlabel(730, 735, "+5V", "start")
text(40, 790, "U1 pin 4 = +5V", 14); text(40, 810, "U1 pin 11 = GND", 14)
dot(260, 730); cap_v(260, 730, 840, "C9", "100 nF", side="right"); gnd(260, 840)
text(236, 875, "at U1 pin 4", 12, color=SOFT)
dot(380, 730); cap_v(380, 730, 840, "C10", "10 µF", side="right"); gnd(380, 840)

dot(520, 730); res_v(520, 730, 800, "R10", "10 kΩ", side="left")
dot(520, 800); res_v(520, 800, 870, "R11", "10 kΩ", side="left"); gnd(520, 870)
wire((520, 800), (660, 800)); dot(590, 800)
cap_v(590, 800, 870, "C7", "10 µF", side="right"); gnd(590, 870)
cap_v(660, 800, 870, "C8", "100 nF", side="right"); gnd(660, 870)
wire((660, 800), (720, 800)); netlabel(730, 805, "VMID = 2.5 V", "start")

text(900, 734, "ESP32 3V3 pin", 14, weight="bold")
wire((1010, 730), (1330, 730)); dot(1050, 730); dot(1270, 730)
for x, ref, pin, what in ((1050, "RV1", "GPIO34", "turbidity"), (1270, "RV2", "GPIO35", "reach")):
    wire((x, 730), (x, 760))
    add(f'<rect x="{x-10}" y="760" width="20" height="70" fill="#ffffff" stroke="{INK}" stroke-width="2"/>')
    wire((x, 830), (x, 850)); gnd(x, 850)
    add(f'<polygon points="{x+12},795 {x+24},789 {x+24},801" fill="{INK}"/>')
    wire((x + 24, 795), (x + 60, 795))
    text(x - 18, 790, ref, 14, "end", "bold"); text(x - 18, 808, "10 kΩ lin", 14, "end")
    text(x + 66, 791, pin, 14, weight="bold"); text(x + 66, 809, what, 13, color=SOFT)

text(1480, 734, "ESP32 GPIO27 (T0 marker)", 14, weight="bold")
wire((1480, 760), (1700, 760)); testpoint(1700, 760, "TP5")
text(1716, 765, "scope CH2", 13, color=SOFT)
text(1480, 820, "Common ground: ESP32 GND, U1 pin 11", 14, color=SOFT)
text(1480, 840, "and every scope ground clip.", 14, color=SOFT)

svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
       f'font-family="Helvetica, Arial, sans-serif">\n<rect width="{W}" height="{H}" fill="#ffffff"/>\n'
       + "\n".join(parts) + "\n</svg>\n")
open(OUT, "w", encoding="utf-8").write(svg)
print("wrote", OUT, len(svg), "bytes")
