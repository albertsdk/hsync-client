#!/usr/bin/env python3
import math
import os

from PIL import Image, ImageDraw, ImageFont

W, H = 600, 467
FPS = 12
TOTAL = 29.0
NODE_R = 29

REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

NODES = {
    "A": (300, 118),
    "B": (133, 340),
    "C": (467, 340),
}

N_FILL = {
    "A": (40, 76, 44),
    "B": (33, 56, 82),
    "C": (62, 40, 74),
}
N_STROKE = {
    "A": (129, 199, 132),
    "B": (110, 178, 236),
    "C": (206, 147, 216),
}
PKT_COLOR = {
    "A": (129, 199, 132),
    "B": (110, 178, 236),
    "C": (206, 147, 216),
}

OFF_FILL = (70, 70, 72)
OFF_STROKE = (150, 153, 158)
OFF_BG = (96, 44, 42)
OFF_TXT = (255, 120, 115)

BG = (68, 68, 68)
EDGE_ACTIVE = (170, 178, 190)
EDGE_FAINT = (108, 114, 122)
EDGE_DOWN = (84, 88, 94)
CAP_BAR = (60, 64, 70)
CAP_BORDER = (96, 102, 112)
CAP_NUM = (200, 208, 220)
CAP_NUM_TXT = (40, 44, 50)
TXT = (235, 238, 242)
SUB = (176, 182, 190)
GOLD = (255, 193, 7)
GOLD_DK = (80, 52, 0)
PKT_BG = (255, 255, 255)
PROG_TRACK = (96, 100, 108)
PROG_FILL = (150, 176, 220)

PHASES = [
    ("1", 2.5, "Three peers A, B, C discovered on the network"),
    ("2", 2.5, "Peer A is elected master (sync coordinator)"),
    ("3", 5.5, "B and C exchange data through A - A relays to the other peer"),
    ("4", 2.0, "A goes offline / leaves the network"),
    ("5", 2.5, "B is elected as the new master"),
    ("6", 5.5, "C exchanges data with B, and vice versa"),
    ("7", 3.0, "A comes back online - B stays master"),
    ("8", 5.5, "A resumes syncing through B - every change reaches all peers"),
]

P_START = []
_acc = 0.0
for _n, _d, _t in PHASES:
    P_START.append(_acc)
    _acc += _d
P2_START = P_START[1]
P3_START = P_START[2]
P4_START = P_START[3]
P5_START = P_START[4]
P6_START = P_START[5]
P7_START = P_START[6]
P8_START = P_START[7]

A_OFF_START = P4_START
A_OFF_END = P7_START + 1.2
A_REJOIN_END = A_OFF_END + 0.5


def master_at(t):
    if t < P2_START:
        return None
    if t < P4_START:
        return "A"
    if t < P5_START:
        return None
    return "B"


def offline_set(t):
    s = set()
    if A_OFF_START <= t < A_REJOIN_END:
        s.add("A")
    return s


def online_alpha(node, t):
    if node != "A":
        return 1.0
    if t < A_OFF_START:
        return 1.0
    if t < A_OFF_END:
        return 0.0
    if t < A_REJOIN_END:
        return (t - A_OFF_END) / (A_REJOIN_END - A_OFF_END)
    return 1.0


def master_scale(t):
    m = master_at(t)
    if m is None:
        return 0.0
    since = P2_START if m == "A" else P5_START
    return min(1.0, max(0.0, (t - since) / 0.4))


EVENTS = []


def chain(steps, color, start, per=0.6, gap=0.6):
    t = start
    for src, dst in steps:
        EVENTS.append({"start": t, "dur": per, "src": src, "dst": dst, "color": color})
        t += gap


for _i in range(3):
    chain([("B", "A"), ("A", "C")], PKT_COLOR["B"], P3_START + _i * 1.7)
    chain([("C", "A"), ("A", "B")], PKT_COLOR["C"], P3_START + 0.85 + _i * 1.7)

for _i in range(3):
    chain([("C", "B")], PKT_COLOR["C"], P6_START + _i * 1.7)
    chain([("B", "C")], PKT_COLOR["B"], P6_START + 0.85 + _i * 1.7)

chain([("A", "B")], PKT_COLOR["A"], A_REJOIN_END)
chain([("B", "A")], PKT_COLOR["B"], A_REJOIN_END + 0.6)

for _i in range(3):
    chain([("A", "B"), ("B", "C")], PKT_COLOR["A"], P8_START + _i * 1.7)
    chain([("C", "B"), ("B", "A")], PKT_COLOR["C"], P8_START + 0.85 + _i * 1.7)


def load_fonts():
    return {
        "title": ImageFont.truetype(BOLD, 18),
        "sub": ImageFont.truetype(REG, 12),
        "node": ImageFont.truetype(BOLD, 26),
        "badge": ImageFont.truetype(BOLD, 12),
        "offline": ImageFont.truetype(BOLD, 11),
        "capn": ImageFont.truetype(BOLD, 15),
        "cap": ImageFont.truetype(REG, 14),
    }


def lerp(c1, c2, a):
    return tuple(int(c1[i] + (c2[i] - c1[i]) * a) for i in range(3))


def center_text(d, cx, cy, text, font, fill):
    lx, t_, r, b = d.textbbox((0, 0), text, font=font)
    w, h = r - lx, b - t_
    d.text((cx - w / 2 - lx, cy - h / 2 - t_), text, font=font, fill=fill)


def pill(d, cx, cy, text, font, fill, txt, scale=1.0):
    if scale <= 0.02:
        return
    lx, t_, r, b = d.textbbox((0, 0), text, font=font)
    w = (r - lx + 20) * scale
    h = (b - t_ + 10) * scale
    x0, y0, x1, y1 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
    d.rounded_rectangle((x0, y0, x1, y1), radius=h / 2, fill=fill, outline=txt, width=1)
    center_text(d, cx, cy, text, font, txt)


def edge_surface(src, dst):
    x1, y1 = NODES[src]
    x2, y2 = NODES[dst]
    dx, dy = x2 - x1, y2 - y1
    dist = math.hypot(dx, dy)
    ux, uy = dx / dist, dy / dist
    return (x1 + ux * NODE_R, y1 + uy * NODE_R, x2 - ux * NODE_R, y2 - uy * NODE_R, ux, uy)


def dashed_line(d, p1, p2, color, width, dash=10, gap=7):
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    dist = math.hypot(dx, dy)
    if dist == 0:
        return
    ux, uy = dx / dist, dy / dist
    n = int(dist / (dash + gap)) + 1
    for i in range(n):
        a = i * (dash + gap)
        b = min(a + dash, dist)
        d.line(
            (p1[0] + ux * a, p1[1] + uy * a, p1[0] + ux * b, p1[1] + uy * b),
            fill=color,
            width=width,
        )


def draw_edges(d, t):
    m = master_at(t)
    off = offline_set(t)
    for a, b in (("A", "B"), ("A", "C"), ("B", "C")):
        px, py, qx, qy, _, _ = edge_surface(a, b)
        if a in off or b in off:
            dashed_line(d, (px, py), (qx, qy), EDGE_DOWN, 2)
            continue
        if m is not None and (a == m or b == m):
            d.line((px, py, qx, qy), fill=EDGE_ACTIVE, width=3)
        elif m is not None:
            dashed_line(d, (px, py), (qx, qy), EDGE_FAINT, 2)
        else:
            d.line((px, py, qx, qy), fill=EDGE_FAINT, width=2)


def draw_node(d, node, t, F):
    cx, cy = NODES[node]
    a = online_alpha(node, t)
    fill = lerp(OFF_FILL, N_FILL[node], a)
    stroke = lerp(OFF_STROKE, N_STROKE[node], a)
    d.ellipse(
        (cx - NODE_R, cy - NODE_R, cx + NODE_R, cy + NODE_R), fill=fill, outline=stroke, width=3
    )
    center_text(d, cx, cy, node, F["node"], stroke if a > 0.5 else OFF_STROKE)


def draw_master(d, t, F):
    m = master_at(t)
    if m is None:
        return
    cx, cy = NODES[m]
    s = master_scale(t)
    if s < 0.05:
        return
    pulse = 2.0 * s * math.sin(2 * math.pi * t / 1.5)
    rr = NODE_R * 1.16 + pulse
    w = max(1.0, 3.0 * s)
    d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), outline=GOLD, width=int(round(w)))
    acx, acy = {
        "A": (cx, cy - NODE_R - 18),
        "B": (cx - NODE_R - 36, cy),
        "C": (cx + NODE_R + 36, cy),
    }[m]
    pill(d, acx, acy, "MASTER", F["badge"], GOLD, GOLD_DK, scale=s)


def draw_offline(d, t, F):
    off = offline_set(t)
    for node in off:
        cx, cy = NODES[node]
        a = online_alpha(node, t)
        if a >= 1.0:
            return
        acx = cx
        acy = cy - NODE_R - 18
        pill(d, acx, acy, "OFFLINE", F["offline"], OFF_BG, OFF_TXT)


def draw_packets(d, t):
    for ev in EVENTS:
        s = ev["start"]
        if not (s <= t <= s + ev["dur"]):
            continue
        px, py, qx, qy, ux, uy = edge_surface(ev["src"], ev["dst"])
        frac = (t - s) / ev["dur"]
        x = px + (qx - px) * frac
        y = py + (qy - py) * frac
        tf = max(0.0, frac - 0.14)
        tx = px + (qx - px) * tf
        ty = py + (qy - py) * tf
        col = ev["color"]
        d.line((tx, ty, x, y), fill=col, width=3)
        ah = 5.0
        hx = x - ux * ah
        hy = y - uy * ah
        nx, ny = -uy, ux
        la = (hx + nx * ah * 0.55, hy + ny * ah * 0.55)
        ra = (hx - nx * ah * 0.55, hy - ny * ah * 0.55)
        d.polygon([(x, y), la, ra], fill=col)
        d.ellipse((x - 6, y - 6, x + 6, y + 6), fill=PKT_BG, outline=col, width=2)


def phase_index(t):
    for i, (_, _, _) in enumerate(PHASES):
        if P_START[i] <= t < P_START[i] + PHASES[i][1]:
            return i
    return len(PHASES) - 1


def draw_caption(d, t, F):
    i = phase_index(t)
    num, _, txt = PHASES[i]
    y0, y1 = H - 84, H - 24
    d.rounded_rectangle((30, y0, W - 30, y1), radius=12, fill=CAP_BAR, outline=CAP_BORDER, width=1)
    ncx = 58
    ncy = (y0 + y1) / 2
    d.ellipse((ncx - 13, ncy - 13, ncx + 13, ncy + 13), fill=CAP_NUM)
    center_text(d, ncx, ncy, num, F["capn"], CAP_NUM_TXT)
    tx_start = ncx + 13 + 14
    tx_end = W - 30 - 10
    center_text(d, (tx_start + tx_end) / 2, ncy, txt, F["cap"], TXT)


def draw_header(d, F):
    center_text(d, W / 2, 24, "P2P Master Election & Relay - LAN Mode", F["title"], TXT)
    center_text(
        d,
        W / 2,
        44,
        "one master acts as the relay channel so every change reaches all peers",
        F["sub"],
        SUB,
    )


def draw_progress(d, t):
    frac = max(0.0, min(1.0, t / TOTAL))
    x0, x1, y0, y1 = 30, W - 30, H - 15, H - 9
    d.rounded_rectangle((x0, y0, x1, y1), radius=3, fill=PROG_TRACK)
    fill_x = x0 + (x1 - x0) * frac
    if fill_x > x0 + 1:
        d.rounded_rectangle((x0, y0, fill_x, y1), radius=3, fill=PROG_FILL)


def render_frame(t, F):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    draw_header(d, F)
    draw_edges(d, t)
    for node in ("A", "B", "C"):
        draw_node(d, node, t, F)
    draw_master(d, t, F)
    draw_offline(d, t, F)
    draw_packets(d, t)
    draw_caption(d, t, F)
    draw_progress(d, t)
    return img


def main():
    F = load_fonts()
    n = int(TOTAL * FPS)
    frames = [render_frame(i / FPS, F) for i in range(n)]
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "p2p-master-election.gif")
    frames[0].save(
        out,
        save_all=True,
        append_images=frames[1:],
        optimize=True,
        loop=0,
        duration=int(1000 / FPS),
    )
    print(f"frames={n}  size={os.path.getsize(out)}  -> {out}")


if __name__ == "__main__":
    main()
