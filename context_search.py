"""Transparent context/keyword rules. No language models or model downloads."""
import copy
import re
from urllib.parse import unquote, urlsplit

# These are explicit rules, not claims of universal language understanding.
STOP = set('и в во на к с со из от за по для до о об у а но или что как это его ее её их он она они мы вы я ты был была было были будет уже еще ещё тот та то те так же только очень все всё свой своих который которая где когда теперь затем потом здесь там потому чтобы лишь через назад прежде после давайте этот эта эти году год года лет летний человек люди рядом также'.split())
FIRST = {'ольга':'Ольга','олег':'Олег','михаил':'Михаил','михаила':'Михаил','александр':'Александр',
         'александра':'Александр','алексей':'Алексей','алексея':'Алексей','владимир':'Владимир','владимира':'Владимир',
         'иван':'Иван','ивана':'Иван','сергей':'Сергей','сергея':'Сергей','юрий':'Юрий','юрия':'Юрий',
         'анна':'Анна','анны':'Анна','елена':'Елена','елены':'Елена','наталья':'Наталья','мария':'Мария',
         'марии':'Мария','светлана':'Светлана','светланы':'Светлана','ирина':'Ирина','ирины':'Ирина',
         'симона':'Симона','пётр':'Пётр','петр':'Пётр','петра':'Пётр','дмитрий':'Дмитрий','дмитрия':'Дмитрий',
         'james':'James','john':'John','olga':'Olga','michael':'Michael','anna':'Anna','simone':'Simone'}
FIRST.update({form: name for name, forms in {
    'Ольга': ('ольги', 'ольге', 'ольгу', 'ольгой'),
    'Анна': ('анне', 'анну', 'анной'), 'Елена': ('елене', 'елену', 'еленой'),
    'Мария': ('марию', 'марией'), 'Ирина': ('ирине', 'ирину', 'ириной'),
}.items() for form in forms})
SINGLE = {'ванга':'Ванга','ванги':'Ванга','ванге':'Ванга','вангу':'Ванга', 'наполеон':'Наполеон',
          'наполеона':'Наполеон', 'сталин':'Сталин','сталина':'Сталин','ленин':'Ленин','ленина':'Ленин'}
PLACES = {'болгари':'Болгария','софи':'София','москв':'Москва','минск':'Минск','мюнхен':'Мюнхен',
          'ленинград':'Ленинград','санкт-петербург':'Санкт-Петербург','киев':'Киев','париж':'Париж',
          'берлин':'Берлин','лондон':'Лондон','росси':'Россия','ссср':'СССР','америк':'Америка',
          'украин':'Украина','беларус':'Беларусь','китай':'Китай','япони':'Япония','германи':'Германия'}
# (stems, Russian concept, English query words, event or concrete action)
CONCEPTS = [
 ('олимп|olympic','Олимпиада','Olympic Games','event'),
 ('брус|uneven bars','брусья','gymnast uneven bars','action'),
 ('бревн|balance beam','гимнастическое бревно','gymnast balance beam','action'),
 ('гимнаст|gymnast','гимнастика','gymnastics','topic'),
 ('тренир|тренер|training','тренировка','athlete training','action'),
 ('медал|наград|medal','награждение медаль','medal award ceremony','event'),
 ('соревн|competition','спортивное выступление','sports competition performance','action'),
 ('выступ|реч[ьи]\\b|speech','выступление речь','public speech','action'),
 ('родил|детств|childhood','детство','childhood','topic'),
 ('пророч|предсказ|провид|prediction','предсказание','prophecy historical archive','topic'),
 ('советск|ссср|soviet','СССР','Soviet Union','topic'),
 ('распад|крах','распад','collapse historical archive','event'),
 ('войн|war\\b|wartime','война','war historical archive','event'),
 ('землетряс|earthquake','землетрясение','earthquake','event'),
 ('наводнен|flood','наводнение','flood','event'),
 ('пожар|fire\\b','пожар','fire','event'),
 ('поезд|железнодорож|train\\b|railway','поезд','railway train','action'),
 ('космос|космич|space','космос','space rocket','topic'),
 ('хирург|surgery','хирургическая операция','surgeon operation hospital','action'),
 ('больниц|hospital','больница','hospital','topic'),
 ('пишет|писал|написа|рукопис|письм|writing','рукопись письмо','writing manuscript letter','action'),
 ('книг|библиот|book','книга библиотека','book library','topic'),
 ('мор(?:е|я|ю|ем)\\b|морск|пляж|beach','море пляж','sea beach','topic'),
 ('лес(?:а|у|ом|ов|е|ной|ные)?\\b|forest','лес','forest','topic'),
 ('строит|строитель|construction','строительство','construction workers','action'),
 ('голосован|выборы|election','выборы голосование','election voting','event'),
]
SPORT_CONCEPTS = {'Олимпиада', 'брусья', 'гимнастическое бревно', 'гимнастика',
                  'тренировка', 'награждение медаль', 'спортивное выступление'}


def tokens(text):
    return re.findall(r"[\w]+(?:[-'][\w]+)*", str(text).casefold().replace('ё', 'е'))


def stem(token):
    token = token.casefold().replace('ё', 'е')
    if re.search('[а-я]', token):
        return re.sub(r'(иями|ами|ого|ему|ому|ыми|ими|иях|ах|ях|ий|ый|ой|ая|яя|ое|ее|ые|ие|ов|ев|ам|ям|ом|ем|ы|и|а|я|у|ю|е)$', '', token)
    return token[:-1] if token.endswith('s') and len(token) > 4 else token


def people(text):
    result = []
    words = re.findall(r'[A-Za-zА-Яа-яЁё]+', text)
    for i, word in enumerate(words):
        canonical = SINGLE.get(word.casefold())
        if canonical:
            result.append({'name': canonical, 'aliases': [word]})
        first = FIRST.get(word.casefold())
        if first and i + 1 < len(words) and words[i+1][0].isupper() and words[i+1].casefold() not in STOP:
            surname = re.sub(r'(овой|ову|евой|еву)$', lambda m: m[0][:2] + 'а', words[i+1])
            result.append({'name': first + ' ' + surname, 'aliases': [word + ' ' + words[i+1], first]})
    return result


def concepts(text):
    value = text.casefold().replace('ё', 'е')
    return [{'ru': ru, 'en': en, 'kind': kind} for pattern, ru, en, kind in CONCEPTS if re.search(r'\b(?:' + pattern + ')', value)]


def keywords(text):
    return list(dict.fromkeys(w for w in tokens(text) if len(w) > 2 and w not in STOP and not w.isdigit()))


def transliterate(text):
    chars = dict(zip('абвгдеёзийклмнопрстуфыэ', ['a','b','v','g','d','e','yo','z','i','y','k','l','m','n','o','p','r','s','t','u','f','y','e']))
    chars.update({'ж':'zh','х':'kh','ц':'ts','ч':'ch','ш':'sh','щ':'shch','ю':'yu','я':'ya','ь':'','ъ':''})
    return ''.join(chars.get(c,c) for c in text.casefold())


def scene_query(text, context='', kind='photo', state=None):
    state = dict(state or {})
    # Callers pass preceding narration only. Read it in order so pronouns can
    # retain a topic without importing a person from a future scene.
    if not state and context:
        for sentence in re.split(r'(?<=[.!?…])\s+', context):
            if sentence.strip():
                state = scene_query(sentence, kind=kind, state=state)['_state']
    found = people(text)
    person = found[-1]['name'] if found else state.get('person', '')
    place = next((name for prefix, name in PLACES.items() if any(w.startswith(prefix) for w in tokens(text))), '')
    dates = re.findall(r'\b(?:1[5-9]\d{2}|20\d{2})\b', text)
    date = dates[-1] if dates else state.get('date', '')
    matches = concepts(text)
    continuation = bool(re.search(r'\b(?:он|она|его|ее|её|ему|ей|этот|эта|там|тогда|he|she|his|her)\b', text.casefold()))
    new_person = bool(found and state.get('person') and person != state['person'])
    # Carry an action only in an explicit continuation. Unrecognised new text
    # keeps its own keywords instead of inheriting an unrelated old action.
    if not matches and continuation and not new_person:
        matches = state.get('concepts', [])
    old_concepts = {c['ru'] for c in state.get('concepts', [])}
    new_concepts = {c['ru'] for c in matches}
    if 'выступление речь' in new_concepts and (old_concepts | new_concepts).intersection(SPORT_CONCEPTS):
        matches = [{'ru':'спортивное выступление','en':'sports competition performance','kind':'action'}
                   if c['ru'] == 'выступление речь' else c for c in matches]
        new_concepts = {c['ru'] for c in matches}
    related_sport = bool(old_concepts.intersection(SPORT_CONCEPTS) and new_concepts.intersection(SPORT_CONCEPTS))
    unrelated = bool(matches and old_concepts and not old_concepts.intersection(new_concepts) and not related_sport)
    if not found and not continuation and (place or unrelated):
        person = ''
    if not dates and (new_person or (unrelated and not continuation)):
        date = ''
    concrete = [c for c in matches if c['kind'] in ('event','action')]
    # Keep the topic too: "collapse" alone loses "Soviet Union" from the query.
    chosen = (concrete + [c for c in matches if c not in concrete])[:3]
    detail = ' '.join(c['ru'] for c in chosen[:3])
    if not detail:
        other = [w for w in keywords(text) if w not in tokens(person) and w not in tokens(place)]
        detail = ' '.join(other[:5])
    subject = person or place or detail
    query = ' '.join(dict.fromkeys(x for x in (person, place, detail, date) if x)).strip()
    if not query:
        raise ValueError('В реплике нет текста для поиска.')
    english = ' '.join(dict.fromkeys(c['en'] for c in chosen[:3]))
    # Stock illustrates the action, not the identity of a named person.
    if kind == 'stock':
        query = english or detail or query
        english = query
    else:
        english = query
    return {'subject': subject, 'search_query': query, 'query_en': english,
            'required_entities': [person] if person and kind != 'stock' else [],
            'must_match': [c['ru'] for c in chosen], 'avoid': [],
            'context': context, 'depiction': 'illustrative' if kind == 'stock' else 'literal',
            'strict_relevance': False, 'query_basis': 'context_rules',
            'visual_reason': 'Поиск по имени, теме, действию и контексту; без анализа изображения.',
            '_state': {'person': person, 'date': date, 'concepts': matches}}


def rank_results(scene, candidates):
    """Metadata matches, not visual verification or a probability of semantic correctness."""
    query = ' '.join(str(scene.get(k,'')) for k in ('subject','search_query','query_en'))
    wanted = {stem(w) for w in keywords(query + ' ' + transliterate(query))}
    for concept in concepts(query):
        wanted.update(stem(w) for w in keywords(concept['en']))
    required = scene.get('required_entities') or []
    ranked = []
    for source in candidates:
        candidate = copy.deepcopy(source)
        # A query parameter is not evidence that the material depicts that query.
        parsed = urlsplit(str(candidate.get('source_url') or ''))
        evidence = str(candidate.get('title') or '') + ' ' + parsed.path
        evidence = unquote(evidence).replace('-', ' ').replace('_', ' ')
        found = {stem(w) for w in keywords(evidence)}
        matched = {w for w in wanted if any(w == f or (min(len(w),len(f)) >= 5 and (w.startswith(f) or f.startswith(w))) for f in found)}
        overlap = len(matched) / max(1,len(wanted))
        named = all(any(stem(alias) in found for alias in (tokens(name)[-1], transliterate(tokens(name)[-1]))) for name in required if tokens(name))
        # Preserve explicit rejections; a weaker fallback must not revive a known wrong result.
        candidate.update(relevance_score=min(1.0, overlap + (0.5 if required and named else 0)),
                         compatible=candidate.get('compatible') is not False and (not required or named)
                                    and (not scene.get('must_match') or not found or bool(matched)),
                         relevance_basis='context_and_source_keywords',
                         relevance_reason='Совпадение ключевых слов в названии и ссылке; содержимое кадра не проверено.')
        ranked.append(candidate)
    return sorted(ranked, key=lambda c:(c['compatible'],c['relevance_score']), reverse=True)
