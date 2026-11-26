"""Country codes, names and free-text location matching.

``resolve_country`` turns user input or a structured API field ("nz", "New
Zealand", "UK") into an ISO 3166-1 alpha-2 code.

``countries_in`` reads free-text locations such as ``"Remote, Canada; Austin,
TX"`` (the kind company job boards use) and returns the countries they name.
It is a heuristic: it knows country names, US states, Canadian provinces,
Australian states, a set of major cities, and any extra keywords you pass in.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from functools import lru_cache

# code|English name|other names...    (ISO 3166-1; base list from the tz database)
_COUNTRIES = """
AD|Andorra
AE|United Arab Emirates|UAE
AF|Afghanistan
AG|Antigua and Barbuda
AI|Anguilla
AL|Albania
AM|Armenia
AO|Angola
AQ|Antarctica
AR|Argentina
AS|American Samoa
AT|Austria|Österreich
AU|Australia
AW|Aruba
AX|Åland Islands
AZ|Azerbaijan
BA|Bosnia and Herzegovina|Bosnia
BB|Barbados
BD|Bangladesh
BE|Belgium|Belgique|België
BF|Burkina Faso
BG|Bulgaria
BH|Bahrain
BI|Burundi
BJ|Benin
BL|Saint Barthélemy|St Barthelemy
BM|Bermuda
BN|Brunei|Brunei Darussalam
BO|Bolivia
BQ|Caribbean Netherlands|Bonaire
BR|Brazil|Brasil
BS|Bahamas
BT|Bhutan
BV|Bouvet Island
BW|Botswana
BY|Belarus
BZ|Belize
CA|Canada
CC|Cocos (Keeling) Islands|Cocos Islands
CD|DR Congo|Democratic Republic of the Congo|Congo-Kinshasa|DRC
CF|Central African Republic
CG|Republic of the Congo|Congo|Congo-Brazzaville
CH|Switzerland|Schweiz|Suisse|Svizzera
CI|Côte d'Ivoire|Ivory Coast
CK|Cook Islands
CL|Chile
CM|Cameroon
CN|China|Mainland China|PRC
CO|Colombia
CR|Costa Rica
CU|Cuba
CV|Cape Verde|Cabo Verde
CW|Curaçao
CX|Christmas Island
CY|Cyprus
CZ|Czech Republic|Czechia
DE|Germany|Deutschland
DJ|Djibouti
DK|Denmark|Danmark
DM|Dominica
DO|Dominican Republic
DZ|Algeria
EC|Ecuador
EE|Estonia
EG|Egypt
EH|Western Sahara
ER|Eritrea
ES|Spain|España
ET|Ethiopia
FI|Finland|Suomi
FJ|Fiji
FK|Falkland Islands
FM|Micronesia
FO|Faroe Islands
FR|France
GA|Gabon
GB|United Kingdom|UK|U.K.|Great Britain|Britain|England|Scotland|Wales|Northern Ireland
GD|Grenada
GE|Georgia
GF|French Guiana
GG|Guernsey
GH|Ghana
GI|Gibraltar
GL|Greenland
GM|Gambia|The Gambia
GN|Guinea
GP|Guadeloupe
GQ|Equatorial Guinea
GR|Greece
GS|South Georgia and the South Sandwich Islands|South Georgia
GT|Guatemala
GU|Guam
GW|Guinea-Bissau
GY|Guyana
HK|Hong Kong|Hong Kong SAR
HM|Heard Island and McDonald Islands
HN|Honduras
HR|Croatia
HT|Haiti
HU|Hungary
ID|Indonesia
IE|Ireland|Republic of Ireland
IL|Israel
IM|Isle of Man
IN|India
IO|British Indian Ocean Territory
IQ|Iraq
IR|Iran
IS|Iceland
IT|Italy|Italia
JE|Jersey
JM|Jamaica
JO|Jordan
JP|Japan
KE|Kenya
KG|Kyrgyzstan
KH|Cambodia
KI|Kiribati
KM|Comoros
KN|Saint Kitts and Nevis|St Kitts and Nevis
KP|North Korea
KR|South Korea|Korea|Republic of Korea
KW|Kuwait
KY|Cayman Islands
KZ|Kazakhstan
LA|Laos|Lao PDR
LB|Lebanon
LC|Saint Lucia|St Lucia
LI|Liechtenstein
LK|Sri Lanka
LR|Liberia
LS|Lesotho
LT|Lithuania
LU|Luxembourg
LV|Latvia
LY|Libya
MA|Morocco
MC|Monaco
MD|Moldova
ME|Montenegro
MF|Saint Martin|St Martin
MG|Madagascar
MH|Marshall Islands
MK|North Macedonia|Macedonia
ML|Mali
MM|Myanmar|Burma
MN|Mongolia
MO|Macau|Macao
MP|Northern Mariana Islands
MQ|Martinique
MR|Mauritania
MS|Montserrat
MT|Malta
MU|Mauritius
MV|Maldives
MW|Malawi
MX|Mexico|México
MY|Malaysia
MZ|Mozambique
NA|Namibia
NC|New Caledonia
NE|Niger
NF|Norfolk Island
NG|Nigeria
NI|Nicaragua
NL|Netherlands|The Netherlands|Nederland|Holland
NO|Norway|Norge
NP|Nepal
NR|Nauru
NU|Niue
NZ|New Zealand|Aotearoa|NZ
OM|Oman
PA|Panama
PE|Peru
PF|French Polynesia
PG|Papua New Guinea
PH|Philippines
PK|Pakistan
PL|Poland|Polska
PM|Saint Pierre and Miquelon
PN|Pitcairn Islands|Pitcairn
PR|Puerto Rico
PS|Palestine
PT|Portugal
PW|Palau
PY|Paraguay
QA|Qatar
RE|Réunion
RO|Romania
RS|Serbia
RU|Russia|Russian Federation
RW|Rwanda
SA|Saudi Arabia|KSA
SB|Solomon Islands
SC|Seychelles
SD|Sudan
SE|Sweden|Sverige
SG|Singapore
SH|Saint Helena|St Helena
SI|Slovenia
SJ|Svalbard and Jan Mayen
SK|Slovakia
SL|Sierra Leone
SM|San Marino
SN|Senegal
SO|Somalia
SR|Suriname
SS|South Sudan
ST|São Tomé and Príncipe|Sao Tome and Principe
SV|El Salvador
SX|Sint Maarten
SY|Syria
SZ|Eswatini|Swaziland
TC|Turks and Caicos Islands
TD|Chad
TF|French Southern Territories
TG|Togo
TH|Thailand
TJ|Tajikistan
TK|Tokelau
TL|Timor-Leste|East Timor
TM|Turkmenistan
TN|Tunisia
TO|Tonga
TR|Turkey|Türkiye
TT|Trinidad and Tobago
TV|Tuvalu
TW|Taiwan
TZ|Tanzania
UA|Ukraine
UG|Uganda
UM|U.S. Minor Outlying Islands
US|United States|United States of America|USA|U.S.A.|US|U.S.
UY|Uruguay
UZ|Uzbekistan
VA|Vatican City|Holy See
VC|Saint Vincent and the Grenadines|St Vincent
VE|Venezuela
VG|British Virgin Islands
VI|U.S. Virgin Islands|US Virgin Islands
VN|Vietnam|Viet Nam
VU|Vanuatu
WF|Wallis and Futuna
WS|Samoa
YE|Yemen
YT|Mayotte
ZA|South Africa
ZM|Zambia
ZW|Zimbabwe
"""

# Names that are also places in other countries. They are matched as weak
# "place" hints instead of definite country names ("Atlanta, Georgia").
_AMBIGUOUS_NAMES = {"georgia": {"GE", "US"}}

# States, provinces and regions, matched as whole words.
_REGIONS = """
US|Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming|District of Columbia
CA|Alberta|British Columbia|Manitoba|New Brunswick|Newfoundland and Labrador|Newfoundland|Nova Scotia|Ontario|Prince Edward Island|Quebec|Québec|Saskatchewan|Northwest Territories|Nunavut|Yukon
AU|New South Wales|Queensland|Tasmania|Western Australia|South Australia|Northern Territory|Australian Capital Territory
NZ|Waikato|Bay of Plenty|Otago|Northland|Manawatu|Manawatū|Hawke's Bay|Taranaki|Southland|Gisborne
DE|Bayern|Bavaria|Baden-Württemberg|Baden-Wuerttemberg|Nordrhein-Westfalen|North Rhine-Westphalia|NRW|Hessen|Hesse|Niedersachsen|Lower Saxony|Sachsen|Saxony|Sachsen-Anhalt|Saxony-Anhalt|Thüringen|Thuringia|Schleswig-Holstein|Rheinland-Pfalz|Rhineland-Palatinate|Saarland|Mecklenburg-Vorpommern
IN|Karnataka|Maharashtra|Tamil Nadu|Telangana|Haryana|Uttar Pradesh|Kerala|Gujarat|West Bengal
"""

# Major cities, matched as whole words. Deliberately skips names that are
# common in several countries (Hamilton, Victoria, Santiago, ...).
_CITIES = """
NZ|Auckland|Wellington|Christchurch|Dunedin|Tauranga|Palmerston North|Napier|Rotorua|New Plymouth|Invercargill|Whangarei|Whangārei|Lower Hutt|Upper Hutt|Porirua|Whanganui|Timaru|Taupo|Taupō
AU|Sydney|Melbourne|Brisbane|Perth|Adelaide|Canberra|Hobart|Darwin|Gold Coast|Geelong|Wollongong|Townsville|Cairns
GB|London|Manchester|Birmingham|Edinburgh|Glasgow|Leeds|Bristol|Liverpool|Belfast|Cardiff|Sheffield|Nottingham|Newcastle upon Tyne|Leicester|Aberdeen|Brighton|Reading|Milton Keynes|Southampton|Coventry|Cambridge|Oxford
IE|Dublin|Cork|Galway|Limerick
US|New York City|NYC|San Francisco|San Francisco Bay Area|Bay Area|Silicon Valley|Los Angeles|Seattle|Chicago|Boston|Austin|Denver|Atlanta|Miami|Dallas|Houston|Philadelphia|Phoenix|San Diego|San Jose|Portland|Pittsburgh|Detroit|Minneapolis|Nashville|Salt Lake City|Raleigh|Charlotte|Baltimore|Las Vegas|Orlando|Tampa|St. Louis|Kansas City|Sacramento|Palo Alto|Mountain View|Menlo Park|Sunnyvale|Redmond|Cupertino|Brooklyn|Manhattan|Jersey City|Washington DC|Washington D.C.
CA|Toronto|Vancouver|Montreal|Montréal|Ottawa|Calgary|Edmonton|Winnipeg|Quebec City|Halifax|Mississauga|Kitchener
DE|Berlin|Munich|München|Hamburg|Frankfurt|Frankfurt am Main|Cologne|Köln|Stuttgart|Düsseldorf|Leipzig|Dresden|Hanover|Hannover|Nuremberg|Nürnberg|Essen|Dortmund|Bonn|Karlsruhe|Mannheim|Heidelberg|Bremen
FR|Paris|Lyon|Marseille|Toulouse|Nantes|Bordeaux|Lille|Strasbourg|Montpellier|Rennes|Grenoble
NL|Amsterdam|Rotterdam|The Hague|Den Haag|Utrecht|Eindhoven|Groningen|Delft|Leiden
BE|Brussels|Bruxelles|Brussel|Antwerp|Antwerpen|Ghent|Gent|Leuven|Liège
CH|Zurich|Zürich|Geneva|Genève|Basel|Bern|Lausanne|Lugano|Zug
AT|Vienna|Wien|Graz|Linz|Salzburg|Innsbruck
ES|Madrid|Barcelona|Valencia|Seville|Sevilla|Málaga|Bilbao|Zaragoza
PT|Lisbon|Lisboa|Porto|Braga
IT|Rome|Roma|Milan|Milano|Turin|Torino|Naples|Napoli|Florence|Firenze|Bologna|Genoa|Genova
SE|Stockholm|Gothenburg|Göteborg|Malmö|Uppsala|Linköping|Lund|Västerås|Örebro|Umeå
NO|Oslo|Bergen|Trondheim|Stavanger
DK|Copenhagen|København|Aarhus|Odense
FI|Helsinki|Espoo|Tampere|Turku|Oulu
IS|Reykjavik|Reykjavík
PL|Warsaw|Warszawa|Kraków|Krakow|Wrocław|Wroclaw|Gdańsk|Gdansk|Poznań|Poznan|Łódź|Lodz|Katowice
CZ|Prague|Praha|Brno|Ostrava
SK|Bratislava
HU|Budapest
RO|Bucharest|București|Cluj-Napoca|Iași|Timișoara
BG|Sofia
GR|Athens|Thessaloniki
HR|Zagreb
RS|Belgrade|Novi Sad
SI|Ljubljana
UA|Kyiv|Kiev|Lviv|Kharkiv|Odesa|Odessa
EE|Tallinn|Tartu
LV|Riga
LT|Vilnius|Kaunas
TR|Istanbul|Ankara|Izmir
IL|Tel Aviv|Jerusalem|Haifa|Herzliya
AE|Dubai|Abu Dhabi
SA|Riyadh|Jeddah
QA|Doha
EG|Cairo
MA|Casablanca|Rabat
NG|Lagos|Abuja
KE|Nairobi
GH|Accra
ZA|Johannesburg|Cape Town|Durban|Pretoria
IN|Bangalore|Bengaluru|Mumbai|Delhi|New Delhi|Hyderabad|Pune|Chennai|Gurgaon|Gurugram|Noida|Kolkata|Ahmedabad|Kochi|Jaipur|Chandigarh
PK|Karachi|Lahore|Islamabad
BD|Dhaka
LK|Colombo
MY|Kuala Lumpur|Penang
TH|Bangkok
VN|Ho Chi Minh City|Hanoi|Da Nang
PH|Manila|Makati|Cebu|Taguig|Quezon City
ID|Jakarta|Bandung|Surabaya|Bali
CN|Beijing|Shanghai|Shenzhen|Guangzhou|Hangzhou|Chengdu
TW|Taipei
JP|Tokyo|Osaka|Kyoto|Yokohama|Fukuoka|Nagoya
KR|Seoul|Busan
BR|São Paulo|Rio de Janeiro|Belo Horizonte|Curitiba|Porto Alegre|Brasília|Recife|Florianópolis
MX|Mexico City|Ciudad de México|CDMX|Guadalajara|Monterrey
AR|Buenos Aires
CO|Bogotá|Medellín|Cali
CL|Santiago de Chile
PE|Lima
UY|Montevideo
"""

# Abbreviations written as their own part of a location ("Austin, TX").
_REGION_CODES = {
    "US": "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV "
    "NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC",
    "CA": "AB BC MB NB NL NS NT NU ON PE QC SK YT",
    "AU": "NSW VIC QLD WA SA TAS ACT NT",
}


def _parse_table(table: str) -> list[tuple[str, list[str]]]:
    rows = []
    for line in table.strip().splitlines():
        code, *names = line.split("|")
        rows.append((code, names))
    return rows


def _norm(text: str, lower: bool = True) -> str:
    """Strip accents and punctuation so "Côte d’Ivoire" == "cote d ivoire"."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("&", " and ")
    if lower:
        text = text.lower()
    return " ".join(re.findall(r"[A-Za-z0-9]+", text))


COUNTRY_NAMES: dict[str, str] = {}  # code -> English name
_NAME_TO_CODE: dict[str, str] = {}  # normalised name or alias -> code
_UPPER_ALIASES: dict[str, str] = {}  # "US", "U.K." ... matched case-sensitively

for _code, _names in _parse_table(_COUNTRIES):
    COUNTRY_NAMES[_code] = _names[0]
    for _name in _names:
        _NAME_TO_CODE.setdefault(_norm(_name), _code)
        if _name.replace(".", "").isupper():
            _UPPER_ALIASES[_norm(_name, lower=False)] = _code

_REGION_CODE_TO_COUNTRIES: dict[str, set[str]] = {}
for _country, _codes in _REGION_CODES.items():
    for _abbr in _codes.split():
        _REGION_CODE_TO_COUNTRIES.setdefault(_abbr, set()).add(_country)


def country_name(code: str) -> str:
    return COUNTRY_NAMES.get(code.upper(), code.upper())


def resolve_country(value: str | None) -> str | None:
    """Return the ISO code for a code or country name, or None if unknown."""
    if not value:
        return None
    text = str(value).strip()
    if len(text) == 2 and text.upper() in COUNTRY_NAMES:
        return text.upper()
    if text.upper() == "UK":
        return "GB"
    return _NAME_TO_CODE.get(_norm(text))


def suggest_countries(value: str, limit: int = 5) -> list[str]:
    """Close matches for an unknown country, for error messages."""
    import difflib

    codes = {name: code for code, name in COUNTRY_NAMES.items()}
    hits = difflib.get_close_matches(value.strip().title(), list(codes), n=limit, cutoff=0.75)
    return [f"{hit} ({codes[hit]})" for hit in hits]


# --------------------------------------------------------------------------- matching

_COUNTRY = 2  # a country name: decisive
_PLACE = 1  # a region or city: a hint that other evidence can override


@lru_cache(maxsize=1)
def _base_terms() -> dict[str, tuple[int, frozenset[str]]]:
    terms: dict[str, tuple[int, frozenset[str]]] = {}
    for code, names in _parse_table(_COUNTRIES):
        for name in names:
            key = _norm(name)
            if name.replace(".", "").isupper() or key in terms:
                continue  # upper-case aliases are matched case-sensitively
            if key in _AMBIGUOUS_NAMES:
                terms[key] = (_PLACE, frozenset(_AMBIGUOUS_NAMES[key]))
            else:
                terms[key] = (_COUNTRY, frozenset({code}))
    for table in (_REGIONS, _CITIES):
        for code, names in _parse_table(table):
            for name in names:
                key = _norm(name)
                level, codes = terms.get(key, (_PLACE, frozenset()))
                if level == _PLACE:
                    terms[key] = (_PLACE, codes | {code})
    return terms


def _compile(phrases: Iterable[str], flags: int = 0) -> re.Pattern[str]:
    ordered = sorted(set(phrases), key=len, reverse=True)  # longest first: "new mexico" before "mexico"
    body = "|".join(re.escape(p) for p in ordered)
    return re.compile(rf"(?<![A-Za-z0-9])(?:{body})(?![A-Za-z0-9])", flags)


@lru_cache(maxsize=64)
def _matcher(extra: tuple[tuple[str, tuple[str, ...]], ...]):
    terms = dict(_base_terms())
    for code, keywords in extra:
        for keyword in keywords:
            key = _norm(keyword)
            if key:
                level, codes = terms.get(key, (_PLACE, frozenset()))
                terms[key] = (level, codes | {code}) if level == _PLACE else (level, codes)
    return terms, _compile(terms), _compile(_UPPER_ALIASES)


_ENTRY_SPLIT = re.compile(r"\s*(?:;|\||\n|\s/\s|\s+or\s+)\s*")
_PART_SPLIT = re.compile(r"[,()\-–—/·•]")


def _entry_countries(entry: str, terms, pattern, upper_pattern) -> set[str]:
    named: set[str] = set()
    hints: list[set[str]] = []
    for match in pattern.finditer(_norm(entry)):
        level, codes = terms[match.group(0)]
        if level == _COUNTRY:
            named |= codes
        else:
            hints.append(set(codes))
    for match in upper_pattern.finditer(_norm(entry, lower=False)):
        named.add(_UPPER_ALIASES[match.group(0)])
    if named:
        return named

    # Abbreviations such as "TX", "ON", "NSW" or "DE". Many are both a US
    # state and a country code ("CA" = California / Canada, "DE" = Delaware /
    # Germany): read them as the state unless a city or region in the same
    # entry backs up the country ("Munich, DE" -> DE, "Fresno, CA" -> US).
    hinted = set().union(*hints) if hints else set()
    code_hints: list[set[str]] = []
    for part in _PART_SPLIT.split(entry):
        token = part.replace(".", "").strip()
        if not (2 <= len(token) <= 3 and token.isalpha() and token.isupper()):
            continue
        regions = _REGION_CODE_TO_COUNTRIES.get(token, set())
        iso = token if len(token) == 2 and token in COUNTRY_NAMES else None
        if iso and (iso in hinted or not regions):
            code_hints.append({iso})
        elif regions:
            code_hints.append(set(regions))

    evidence = hints + code_hints
    if not evidence:
        return set()
    common = set.intersection(*evidence)
    if common:
        return common  # e.g. "Perth, WA" -> AU, "Berlin, DE" -> DE
    # Conflicting hints: an explicit abbreviation beats a city name
    # ("London, ON" is in Canada), otherwise keep every candidate.
    return set().union(*(code_hints or hints))


def countries_in(
    text: str | None, extra_keywords: Mapping[str, Iterable[str]] | None = None
) -> set[str]:
    """Countries named by a free-text location string.

    ``extra_keywords`` maps a country code to more words that mean "in this
    country", for example ``{"NZ": ["Hamilton", "Nelson"]}``.
    """
    if not text:
        return set()
    extra = tuple(sorted((code, tuple(words)) for code, words in (extra_keywords or {}).items()))
    terms, pattern, upper_pattern = _matcher(extra)
    found: set[str] = set()
    for entry in _ENTRY_SPLIT.split(text):
        if entry.strip():
            found |= _entry_countries(entry, terms, pattern, upper_pattern)
    return found


def mentions_country(
    texts: Iterable[str | None],
    country: str,
    extra_keywords: Mapping[str, Iterable[str]] | None = None,
) -> bool:
    return any(country in countries_in(text, extra_keywords) for text in texts if text)
