"""Exercise the real isolated model with synthetic sales turns; never send Viber."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.codex_replies import CodexReplies
from app.portfolio import candidates

root = Path(__file__).resolve().parent.parent
instructions = (root / 'prompts/sajtolog-replies.txt').read_text(encoding='utf-8')
generator = CodexReplies()
cases = [
    ('portfolio', 'Možete li da pošaljete primer nekog vašeg rada za frizerski salon?', ''),
    ('assumption', 'Kako da vam pošaljem fotografije i tekst? Može preko WeTransfera?', ''),
    ('known_floor', 'Za jednostavan sajt bez zakazivanja mogu da platim 60 EUR, može?', ''),
    ('saved_rule', 'Kako da vam pošaljem fotografije i tekst? Može preko WeTransfera?',
     'Prihvatam materijale preko WeTransfera. To je odobren način slanja; nema nove pretpostavke.'),
]

def run(case):
    name, body, extra = case
    context = {'contact': {'business_name': 'Frizerski salon', 'viber_name': 'Test salon'},
               'messages': [{'direction': 'OUTGOING', 'body': 'Pozdrav, bavim se izradom sajtova. Da li ste razmišljali o sajtu za salon?'},
                            {'direction': 'INCOMING', 'body': body}]}
    context['portfolio_candidates'] = candidates(context)
    context['saved_owner_rules'] = [{'topic': 'material_transfer_method', 'scenario': 'Kako mogu da dobijem materijale?', 'owner_rule': extra}] if extra else []
    start = time.perf_counter()
    result = generator.generate(context, instructions)
    assert result['action'] == 'reply', (name, result)
    if name == 'portfolio':
        assert any(r['url'] in result['text'] for r in context['portfolio_candidates']), result
    if name == 'assumption':
        assert result['assumptions'] and all(i['question'] not in result['text'] for i in result['assumptions']), result
    if name == 'known_floor':
        assert '90' in result['text'] and not result['assumptions'], result
    if name == 'saved_rule':
        assert not result['assumptions'], result
    return {'case': name, 'seconds': round(time.perf_counter() - start, 2), **result}

try:
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(run, cases))
finally:
    generator.close()
destination = root / 'benchmark-results' / 'reply-feedback.json'
destination.parent.mkdir(exist_ok=True)
destination.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(results, ensure_ascii=True))
