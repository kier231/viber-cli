"""Small, relevant selections from the owner's confirmed portfolio catalog."""
from functools import lru_cache
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit

CATALOG = Path(__file__).resolve().parent.parent / 'data' / 'portfolio.json'
ALIASES = {
    'Hair salon': 'frizer frizerski frizure kosa šišanje',
    'Barbershop': 'berber brada barber',
    'Nail salon': 'nokti manikir pedikir',
    'Facial and beauty clinic': 'kozmetika kozmetički lice lepota',
    'Laser hair removal': 'epilacija depilacija laser',
    'Brow and lash studio': 'obrve trepavice',
    'Makeup artist': 'šminka šminkanje',
    'Tattoo studio': 'tetovaža tattoo',
    'Massage studio': 'masaža',
    'Auto mechanic': 'automehaničar mehaničar autoservis',
    'Tire fitting and repair': 'vulkanizer gume',
    'Car wash and detailing': 'autoperionica perionica detailing',
    'Auto body and paint repair': 'autolimar limarija farbanje',
    'Plumber': 'vodoinstalater vodovod',
    'Electrician': 'električar elektro',
    'Locksmith': 'bravar brave ključevi',
    'Air conditioning and heating service': 'klima grejanje klimatizacija',
    'Painter and decorator': 'moler krečenje',
    'House cleaning': 'čišćenje',
    'Moving company': 'selidbe selidba',
    'Carpentry and custom furniture': 'stolar nameštaj kuhinje',
    'Landscaping': 'bašta dvorište uređenje',
    'Dental practice': 'zubar stomatolog stomatološka',
    'Veterinary clinic': 'veterinar veterinarska',
    'Pet grooming': 'ljubimci grooming šišanje pasa',
    'Restaurant': 'restoran restorani',
    'Coffee shop': 'kafić kafeterija kafa',
    'Bakery': 'pekara pecivo',
    'Cake and pastry shop': 'poslastičarnica torte kolači',
    'Catering': 'ketering catering',
    'Pizzeria': 'picerija pica pizza',
    'Law firm': 'advokat advokatska',
    'Bookkeeping and accounting': 'knjigovođa knjigovodstvo računovodstvo',
    'Real estate agency': 'nekretnine stanovi',
    'Architecture studio': 'arhitekt arhitektura',
    'Language school': 'jezici engleski škola jezika',
    'Wedding photographer': 'fotograf fotografija venčanje',
    'Wedding and event planner': 'dekoracije proslave venčanja događaji',
    'Florist': 'cveće cvećara rasadnik',
    'Gym': 'teretana fitnes',
    'Children\'s play and birthday venue': 'igraonica rođendan',
    'Computer repair': 'računari kompjuteri servis računara',
    'Phone repair': 'telefoni servis telefona',
    'Fashion boutique and clothing': 'butik odeća garderoba',
    'Web and software development': 'sajtovi programiranje softver',
}
STOP = {'salon', 'shop', 'and', 'the', 'with', 'for', 'service', 'services', 'studio', 'clinic', 'company', 'repair'}


def tokens(text):
    text = unicodedata.normalize('NFKD', text.lower().replace('đ', 'dj'))
    text = ''.join(c for c in text if not unicodedata.combining(c))
    return {word[:5] for word in re.findall(r'[a-z0-9]+', text) if len(word) > 3 and word not in STOP}


def normalize(records):
    """Import data fields only; document text never becomes model instructions."""
    result, seen = [], set()
    for group in records:
        category = str(group['category'])[:150]
        for side in ('serbian', 'international'):
            for entry in group.get(side, []):
                url = entry.get('url', '')
                parsed = urlsplit(url)
                if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                        or parsed.username or parsed.password or len(url) > 2000):
                    raise ValueError('Portfolio contains an invalid website URL.')
                name = entry.get('name', '').strip()
                if not name or len(name) > 250:
                    raise ValueError('Portfolio contains an invalid project name.')
                if url in seen:
                    continue
                seen.add(url)
                result.append({'category': category, 'name': name, 'url': url,
                               'market': str(entry.get('market', ''))[:100],
                               'description': str(entry.get('whatIsItAbout', ''))[:600],
                               'search_terms': str(entry.get('capturedTitle', ''))[:300]})
    return result


@lru_cache(maxsize=4)
def _read(path, mtime):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def catalog():
    if not CATALOG.exists():
        return []
    return _read(str(CATALOG), CATALOG.stat().st_mtime_ns)


def candidates(context, limit=4):
    records = catalog()
    # Incoming correspondence describes the customer's business; outgoing
    # portfolio links must not steer the next retrieval to our own industry.
    query = tokens(' '.join(str(v or '') for v in context['contact'].values()) + ' ' +
                   context.get('owner_client_description', '') + ' ' +
                   ' '.join(m['body'] or '' for m in context['messages'][-20:] if m['direction'] == 'INCOMING'))
    categories = {}
    for row in records:
        group = categories.setdefault(row['category'], {'score': 0, 'rows': []})
        group['rows'].append(row)
        direct = tokens(row['category'] + ' ' + ALIASES.get(row['category'], ''))
        group['score'] = max(group['score'], 4 * len(query & direct) +
                             len(query & tokens(row['search_terms'] + ' ' + row['description'])))
    ranked = sorted(categories.values(), key=lambda g: g['score'], reverse=True)
    if not ranked or not ranked[0]['score']:
        return []
    rows = sorted(ranked[0]['rows'], key=lambda r: r['market'] != 'Serbia')
    return [{k: v for k, v in row.items() if k != 'search_terms'} for row in rows[:limit]]
