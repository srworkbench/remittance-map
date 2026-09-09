"""Native reports drawn from the computed plan, including ambiguous bundle witnesses."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

BG = '#0b1424'
PANEL = '#142238'
INK = '#f4f7fb'
MUTED = '#a3b4cc'
AMBER = '#ffc36a'
GREEN = '#72e0bd'
BLUE = '#85b9ff'


def font(size):
    try:
        return ImageFont.truetype('DejaVuSans.ttf', size=size)
    except OSError:
        return ImageFont.load_default(size=size)


def money(value):
    return f'{value // 100:,}.{value % 100:02d}'


def text(draw, x, y, value, size=32, fill=INK, width=1420):
    value = str(value)
    face = font(size)
    if draw.textlength(value, font=face) > width:
        while value and draw.textlength(value + '...', font=face) > width:
            value = value[:-1]
        value += '...'
    draw.text((x, y), value, font=face, fill=fill)


def wrapped(draw, x, y, value, size=28, width=620):
    words = str(value).split(); line = ''; lines = []
    for word in words:
        attempt = (line + ' ' + word).strip()
        if draw.textlength(attempt, font=font(size)) > width and line:
            lines.append(line); line = word
        else:
            line = attempt
    if line: lines.append(line)
    for index, line in enumerate(lines):
        text(draw, x, y + index * (size + 10), line, size, width=width)
    return len(lines) * (size + 10)


def canvas(height):
    image = Image.new('RGB', (1600, height), BG); draw = ImageDraw.Draw(image)
    text(draw, 80, 46, 'REMITTANCE MAP  /  REVIEW BEFORE ALLOCATION', 24, BLUE)
    return image, draw


def card(draw, rect, outline=None):
    draw.rounded_rectangle(rect, radius=24, fill=PANEL, outline=outline, width=3)


def render_ambiguity(plan, payment, output):
    invoices = {i['invoice_id']: i for i in plan['invoices']}
    options = payment['options'][:2]
    max_rows = max(len(o) for o in options)
    bottom = 600 + max_rows * 80
    image = Image.new('RGB', (1000, bottom + 250), BG)
    draw = ImageDraw.Draw(image)
    text(draw, 50, 32, 'REMITTANCE MAP / ACTUAL REVIEW RESULT', 23, BLUE, 900)
    text(draw, 50, 90, 'One payment.', 67, width=900)
    text(draw, 50, 166, 'Two exact matches.', 67, width=900)
    text(draw, 50, 255, 'Both fit. Neither is a unique match.', 32, MUTED, 900)
    card(draw, (250, 325, 750, 450), AMBER)
    text(draw, 280, 342, payment['payment_id'], 28, MUTED, 440)
    text(draw, 280, 386, payment['currency'] + ' ' + money(payment['cents']), 46, AMBER, 440)
    draw.line([(500, 452), (500, 479), (263, 479), (263, 505)], fill=AMBER, width=5)
    draw.line([(500, 479), (737, 479), (737, 505)], fill=AMBER, width=5)
    for n, option in enumerate(options):
        x = 50 + n * 475
        card(draw, (x, 505, x + 425, bottom), AMBER)
        text(draw, x + 22, 525, f'POSSIBLE BUNDLE {n + 1}', 23, AMBER, 380)
        for index, invoice_id in enumerate(option):
            y = 575 + index * 80
            text(draw, x + 22, y, invoice_id, 36, width=150)
            text(draw, x + 188, y, money(invoices[invoice_id]['cents']), 36, width=220)
        text(draw, x + 22, bottom - 54, '= ' + money(sum(invoices[i]['cents'] for i in option)), 36, GREEN, 380)
    text(draw, 50, bottom + 43, 'HELD FOR REVIEW', 49, AMBER, 900)
    text(draw, 50, bottom + 116, 'Neither bundle is exported.', 35, INK, 900)
    text(draw, 50, bottom + 180, 'Two possibilities shown; more may exist.', 25, MUTED, 900)
    image.save(output/'ambiguity.png')


def render_overview(plan, output):
    rows = plan['payments']
    for page, offset in enumerate(range(0, len(rows), 6), 1):
        shown = rows[offset:offset + 6]
        totals = plan['totals_cents']
        header_height = 250 + 75 * len(totals)
        height = header_height + 138 * len(shown) + 120
        image, draw = canvas(height)
        text(draw, 80, 108, 'Every match has an explanation.', 62)
        for n, (currency, sums) in enumerate(sorted(totals.items())):
            y = 200 + n * 75
            text(draw, 80, y, currency, 34, MUTED, 125)
            text(draw, 220, y, money(sums['SUGGESTED']) + ' suggested', 34, GREEN, 620)
            text(draw, 880, y, money(sums['REVIEW']) + ' held for review', 34, AMBER, 630)
        y = header_height
        for r in shown:
            color = GREEN if r['status'] == 'SUGGESTED' else AMBER
            card(draw, (80, y, 1520, y + 118))
            text(draw, 108, y + 18, r['payment_id'], 31, INK, 200)
            text(draw, 330, y + 18, r['currency'] + ' ' + money(r['cents']), 31, INK, 340)
            text(draw, 720, y + 18, r['status'], 28, color, 740)
            reason = r['reason']
            if r['status'] == 'SUGGESTED': reason = ' + '.join(r['options'][0]) + ' = exact total; no competing payment'
            text(draw, 108, y + 67, reason, 27, MUTED, 1370)
            y += 138
        text(draw, 80, y + 25, f'Review suggestions before use. No payments posted.  /  Page {page}', 25, MUTED)
        image.save(output/f'overview-{page:03}.png')


def render(plan, output):
    output = Path(output)
    render_overview(plan, output)
    ambiguous = next((r for r in plan['payments'] if len(r['options']) > 1), None)
    if ambiguous:
        render_ambiguity(plan, ambiguous, output)
