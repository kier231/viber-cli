"""Exercise the approved voice through the production generator, with synthetic chats."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.codex_replies import CodexReplies, DRAFT_MODEL
from app.portfolio import candidates


def run():
    instructions = (ROOT/'prompts/sajtolog-replies.txt').read_text(encoding='utf-8').strip()
    if not 0 < len(instructions) <= 8000:
        raise ValueError('Owner instructions do not fit the dashboard limit.')
    generator = CodexReplies()
    if DRAFT_MODEL != 'gpt-6.1-sol':
        raise ValueError('This approved evaluation requires the configured gpt-6.1-sol model.')
    baseline = [{'direction': 'OUTGOING', 'body': 'Izrada jednostavnog sajta za salon sa uslugama i kontaktom bila bi 90 EUR.'}]
    cases = [
        ('booking_price', 'Ipak želim jedan kalendar za zakazivanje, bez posebnih integracija. Koliko onda košta izrada?', '110'),
        ('three_days', 'Treba mi za tri dana, avans mogu da uplatim danas.', None),
        ('portfolio_current', 'Koliko bi danas koštao sajt za salon kao vaš primer Bibbis?', '120'),
        ('portfolio_historical', 'Koliko ste baš taj Bibbis sajt ranije naplatili?', '120'),
        ('smaller_store', 'Samo jedan salon i deset proizvoda, standardno poručivanje. Bez posebnih integracija. Koliko bi bilo?', '260'),
        ('annual_maintenance', 'Održavanje je 20 mesečno, prvih 30 dana besplatno. Koliko tačno košta prva godina unapred?', None),
    ]

    def check(case):
        name, body, price = case
        context = {'contact': {'business_name': 'Frizerski salon Primer'},
                   'owner_client_description': 'Salon Primer; jednostavan javni sajt. Materijali još nisu poslati.',
                   'messages': baseline + [{'direction': 'INCOMING', 'body': body}], 'saved_owner_rules': []}
        context['portfolio_candidates'] = candidates(context)
        result = generator.generate(context, instructions)
        errors = []
        text = result['text'].lower()
        if result['action'] != 'reply': errors.append('ordinary service question was held')
        if price and price not in text: errors.append('missing current scope price')
        if text.count('?') > 1: errors.append('multiple customer questions')
        if name == 'booking_price' and re.search(r'održavan|odrzavan|domen|20\s*(?:eur|€)', text):
            errors.append('unrequested recurring-cost pitch in first creation-price answer')
        if name == 'three_days':
            if not re.search(r'\btri\b|\b3\b', text) or 'avans' not in text:
                errors.append('missing authorized short deadline or advance condition')
            if re.search(r'proveri|provjeri|saček|sacek', text):
                errors.append('already authorized simple-site deadline unnecessarily rechecked')
            if result['assumptions']: errors.append('known deadline treated as missing owner rule')
        if name == 'portfolio_historical':
            if not re.search(r'ne znam|nije poznat|nemam.*(?:podat|iznos)', text):
                errors.append('unknown historical charge not distinguished')
            if not re.search(r'sada|danas|bismo', text): errors.append('current estimate not distinguished from historical fee')
        if name == 'annual_maintenance':
            if '240' in text: errors.append('unapproved first-year amount quoted despite free first 30 days')
            if not result['assumptions']: errors.append('missing private annual-billing owner question')
        record = {'case': name, **result, 'errors': errors}
        print(json.dumps(record, ensure_ascii=True), flush=True)
        return record

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(check, cases))
    finally:
        generator.close()
    report = {'model': 'gpt-6.1-sol', 'reasoning': 'low', 'real_viber_messages_sent': 0,
              'passed': all(not r['errors'] for r in results), 'results': results}
    output = ROOT/'benchmark-results/approved-production-voice.json'
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'passed': report['passed'], 'cases': len(results), 'real_viber_messages_sent': 0}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(run())
