"""
Core module: Session-based ChatGPT Plus payment link generation.
Extracted from the desktop app — zero GUI dependencies, server-deployable.

Supports all payment modes:
  - 无卡长链接 (hosted Stripe checkout URL, no card needed)
  - PayPal 长链接 (PayPal BA approve URL, requires PayPal payment)
  - GoPay 长链接
  - Apple Pay 支付页

Usage:
    from core import parse_session_json, generate_payment_link

    access_token = parse_session_json(session_json_text)
    result = generate_payment_link(
        access_token=access_token,
        mode="无卡长链接 US/USD",
        proxy_url="http://127.0.0.1:7890",
    )
    print(result["long_url"])
"""

from __future__ import annotations

import base64
import json
import random
import re
import select
import socket
import ssl
import threading
import time
import uuid
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, unquote, urljoin, urlparse, urlsplit, urlunsplit

import requests

try:
    from curl_cffi.requests import Session as CurlCffiSession  # type: ignore
except ImportError:
    CurlCffiSession = None  # type: ignore

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)
DEFAULT_STRIPE_PK = "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n"
STRIPE_VERSION_FULL = "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
DEFAULT_STRIPE_RUNTIME_VERSION = "6f8494a281"
PAY_LONG_LINK_TIMEOUT = 30

# ---------------------------------------------------------------------------
# Payment mode definitions
# ---------------------------------------------------------------------------

PAYMENT_MODES = {
    "无卡长链接 US/USD":       {"country": "US", "currency": "USD"},
    "无卡长链接 BR/BRL":       {"country": "BR", "currency": "BRL"},
    "无卡长链接 DE/EUR":       {"country": "DE", "currency": "EUR"},
    "无卡长链接 FR/EUR":       {"country": "FR", "currency": "EUR"},
    "无卡长链接 GB/GBP":       {"country": "GB", "currency": "GBP"},
    "无卡长链接 CA/CAD":       {"country": "CA", "currency": "CAD"},
    "无卡长链接 AU/AUD":       {"country": "AU", "currency": "AUD"},
    "无卡长链接 JP/JPY":       {"country": "JP", "currency": "JPY"},
    "GoPay 长链接 ID/IDR":     {"country": "ID", "currency": "IDR"},
    "PayPal 长链接 US/USD":    {"country": "US", "currency": "USD"},
    "PayPal 长链接 FR/EUR":    {"country": "FR", "currency": "EUR"},
    "PayPal 长链接 JP/JPY":    {"country": "JP", "currency": "JPY"},
    "Apple Pay 支付页 US/USD": {"country": "US", "currency": "USD", "apple_pay_hosted": True},
    "Apple Pay 支付页 JP/JPY": {"country": "JP", "currency": "JPY", "apple_pay_hosted": True},
}

# ---------------------------------------------------------------------------
# Country / currency / locale data
# ---------------------------------------------------------------------------

COUNTRY_CURRENCY = {
    "AT": "EUR", "AU": "AUD", "BE": "EUR", "BR": "BRL", "CA": "CAD", "CH": "CHF",
    "CZ": "CZK", "DE": "EUR", "DK": "DKK", "ES": "EUR", "FI": "EUR", "FR": "EUR",
    "GB": "GBP", "HK": "HKD", "ID": "IDR", "IE": "EUR", "IN": "INR", "IT": "EUR",
    "JP": "JPY", "KR": "KRW", "MX": "MXN", "MY": "MYR", "NL": "EUR", "NO": "NOK",
    "NZ": "NZD", "PH": "PHP", "PL": "PLN", "PT": "EUR", "SE": "SEK", "SG": "SGD",
    "TH": "THB", "TW": "TWD", "US": "USD", "VN": "VND",
    "AE": "AED", "AR": "ARS", "BH": "BHD", "BM": "BMD", "BO": "BOB", "BQ": "USD",
    "CL": "CLP", "CO": "COP", "GU": "USD", "IL": "ILS", "PR": "USD", "TR": "TRY",
    "UA": "UAH", "UM": "USD", "ZA": "ZAR",
}

OPENAI_SUPPORTED_COUNTRY_CODES = {
    "AX", "AL", "DZ", "AS", "AD", "AO", "AI", "AQ", "AG", "AR",
    "AM", "AW", "AU", "AT", "AZ", "BS", "BH", "BD", "BB", "BE",
    "BZ", "BJ", "BM", "BT", "BO", "BQ", "BA", "BW", "BV", "BR",
    "IO", "BN", "BG", "BF", "BI", "CV", "KH", "CM", "CA", "KY",
    "CF", "TD", "CL", "CX", "CC", "CO", "KM", "CG", "CK", "CR",
    "CI", "HR", "CW", "CY", "CZ", "DK", "DJ", "DM", "DO", "EC",
    "SV", "GQ", "ER", "EE", "SZ", "FK", "FO", "FJ", "FI", "FR",
    "GF", "PF", "TF", "GA", "GM", "GE", "DE", "GH", "GI", "GR",
    "GL", "GD", "GP", "GU", "GT", "GG", "GN", "GW", "GY", "HT",
    "HM", "VA", "HN", "HU", "IS", "IN", "ID", "IQ", "IE", "IM",
    "IL", "IT", "JM", "JP", "JE", "JO", "KZ", "KE", "KI", "KW",
    "KG", "LA", "LV", "LB", "LS", "LR", "LI", "LT", "LU", "MG",
    "MW", "MY", "MV", "ML", "MT", "MH", "MQ", "MR", "MU", "YT",
    "MX", "FM", "MD", "MC", "MN", "ME", "MS", "MA", "MZ", "MM",
    "NA", "NR", "NP", "NL", "NC", "NZ", "NI", "NE", "NG", "NU",
    "NF", "MK", "MP", "NO", "OM", "PK", "PW", "PS", "PA", "PG",
    "PE", "PH", "PN", "PL", "PT", "PR", "QA", "RE", "RO", "RW",
    "BL", "SH", "KN", "LC", "MF", "PM", "VC", "WS", "SM", "ST",
    "SN", "RS", "SC", "SL", "SG", "SX", "SK", "SI", "SB", "SO",
    "ZA", "GS", "KR", "SS", "ES", "LK", "SR", "SJ", "SE", "CH",
    "TW", "TZ", "TH", "TL", "TG", "TK", "TO", "TT", "TN", "TR",
    "TM", "TC", "TV", "UG", "UA", "AE", "GB", "UM", "US", "UY",
    "UZ", "VU", "WF", "EH", "ZM",
}

EUR_COUNTRIES = {
    "AD", "AT", "BE", "CY", "EE", "FI", "FR", "DE", "GR", "HR",
    "IE", "IT", "LV", "LT", "LU", "MT", "MC", "ME", "NL", "PT",
    "SM", "SK", "SI", "ES",
}
COUNTRY_CURRENCY.update({country: "EUR" for country in EUR_COUNTRIES if country not in COUNTRY_CURRENCY})

COUNTRY_PHONE_PREFIX = {
    "AU": "+61", "CA": "+1", "DE": "+49", "GB": "+44", "IE": "+353", "JP": "+81",
    "NZ": "+64", "SG": "+65", "TH": "+66", "US": "+1",
    "AD": "+376", "AE": "+971", "AL": "+355", "AR": "+54", "AT": "+43", "BE": "+32",
    "BG": "+359", "BH": "+973", "BM": "+1", "BO": "+591", "BR": "+55", "CH": "+41",
    "CL": "+56", "CO": "+57", "CR": "+506", "CY": "+357", "CZ": "+420", "DK": "+45",
    "EE": "+372", "ES": "+34", "FI": "+358", "FR": "+33", "GI": "+350", "GR": "+30",
    "HK": "+852", "HU": "+36", "ID": "+62", "IL": "+972", "IN": "+91", "IS": "+354",
    "IT": "+39", "KR": "+82", "KZ": "+7", "LI": "+423", "LT": "+370", "LU": "+352",
    "LV": "+371", "MC": "+377", "MD": "+373", "ME": "+382", "MK": "+389", "MT": "+356",
    "MX": "+52", "MY": "+60", "NL": "+31", "NO": "+47", "PH": "+63", "PL": "+48",
    "PT": "+351", "QA": "+974", "RO": "+40", "RS": "+381", "SA": "+966", "SE": "+46",
    "SI": "+386", "SK": "+421", "SM": "+378", "TR": "+90", "TW": "+886", "UA": "+380",
    "UY": "+598", "ZA": "+27",
}

LOCALE_MAP = {
    "de": ("de-DE", "de"), "en": ("en-US", "en"), "en-US": ("en-US", "en"),
    "es": ("es-ES", "es"), "fr": ("fr-FR", "fr"), "id": ("id-ID", "id"),
    "it": ("it-IT", "it"), "ja": ("ja-JP", "ja"), "ko": ("ko-KR", "ko"),
    "pt-BR": ("pt-BR", "pt-BR"), "zh-CN": ("zh-CN", "zh-CN"), "zh-TW": ("zh-TW", "zh-TW"),
}

# ---------------------------------------------------------------------------
# Billing data pools (randomized per request)
# ---------------------------------------------------------------------------

US_BILLING_NAMES = [
    ("James", "Smith"), ("John", "Brown"), ("Michael", "Johnson"),
    ("Robert", "Miller"), ("David", "Davis"), ("William", "Wilson"),
]
US_BILLING_STREETS = [
    ("3110 Sunset Boulevard", "Los Angeles", "CA", "90026"),
    ("1200 Market Street", "San Francisco", "CA", "94102"),
    ("500 Main Street", "Austin", "TX", "78701"),
    ("88 Broadway", "New York", "NY", "10007"),
    ("1200 Peachtree St", "Atlanta", "GA", "30309"),
]

DE_BILLING_NAMES = [
    ("Lukas", "Schneider"), ("Felix", "Muller"), ("Jonas", "Weber"),
    ("Leon", "Fischer"), ("Marie", "Wagner"), ("Laura", "Becker"),
    ("Maximilian", "Hoffmann"), ("Paul", "Schulz"), ("Emma", "Koch"),
    ("Hannah", "Bauer"), ("Sophie", "Richter"), ("Noah", "Klein"),
]
DE_BILLING_STREETS = [
    ("Friedrichstrasse 123", "Berlin", "BE", "10117"),
    ("Leopoldstrasse 50", "Munich", "BY", "80802"),
    ("Zeil 85", "Frankfurt am Main", "HE", "60313"),
    ("Konigsallee 60", "Dusseldorf", "NW", "40212"),
    ("Moenckebergstrasse 7", "Hamburg", "HH", "20095"),
    ("Hohenzollernring 72", "Cologne", "NW", "50672"),
    ("Kaiserstrasse 44", "Stuttgart", "BW", "70173"),
    ("Kaufingerstrasse 15", "Munich", "BY", "80331"),
    ("Georgstrasse 24", "Hanover", "NI", "30159"),
    ("Prager Strasse 9", "Dresden", "SN", "01069"),
    ("Schadowstrasse 36", "Dusseldorf", "NW", "40212"),
    ("Breite Strasse 18", "Bonn", "NW", "53111"),
]

GB_BILLING_NAMES = [
    ("Oliver", "Smith"), ("George", "Taylor"), ("Harry", "Brown"),
    ("Noah", "Wilson"), ("Jack", "Davies"), ("Arthur", "Evans"),
    ("Olivia", "Johnson"), ("Amelia", "Roberts"), ("Isla", "Walker"),
    ("Ava", "Thompson"), ("Mia", "White"), ("Grace", "Hughes"),
]
GB_BILLING_STREETS = [
    ("221B Baker Street", "London", "England", "NW1 6XE"),
    ("10 Downing Street", "London", "England", "SW1A 2AA"),
    ("45 Deansgate", "Manchester", "England", "M3 2AY"),
    ("18 Park Row", "Leeds", "England", "LS1 5JA"),
    ("77 Queen Street", "Cardiff", "Wales", "CF10 2GR"),
    ("9 Princes Street", "Edinburgh", "Scotland", "EH2 2ER"),
    ("33 Broad Street", "Birmingham", "England", "B1 2HF"),
    ("14 Castle Street", "Liverpool", "England", "L2 0NE"),
    ("52 College Green", "Bristol", "England", "BS1 5SH"),
    ("6 Royal Avenue", "Belfast", "Northern Ireland", "BT1 1DA"),
]

AU_BILLING_NAMES = [
    ("Jack", "Wilson"), ("Oliver", "Taylor"), ("Noah", "Brown"),
    ("Charlotte", "Smith"), ("Amelia", "Jones"), ("Isla", "Williams"),
]
AU_BILLING_STREETS = [
    ("120 Collins Street", "Melbourne", "Victoria", "3000"),
    ("88 George Street", "Sydney", "New South Wales", "2000"),
    ("45 Queen Street", "Brisbane", "Queensland", "4000"),
    ("22 King William Street", "Adelaide", "South Australia", "5000"),
    ("60 St Georges Terrace", "Perth", "Western Australia", "6000"),
    ("18 Elizabeth Street", "Hobart", "Tasmania", "7000"),
]

EXTRA_BILLING_NAMES = [
    ("Alex", "Tan"), ("Daniel", "Lee"), ("Emma", "Wong"),
    ("Mia", "Chen"), ("Noah", "Martin"), ("Olivia", "Nguyen"),
]
EXTRA_BILLING_STREETS = {
    "TH": [
        ("999 Rama I Road", "Bangkok", "Bangkok", "10330"),
        ("88 Sukhumvit Road", "Bangkok", "Bangkok", "10110"),
        ("45 Nimman Road", "Chiang Mai", "Chiang Mai", "50200"),
    ],
    "JP": [
        ("1-1 Marunouchi", "Chiyoda-ku", "Tokyo", "100-0005"),
        ("2-2-1 Yaesu", "Chuo-ku", "Tokyo", "104-0028"),
        ("3-1 Umeda", "Osaka", "Osaka", "530-0001"),
    ],
    "SG": [
        ("10 Anson Road", "Singapore", "Singapore", "079903"),
        ("1 Raffles Place", "Singapore", "Singapore", "048616"),
        ("80 Robinson Road", "Singapore", "Singapore", "068898"),
    ],
    "NZ": [
        ("22 Queen Street", "Auckland", "Auckland", "1010"),
        ("50 Lambton Quay", "Wellington", "Wellington", "6011"),
        ("120 Hereford Street", "Christchurch", "Canterbury", "8011"),
    ],
    "CA": [
        ("100 King Street West", "Toronto", "ON", "M5X 1A9"),
        ("555 West Hastings Street", "Vancouver", "BC", "V6B 4N6"),
        ("1250 Rene-Levesque Blvd", "Montreal", "QC", "H3B 4W8"),
    ],
    "IE": [
        ("1 Grand Canal Square", "Dublin", "Dublin", "D02 P820"),
        ("10 South Mall", "Cork", "Cork", "T12 RD43"),
        ("5 Eyre Square", "Galway", "Galway", "H91 FPK2"),
    ],
}

BILLING_PROFILE_CITY_BY_COUNTRY = {
    "AT": ["Vienna", "Graz", "Linz"], "BE": ["Brussels", "Antwerp", "Ghent"],
    "BR": ["Sao Paulo", "Rio de Janeiro", "Brasilia"],
    "CH": ["Zurich", "Geneva", "Basel"], "DK": ["Copenhagen", "Aarhus", "Odense"],
    "ES": ["Madrid", "Barcelona", "Valencia"],
    "FI": ["Helsinki", "Espoo", "Tampere"], "FR": ["Paris", "Lyon", "Marseille"],
    "ID": ["Jakarta", "Surabaya", "Bandung"],
    "IT": ["Rome", "Milan", "Turin"], "KR": ["Seoul", "Busan", "Incheon"],
    "MX": ["Mexico City", "Guadalajara", "Monterrey"],
    "NL": ["Amsterdam", "Rotterdam", "Utrecht"], "NO": ["Oslo", "Bergen", "Trondheim"],
    "PL": ["Warsaw", "Krakow", "Gdansk"],
    "PT": ["Lisbon", "Porto", "Coimbra"],
    "SE": ["Stockholm", "Gothenburg", "Malmo"],
    "TW": ["Taipei", "Taichung", "Kaohsiung"],
}

POSTAL_PATTERN_BY_COUNTRY = {
    "AD": "AD###", "AR": "C####", "AU": "####", "AT": "####", "BE": "####",
    "BR": "#####-###", "CA": "A#A #A#", "CH": "####", "CL": "#######",
    "CZ": "### ##", "DE": "#####", "DK": "####", "ES": "#####", "FI": "#####",
    "FR": "#####", "GB": "AA# #AA", "IE": "A## A###", "ID": "#####",
    "IN": "######", "IT": "#####", "JP": "###-####", "KR": "#####",
    "MX": "#####", "NL": "#### AA", "NO": "####", "NZ": "####",
    "PL": "##-###", "PT": "####-###", "SE": "### ##", "SG": "######",
    "TH": "#####", "US": "#####",
}

BILLING_STREET_POOL = ["Market Street", "Central Avenue", "Station Road", "Main Street", "High Street", "King Street"]
BILLING_PROFILE_BY_COUNTRY = {
    country: {
        "currency": COUNTRY_CURRENCY.get(country, "USD"),
        "phone_prefix": COUNTRY_PHONE_PREFIX.get(country, "+1"),
        "city_pool": BILLING_PROFILE_CITY_BY_COUNTRY.get(country, ["Capital City", "Central District", "Market Town"]),
        "postal_pattern": POSTAL_PATTERN_BY_COUNTRY.get(country, "#####"),
        "street_pool": BILLING_STREET_POOL,
    }
    for country in OPENAI_SUPPORTED_COUNTRY_CODES
}


# ===================================================================
# Session parsing
# ===================================================================

def find_access_token(value) -> str:
    """Recursively search a dict/list for accessToken/access_token/token."""
    if isinstance(value, dict):
        for key in ("accessToken", "access_token", "token"):
            token = str(value.get(key) or "").strip()
            if token:
                return token
        for item in value.values():
            token = find_access_token(item)
            if token:
                return token
    if isinstance(value, list):
        for item in value:
            token = find_access_token(item)
            if token:
                return token
    return ""


def parse_session_json(text: str) -> str:
    """
    Extract access token from Session JSON text or raw Bearer token.
    Returns the access_token string, or empty string if not found.
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    if raw.startswith("Bearer "):
        return raw.split(None, 1)[1].strip()
    try:
        return find_access_token(json.loads(raw))
    except Exception:
        pass
    match = re.search(r'"(?:accessToken|access_token|token)"\s*:\s*"([^"]+)"', raw)
    if match:
        return match.group(1).strip()
    return raw if raw.count(".") >= 2 and len(raw) > 80 else ""


# ===================================================================
# Helpers
# ===================================================================

def currency_for_country(country: str) -> str:
    return COUNTRY_CURRENCY.get(str(country or "").upper(), "USD")


def normalize_opll_country(country: str) -> str:
    country = str(country or "").strip().upper()
    return country if country in OPENAI_SUPPORTED_COUNTRY_CODES else "US"


def locale_parts(locale: str = "en") -> tuple[str, str]:
    return LOCALE_MAP.get(str(locale or "").strip(), LOCALE_MAP["en"])


def opll_short_error(detail: str, limit: int = 260) -> str:
    text = re.sub(r"\s+", " ", str(detail or "")).strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def opll_first_non_empty(values: dict, *keys: str) -> str:
    for key in keys:
        value = str(values.get(key) or "").strip()
        if value:
            return value
    return ""


def opll_random_postal_code(pattern: str) -> str:
    result = []
    for char in str(pattern or "#####"):
        if char == "#":
            result.append(str(random.randint(0, 9)))
        elif char == "A":
            result.append(chr(random.randint(ord("A"), ord("Z"))))
        else:
            result.append(char)
    return "".join(result)


# ===================================================================
# Proxy helpers
# ===================================================================

def normalize_proxy_url(value: str, default_scheme: str = "http") -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = f"{default_scheme}://{text}"
    return text


def mask_proxy_url(proxy_url: str) -> str:
    text = str(proxy_url or "").strip()
    if not text:
        return "直连"
    try:
        parsed = urlsplit(text)
        if "@" not in parsed.netloc:
            return text
        userinfo, host = parsed.netloc.rsplit("@", 1)
        if ":" in userinfo:
            username, _password = userinfo.split(":", 1)
            userinfo = f"{username}:***"
        else:
            userinfo = "***"
        return urlunsplit((parsed.scheme, f"{userinfo}@{host}", parsed.path, parsed.query, parsed.fragment))
    except Exception:
        return re.sub(r":([^:@/]+)@", ":***@", text)


# ===================================================================
# HTTP session factories
# ===================================================================


def random_proxy_sid(length: int = 10) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(random.choice(alphabet) for _ in range(length))


def randomize_proxy_sid(proxy_url: str) -> str:
    text = str(proxy_url or "").strip()
    if not text:
        return ""
    sid = random_proxy_sid()
    parsed = urlsplit(text)
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if any(key.lower() == "sid" for key, _value in query_pairs):
        query = urlencode([(key, sid if key.lower() == "sid" else value) for key, value in query_pairs])
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))
    netloc = parsed.netloc
    if "@" in netloc:
        userinfo, host = netloc.rsplit("@", 1)
        new_userinfo = re.sub(r"(?i)(sid[-_=])([^-:@;&/?]+)", lambda m: f"{m.group(1)}{sid}", userinfo, count=1)
        if new_userinfo != userinfo:
            return urlunsplit((parsed.scheme, f"{new_userinfo}@{host}", parsed.path, parsed.query, parsed.fragment))
    new_text = re.sub(r"(?i)(sid[-_=])([^-:@;&/?]+)", lambda m: f"{m.group(1)}{sid}", text, count=1)
    return new_text

def opll_new_http_session() -> requests.Session:
    if CurlCffiSession is not None:
        session = CurlCffiSession(impersonate="chrome136")  # type: ignore[assignment]
    else:
        session = requests.Session()
    if hasattr(session, "trust_env"):
        session.trust_env = False
    return session


def opll_build_chatgpt_session(access_token: str, proxy_url: str = "") -> requests.Session:
    token = parse_session_json(access_token) or str(access_token or "").strip()
    if not token:
        raise RuntimeError("当前账号没有 Access Token，请先提供 Session 信息")
    device_id = str(uuid.uuid4())
    session = opll_new_http_session()
    session.headers.update({
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Authorization": f"Bearer {token}",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "Content-Type": "application/json",
        "oai-device-id": device_id,
        "oai-language": "en-US",
        "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "Cookie": f"oai-did={device_id}",
    })
    if proxy_url:
        session.proxies.update({"http": proxy_url, "https": proxy_url})
    return session


def opll_build_stripe_session(proxy_url: str = "") -> requests.Session:
    session = opll_new_http_session()
    session.headers.update({"User-Agent": DEFAULT_USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    if proxy_url:
        session.proxies.update({"http": proxy_url, "https": proxy_url})
    return session


# ===================================================================
# Checkout creation (OpenAI backend)
# ===================================================================

def opll_extract_processor_entity(data) -> str:
    if not isinstance(data, dict):
        return ""
    direct = data.get("processor_entity") or data.get("processorEntity")
    if direct:
        return str(direct).strip()
    for key in ("checkout_session", "session", "checkout", "data"):
        nested = data.get(key)
        if isinstance(nested, dict):
            found = opll_extract_processor_entity(nested)
            if found:
                return found
    return ""


def opll_extract_stripe_publishable_key(data) -> str:
    if isinstance(data, str):
        match = re.search(r"pk_live_[A-Za-z0-9]+", data)
        return match.group(0) if match else ""
    if isinstance(data, dict):
        for key in ("stripe_publishable_key", "publishable_key", "publishableKey",
                     "stripePublishableKey", "key"):
            found = opll_extract_stripe_publishable_key(data.get(key))
            if found:
                return found
        for item in data.values():
            found = opll_extract_stripe_publishable_key(item)
            if found:
                return found
    if isinstance(data, list):
        for item in data:
            found = opll_extract_stripe_publishable_key(item)
            if found:
                return found
    return ""


def opll_processor_entity_for_country(country: str, processor_entity: str = "") -> str:
    entity = str(processor_entity or "").strip()
    if entity:
        return entity
    return "openai_llc" if str(country or "").upper() == "US" else "openai_ie"


def opll_chatgpt_success_return_url(cs_id: str, country: str, processor_entity: str = "") -> str:
    entity = opll_processor_entity_for_country(country, processor_entity)
    return f"https://chatgpt.com/checkout/verify?stripe_session_id={cs_id}&processor_entity={entity}&plan_type=plus"


def opll_create_checkout(access_token: str, country: str, currency: str, proxy_url: str = "") -> dict:
    country = normalize_opll_country(country)
    currency = currency_for_country(country)
    session = opll_build_chatgpt_session(access_token, proxy_url)
    response = session.post(
        "https://chatgpt.com/backend-api/payments/checkout",
        json={
            "entry_point": "all_plans_pricing_modal",
            "plan_name": "chatgptplusplan",
            "billing_details": {"country": country, "currency": currency},
            "promo_campaign": {"promo_campaign_id": "plus-1-month-free", "is_coupon_from_query_param": False},
            "checkout_ui_mode": "custom",
        },
        headers={
            "Referer": "https://chatgpt.com/",
            "x-openai-target-path": "/backend-api/payments/checkout",
            "x-openai-target-route": "/backend-api/payments/checkout",
        },
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"checkout create failed: HTTP {response.status_code} {response.text[:500]}")
    data = response.json() or {}
    cs_id = data.get("checkout_session_id") or data.get("session_id") or data.get("id")
    if not cs_id or not str(cs_id).startswith("cs_"):
        raise RuntimeError(f"checkout response missing cs_id: {str(data)[:500]}")
    return {
        "cs_id": str(cs_id),
        "processor_entity": opll_extract_processor_entity(data),
        "stripe_publishable_key": opll_extract_stripe_publishable_key(data),
        "billing_country": country,
        "currency": currency,
    }


# ===================================================================
# Stripe operations
# ===================================================================

def opll_stripe_key_for_checkout(checkout: dict | None = None) -> str:
    return str((checkout or {}).get("stripe_publishable_key") or "").strip() or DEFAULT_STRIPE_PK


def opll_stripe_init(cs_id: str, country: str, currency: str,
                     proxy_url: str = "", payment_locale: str = "en",
                     stripe: requests.Session | None = None,
                     ctx: dict | None = None,
                     checkout: dict | None = None) -> dict:
    browser_locale, elements_locale = locale_parts(payment_locale)
    stripe_pk = opll_stripe_key_for_checkout(checkout)
    stripe_session = stripe or requests.Session()
    if stripe is None:
        stripe_session.headers.update({"User-Agent": DEFAULT_USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
        if hasattr(stripe_session, "trust_env"):
            stripe_session.trust_env = False
        if proxy_url:
            stripe_session.proxies.update({"http": proxy_url, "https": proxy_url})
    response = stripe_session.post(
        f"https://api.stripe.com/v1/payment_pages/{cs_id}/init",
        data={
            "browser_locale": browser_locale,
            "browser_timezone": "Asia/Shanghai",
            "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
            "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[stripe_js_id]": str((ctx or {}).get("stripe_js_id") or uuid.uuid4()),
            "elements_session_client[locale]": elements_locale,
            "elements_session_client[is_aggregation_expected]": "false",
            "elements_options_client[saved_payment_method][enable_save]": "never",
            "elements_options_client[saved_payment_method][enable_redisplay]": "never",
            "key": stripe_pk,
            "_stripe_version": STRIPE_VERSION_FULL,
        },
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"stripe init failed: HTTP {response.status_code} {response.text[:500]}")
    return response.json() or {}


def opll_stripe_context(init_payload: dict, payment_locale: str = "en", ctx: dict | None = None) -> dict:
    _browser_locale, elements_locale = locale_parts(payment_locale)
    base = ctx or {}
    return {
        "stripe_js_id": str(base.get("stripe_js_id") or uuid.uuid4()),
        "elements_session_id": str(base.get("elements_session_id") or f"elements_session_{uuid.uuid4().hex[:11]}"),
        "elements_session_config_id": str(init_payload.get("config_id") or base.get("elements_session_config_id") or uuid.uuid4()),
        "config_id": str(init_payload.get("config_id") or ""),
        "init_checksum": str(init_payload.get("init_checksum") or ""),
        "checkout_amount": str(opll_expected_amount(init_payload)),
        "currency": str(init_payload.get("currency") or "").lower(),
        "locale": elements_locale,
        "runtime_version": str(base.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION),
    }


def opll_expected_amount(init_payload: dict) -> str:
    return opll_stripe_amount_info(init_payload)[0]


def opll_stripe_amount_info(init_payload) -> tuple[str, str]:
    if not isinstance(init_payload, dict):
        return "0", "missing_payload"
    total_summary = init_payload.get("total_summary") if isinstance(init_payload, dict) else None
    if isinstance(total_summary, dict) and total_summary.get("due") is not None:
        return str(total_summary.get("due")), "total_summary.due"
    invoice = init_payload.get("invoice") if isinstance(init_payload, dict) else None
    if isinstance(invoice, dict) and invoice.get("amount_due") is not None:
        return str(invoice.get("amount_due")), "invoice.amount_due"
    line_items = init_payload.get("line_items") if isinstance(init_payload, dict) else None
    if isinstance(line_items, list):
        total = 0
        found = False
        for item in line_items:
            if isinstance(item, dict) and item.get("amount") is not None:
                try:
                    total += int(item.get("amount") or 0)
                    found = True
                except Exception:
                    pass
        if found:
            return str(total), "line_items.amount"
    return "0", "fallback_zero"


def opll_amount_is_zero(amount: str) -> bool:
    try:
        return int(str(amount or "").strip()) == 0
    except Exception:
        return False


def opll_require_zero_amount(init_payload: dict, currency: str = "") -> tuple[str, str]:
    amount, source = opll_stripe_amount_info(init_payload)
    if not opll_amount_is_zero(amount):
        currency_part = f", currency={currency}" if currency else ""
        raise RuntimeError(
            f"金额非 0，跳过本次提取并重试: amount={amount}, source={source}{currency_part}"
        )
    return amount, source


# ===================================================================
# Billing info generation
# ===================================================================

def opll_billing_for_country(country: str) -> dict:
    country = normalize_opll_country(country)
    if country == "DE":
        first, last = random.choice(DE_BILLING_NAMES)
        line1, city, state, postal = random.choice(DE_BILLING_STREETS)
    elif country == "GB":
        first, last = random.choice(GB_BILLING_NAMES)
        line1, city, state, postal = random.choice(GB_BILLING_STREETS)
    elif country == "AU":
        first, last = random.choice(AU_BILLING_NAMES)
        line1, city, state, postal = random.choice(AU_BILLING_STREETS)
    elif country == "US":
        first, last = random.choice(US_BILLING_NAMES)
        line1, city, state, postal = random.choice(US_BILLING_STREETS)
    elif country in EXTRA_BILLING_STREETS:
        first, last = random.choice(EXTRA_BILLING_NAMES)
        line1, city, state, postal = random.choice(EXTRA_BILLING_STREETS[country])
    elif country in OPENAI_SUPPORTED_COUNTRY_CODES:
        profile = BILLING_PROFILE_BY_COUNTRY[country]
        first, last = random.choice(EXTRA_BILLING_NAMES)
        line1 = f"{random.randint(10, 999)} {random.choice(profile['street_pool'])}"
        city = random.choice(profile["city_pool"])
        state = country
        postal = opll_random_postal_code(str(profile.get("postal_pattern") or "#####"))
    else:
        raise RuntimeError(f"不支持的账单资料地区: {country}")
    suffix = random.randint(1000, 9999)
    phone_prefix = str(BILLING_PROFILE_BY_COUNTRY.get(country, {}).get("phone_prefix")
                       or COUNTRY_PHONE_PREFIX.get(country, "+1"))
    return {
        "name": f"{first} {last}",
        "email": f"{first.lower()}.{last.lower()}{suffix}@example.com",
        "phone": f"{phone_prefix}{random.randint(100000000, 999999999)}",
        "country": country,
        "line1": line1,
        "city": city,
        "state": state,
        "postal_code": postal,
    }


# ===================================================================
# Stripe payment method (PayPal)
# ===================================================================

def opll_stripe_create_paypal_method(stripe: requests.Session, cs_id: str, ctx: dict,
                                      billing: dict, stripe_pk: str = "") -> str:
    runtime_version = str(ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION)
    body = {
        "billing_details[name]": billing.get("name") or "John Doe",
        "billing_details[email]": billing.get("email") or "buyer@example.com",
        "billing_details[phone]": billing.get("phone") or "",
        "billing_details[address][country]": billing.get("country") or "US",
        "billing_details[address][line1]": billing.get("line1") or "3110 Sunset Boulevard",
        "billing_details[address][city]": billing.get("city") or "Los Angeles",
        "billing_details[address][postal_code]": billing.get("postal_code") or "90026",
        "billing_details[address][state]": billing.get("state") or "CA",
        "type": "paypal",
        "payment_user_agent": f"stripe.js/{runtime_version}; stripe-js-v3/{runtime_version}; payment-element; deferred-intent",
        "referrer": "https://chatgpt.com",
        "time_on_page": str(random.randint(25000, 55000)),
        "client_attribution_metadata[checkout_session_id]": cs_id,
        "client_attribution_metadata[client_session_id]": ctx["stripe_js_id"],
        "client_attribution_metadata[checkout_config_id]": ctx.get("config_id") or "",
        "client_attribution_metadata[elements_session_id]": ctx["elements_session_id"],
        "client_attribution_metadata[elements_session_config_id]": ctx["elements_session_config_id"],
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "2021",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "key": stripe_pk or DEFAULT_STRIPE_PK,
        "_stripe_version": STRIPE_VERSION_FULL,
    }
    response = stripe.post("https://api.stripe.com/v1/payment_methods", data=body, timeout=PAY_LONG_LINK_TIMEOUT)
    if response.status_code >= 400:
        raise RuntimeError(f"stripe payment_methods failed: HTTP {response.status_code} {response.text[:500]}")
    pm_id = str((response.json() or {}).get("id") or "")
    if not pm_id.startswith("pm_"):
        raise RuntimeError(f"stripe payment_methods bad response: {response.text[:300]}")
    return pm_id


# ===================================================================
# ChatGPT approve
# ===================================================================

class OpllStripeRequiresApproval(Exception):
    pass


class OpllChatgptApproveBlocked(Exception):
    pass


OPLL_APPROVE_BURST_RESULTS = {"blocked", "exception"}


def opll_chatgpt_approve(chatgpt: requests.Session, cs_id: str, checkout: dict) -> None:
    entity = opll_processor_entity_for_country(checkout["billing_country"], checkout.get("processor_entity", ""))
    try:
        chatgpt.post(
            "https://chatgpt.com/backend-api/sentinel/ping",
            json={},
            headers={
                "Referer": "https://chatgpt.com/",
                "x-openai-target-path": "/backend-api/sentinel/ping",
                "x-openai-target-route": "/backend-api/sentinel/ping",
            },
            timeout=PAY_LONG_LINK_TIMEOUT,
        )
    except Exception:
        pass
    response = chatgpt.post(
        "https://chatgpt.com/backend-api/payments/checkout/approve",
        json={"checkout_session_id": cs_id, "processor_entity": entity},
        headers={
            "Referer": f"https://chatgpt.com/checkout/{entity}/{cs_id}",
            "x-openai-target-path": "/backend-api/payments/checkout/approve",
            "x-openai-target-route": "/backend-api/payments/checkout/approve",
        },
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"chatgpt approve failed: HTTP {response.status_code} {response.text[:500]}")
    try:
        result = (response.json() or {}).get("result")
    except Exception:
        result = ""
    normalized_result = str(result or "").strip().lower()
    if normalized_result in OPLL_APPROVE_BURST_RESULTS:
        raise OpllChatgptApproveBlocked(f"chatgpt approve retryable result: {normalized_result!r}")
    if result != "approved":
        raise RuntimeError(f"chatgpt approve unexpected result: {result!r}")


def opll_chatgpt_approve_with_retry(access_token: str, cs_id: str, checkout: dict,
                                     proxy_url: str = "") -> requests.Session:
    last_error = ""
    for _ in range(3):
        try:
            chatgpt = opll_build_chatgpt_session(access_token, proxy_url)
            opll_chatgpt_approve(chatgpt, cs_id, checkout)
            return chatgpt
        except OpllChatgptApproveBlocked as exc:
            last_error = str(exc)
            break
        except Exception as exc:
            last_error = str(exc)
            time.sleep(1)
    raise RuntimeError(f"ChatGPT approve 连续失败: {last_error}")


# ===================================================================
# Stripe redirect + confirm
# ===================================================================

def opll_is_external_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except Exception:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def opll_is_paypal_url(value: str) -> bool:
    host = (urlsplit(value).netloc or "").lower()
    return host == "paypal.com" or host.endswith(".paypal.com") or \
           host == "paypalobjects.com" or host.endswith(".paypalobjects.com")


def opll_is_paypal_ba_approve_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    if not (host == "paypal.com" or host.endswith(".paypal.com")):
        return False
    path = parsed.path.rstrip("/").lower()
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    return path == "/agreements/approve" and bool(str(query.get("ba_token") or "").strip())


def opll_is_ignored_resource_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except Exception:
        return False
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    ignored_hosts = {"stripe-camo.global.ssl.fastly.net", "files.stripe.com",
                     "q.stripe.com", "js.stripe.com", "m.stripe.network"}
    ignored_suffixes = (".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif", ".ico",
                        ".css", ".js", ".woff", ".woff2")
    if host in ignored_hosts or any(host.endswith(f".{item}") for item in ignored_hosts):
        return True
    return path.endswith(ignored_suffixes)


def opll_collect_urls(payload, urls: list[str] | None = None) -> list[str]:
    found = urls if urls is not None else []
    if isinstance(payload, str):
        for match in re.findall(r"https?://[^\s\"'<>]+", payload):
            found.append(match.rstrip("),.;]"))
    elif isinstance(payload, dict):
        for key, value in payload.items():
            if key in ("url", "return_url", "redirect_url", "redirect_to_url") and \
               isinstance(value, str) and opll_is_external_url(value):
                found.append(value)
            else:
                opll_collect_urls(value, found)
    elif isinstance(payload, list):
        for item in payload:
            opll_collect_urls(item, found)
    return found


def opll_extract_redirect_to_url(payload) -> str:
    if not isinstance(payload, dict):
        urls = opll_collect_urls(payload)
        return next(
            (item for item in urls if opll_is_paypal_ba_approve_url(item)),
            next((item for item in urls if opll_is_paypal_url(item) and not opll_is_ignored_resource_url(item)), ""),
        )
    next_action = payload.get("next_action")
    if isinstance(next_action, dict) and next_action.get("type") == "redirect_to_url":
        redirect_to_url = next_action.get("redirect_to_url") or {}
        if isinstance(redirect_to_url, dict):
            url = str(redirect_to_url.get("url") or "").strip()
            if url:
                return url
    for key in ("setup_intent", "payment_intent"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            found = opll_extract_redirect_to_url(nested)
            if found:
                return found
    urls = opll_collect_urls(payload)
    return next(
        (item for item in urls if opll_is_paypal_ba_approve_url(item)),
        next((item for item in urls if opll_is_paypal_url(item) and not opll_is_ignored_resource_url(item)), ""),
    )


def opll_submission_attempt_failure_fields(submission) -> dict[str, str]:
    wanted = {"error", "code", "message", "reason", "failure_reason", "decline_code",
              "failure_code", "failure_message"}
    found: dict[str, str] = {}

    def walk(value) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key or "").strip()
                if normalized in wanted and normalized not in found:
                    if isinstance(item, (str, int, float, bool)):
                        text = str(item).strip()
                    elif isinstance(item, dict):
                        text = str(item.get("message") or item.get("code") or
                                   item.get("reason") or item.get("type") or "").strip()
                    else:
                        text = ""
                    if text:
                        found[normalized] = text[:240]
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    if isinstance(submission, dict):
        walk(submission)
    return found


def opll_find_submission_attempt(payload) -> dict:
    if isinstance(payload, dict):
        item = payload.get("submission_attempt")
        if isinstance(item, dict):
            return item
        for value in payload.values():
            found = opll_find_submission_attempt(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = opll_find_submission_attempt(value)
            if found:
                return found
    return {}


def opll_stripe_error_summary(prefix: str, response) -> str:
    try:
        payload = response.json() or {}
    except Exception:
        payload = {}
    error = payload.get("error") if isinstance(payload, dict) else {}
    if not isinstance(error, dict):
        error = {}
    extra_fields = error.get("extra_fields") if isinstance(error.get("extra_fields"), dict) else {}
    parts = []
    for label, value in (
        ("code", error.get("code")),
        ("decline_code", error.get("decline_code")),
        ("type", error.get("type")),
        ("message", error.get("message")),
        ("payment_method_type", extra_fields.get("payment_method_type")),
        ("confirm_error_reason", extra_fields.get("confirm_error_reason")),
        ("confirm_error_code", extra_fields.get("confirm_error_code")),
        ("confirm_error_message", extra_fields.get("confirm_error_message")),
    ):
        if value is not None and value != "":
            parts.append(f"{label}={opll_short_error(str(value), 180)}")
    if parts:
        return f"{prefix}: " + ", ".join(parts)
    return f"{prefix}: {opll_short_error(response.text, 500)}"


def opll_stripe_payload_diagnostics(payload, ctx: dict) -> str:
    if not isinstance(payload, dict):
        return f"payload_type={type(payload).__name__}"
    keys = ",".join(sorted(payload.keys())[:12])
    urls = opll_collect_urls(payload)
    paypal_count = sum(1 for item in urls if opll_is_paypal_url(item))
    ba_count = sum(1 for item in urls if opll_is_paypal_ba_approve_url(item))
    ignored_count = sum(1 for item in urls if opll_is_ignored_resource_url(item))
    submission = opll_find_submission_attempt(payload)
    submission_state = str(submission.get("state") or "") if isinstance(submission, dict) else ""
    submission_fields = opll_submission_attempt_failure_fields(submission)
    submission_reason = opll_first_non_empty(submission_fields, "reason", "failure_reason",
                                              "decline_code", "failure_code", "code")
    submission_code = opll_first_non_empty(submission_fields, "code", "decline_code", "failure_code")
    submission_message = opll_first_non_empty(submission_fields, "message", "failure_message", "error")
    return (
        f"keys=[{keys}], urls={len(urls)}, paypal_urls={paypal_count}, "
        f"ba_approve_urls={ba_count}, ignored_resource_urls={ignored_count}, "
        f"submission_attempt={bool(submission)}, submission_state={submission_state or '未知'}, "
        f"submission_reason={submission_reason or '无'}, submission_code={submission_code or '无'}, "
        f"submission_message={submission_message or '无'}, ctx_session={ctx.get('elements_session_id') or ''}"
    )


def opll_stripe_payment_page_redirect_url(stripe: requests.Session, cs_id: str, stripe_pk: str,
                                           payment_locale: str = "en", timeout_seconds: int = 45,
                                           ctx: dict | None = None) -> str:
    deadline = time.time() + max(1, timeout_seconds)
    _browser_locale, elements_locale = locale_parts(payment_locale)
    ctx = ctx or {}
    params = {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[session_id]": str(ctx.get("elements_session_id") or f"elements_session_{uuid.uuid4().hex[:11]}"),
        "elements_session_client[stripe_js_id]": str(ctx.get("stripe_js_id") or uuid.uuid4()),
        "elements_session_client[locale]": elements_locale,
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "never",
        "elements_options_client[saved_payment_method][enable_redisplay]": "never",
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION_FULL,
    }
    last_err = ""
    while time.time() < deadline:
        response = stripe.get(
            f"https://api.stripe.com/v1/payment_pages/{cs_id}",
            params=params,
            timeout=PAY_LONG_LINK_TIMEOUT,
        )
        if response.status_code == 200:
            payload = response.json() or {}
            redirect_url = opll_extract_redirect_to_url(payload)
            if redirect_url:
                return redirect_url
            submission = opll_find_submission_attempt(payload)
            if submission.get("state") == "requires_approval":
                raise OpllStripeRequiresApproval("payment page requires ChatGPT approval")
            if submission.get("state") == "failed":
                raise RuntimeError(f"stripe submission failed: {opll_stripe_payload_diagnostics(payload, ctx)}")
            last_err = opll_stripe_payload_diagnostics(payload, ctx)
        else:
            last_err = f"HTTP {response.status_code} {response.text[:120]}"
        time.sleep(1)
    raise RuntimeError(f"redirect url resolution timeout: {last_err}")


def opll_resolve_external_redirect(stripe: requests.Session, redirect_url: str,
                                    preferred_hosts: tuple[str, ...] = ("paypal.com",)) -> str:
    current = str(redirect_url or "").strip()
    for _ in range(5):
        if not current:
            return ""
        if opll_is_paypal_ba_approve_url(current):
            return current
        host = (urlsplit(current).netloc or "").lower()
        if preferred_hosts and any(host == item or host.endswith(f".{item}") for item in preferred_hosts):
            return current
        try:
            response = stripe.get(current, allow_redirects=False, timeout=PAY_LONG_LINK_TIMEOUT)
        except Exception:
            return current
        if response.status_code not in (301, 302, 303, 307, 308):
            return current
        location = str(response.headers.get("Location") or "").strip()
        if not location:
            return current
        current = urljoin(current, location)
    return current


def opll_to_openai_pay_url(stripe_hosted_url: str) -> str:
    url = str(stripe_hosted_url or "").strip()
    if not url:
        return ""
    if url.startswith("https://checkout.stripe.com"):
        return "https://pay.openai.com" + url[len("https://checkout.stripe.com"):]
    parsed = urlsplit(url)
    if parsed.netloc.lower() == "checkout.stripe.com":
        return urlunsplit((parsed.scheme or "https", "pay.openai.com", parsed.path, parsed.query, parsed.fragment))
    return url


def opll_stripe_checkout_long_url(cs_id: str, country: str, processor_entity: str = "") -> str:
    return (
        f"https://checkout.stripe.com/c/pay/{cs_id}"
        f"?returned_from_redirect=true&ui_mode=custom&return_url="
        f"{quote(opll_chatgpt_success_return_url(cs_id, country, processor_entity), safe='')}"
    )


def opll_stripe_confirm_return_url(cs_id: str, checkout: dict, stripe_hosted_url: str) -> str:
    hosted_url = opll_to_openai_pay_url(stripe_hosted_url) or opll_stripe_checkout_long_url(
        cs_id, checkout["billing_country"], checkout.get("processor_entity", ""))
    if "pay.openai.com/" in hosted_url or "checkout.stripe.com/" in hosted_url:
        parsed = urlsplit(hosted_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query.setdefault("success_return_url",
                         opll_chatgpt_success_return_url(cs_id, checkout["billing_country"],
                                                          checkout.get("processor_entity", "")))
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
    return hosted_url


def opll_stripe_confirm(stripe: requests.Session, cs_id: str, pm_id: str, stripe_pk: str,
                         init_payload: dict, ctx: dict, checkout: dict, stripe_hosted_url: str) -> dict:
    return_url = opll_stripe_confirm_return_url(cs_id, checkout, stripe_hosted_url)
    runtime_version = str(ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION)
    response = stripe.post(
        f"https://api.stripe.com/v1/payment_pages/{cs_id}/confirm",
        data={
            "guid": uuid.uuid4().hex,
            "muid": uuid.uuid4().hex,
            "sid": uuid.uuid4().hex,
            "payment_method": pm_id,
            "init_checksum": str(init_payload.get("init_checksum") or ctx.get("init_checksum") or ""),
            "version": runtime_version,
            "expected_amount": str(ctx.get("checkout_amount") or opll_expected_amount(init_payload)),
            "expected_payment_method_type": "paypal",
            "return_url": return_url,
            "elements_session_client[session_id]": ctx["elements_session_id"],
            "elements_session_client[locale]": str(ctx.get("locale") or "en"),
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[is_aggregation_expected]": "false",
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[stripe_js_id]": ctx["stripe_js_id"],
            "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
            "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
            "elements_options_client[saved_payment_method][enable_save]": "never",
            "elements_options_client[saved_payment_method][enable_redisplay]": "never",
            "client_attribution_metadata[client_session_id]": ctx["stripe_js_id"],
            "client_attribution_metadata[checkout_session_id]": cs_id,
            "client_attribution_metadata[checkout_config_id]": ctx.get("config_id") or "",
            "client_attribution_metadata[elements_session_id]": ctx["elements_session_id"],
            "client_attribution_metadata[elements_session_config_id]": ctx["elements_session_config_id"],
            "client_attribution_metadata[merchant_integration_source]": "checkout",
            "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
            "client_attribution_metadata[merchant_integration_version]": "custom",
            "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
            "client_attribution_metadata[payment_method_selection_flow]": "automatic",
            "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
            "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
            "consent[terms_of_service]": "accepted",
            "key": stripe_pk,
            "_stripe_version": STRIPE_VERSION_FULL,
        },
        timeout=PAY_LONG_LINK_TIMEOUT,
    )
    if response.status_code >= 400:
        raise RuntimeError(opll_stripe_error_summary("stripe confirm failed", response))
    return response.json() or {}


def opll_redirect_url_after_confirm(access_token: str, stripe: requests.Session, confirm_payload: dict,
                                     cs_id: str, stripe_pk: str, ctx: dict, checkout: dict,
                                     proxy_url: str = "") -> str:
    redirect_url = opll_extract_redirect_to_url(confirm_payload)
    if redirect_url:
        return redirect_url
    submission = opll_find_submission_attempt(confirm_payload)
    if submission.get("state") == "requires_approval":
        opll_chatgpt_approve_with_retry(access_token, cs_id, checkout, proxy_url)
        return opll_stripe_payment_page_redirect_url(stripe, cs_id, stripe_pk, ctx=ctx, timeout_seconds=45)
    if submission.get("state") == "failed":
        raise RuntimeError(f"stripe submission failed: {opll_stripe_payload_diagnostics(confirm_payload, ctx)}")
    try:
        return opll_stripe_payment_page_redirect_url(stripe, cs_id, stripe_pk, ctx=ctx, timeout_seconds=30)
    except OpllStripeRequiresApproval:
        opll_chatgpt_approve_with_retry(access_token, cs_id, checkout, proxy_url)
        return opll_stripe_payment_page_redirect_url(stripe, cs_id, stripe_pk, ctx=ctx, timeout_seconds=45)


def opll_combo_attempt_order(country: str) -> list[tuple[str, str]]:
    requested = normalize_opll_country(country)
    ordered = [(requested, requested)]
    if requested == "DE":
        ordered.extend([("US", "US"), ("DE", "US"), ("US", "DE")])
    result = []
    seen = set()
    for item in ordered:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


# ===================================================================
# Top-level payment link generation
# ===================================================================

def generate_opll_paypal_long_link(access_token: str, country: str, currency: str,
                                    proxy_url: str = "") -> dict:
    """
    Generate a PayPal BA approve long link from a ChatGPT access token.
    This is used for modes like "PayPal 长链接 US/USD", "PayPal 长链接 FR/EUR",
    and "PayPal 长链接 JP/JPY".
    """
    failures: list[str] = []
    requested_country = normalize_opll_country(country)
    for checkout_country, pm_country in opll_combo_attempt_order(requested_country):
        try:
            checkout = opll_create_checkout(access_token, checkout_country,
                                             currency_for_country(checkout_country), proxy_url)
            stripe = opll_build_stripe_session(proxy_url)
            init_payload = opll_stripe_init(checkout["cs_id"], checkout["billing_country"],
                                             checkout["currency"], proxy_url,
                                             stripe=stripe, checkout=checkout)
            stripe_hosted_url = str(init_payload.get("stripe_hosted_url") or "").strip()
            if not stripe_hosted_url:
                raise RuntimeError(f"stripe init response missing stripe_hosted_url, "
                                   f"keys={sorted(init_payload.keys())}")
            hosted_long_url = opll_to_openai_pay_url(stripe_hosted_url)
            stripe_pk = opll_stripe_key_for_checkout(checkout)
            ctx = opll_stripe_context(init_payload)
            if not ctx.get("currency"):
                ctx["currency"] = str(checkout.get("currency") or "").lower()
            stripe_amount, stripe_amount_source = opll_require_zero_amount(init_payload, checkout["currency"])
            pm_id = opll_stripe_create_paypal_method(stripe, checkout["cs_id"], ctx,
                                                      opll_billing_for_country(pm_country), stripe_pk)
            confirm_payload = opll_stripe_confirm(stripe, checkout["cs_id"], pm_id, stripe_pk,
                                                   init_payload, ctx, checkout, stripe_hosted_url)
            stripe_redirect_url = opll_redirect_url_after_confirm(
                access_token, stripe, confirm_payload, checkout["cs_id"], stripe_pk,
                ctx, checkout, proxy_url)
            provider_url = stripe_redirect_url if opll_is_paypal_ba_approve_url(stripe_redirect_url) \
                else opll_resolve_external_redirect(stripe, stripe_redirect_url)
            if not opll_is_paypal_ba_approve_url(provider_url):
                resource_hint = "仅发现 Stripe 资源 URL，未发现 PayPal BA approve 链；" \
                    if opll_is_ignored_resource_url(provider_url) else ""
                raise RuntimeError(
                    f"{resource_hint}未提取到最终 PayPal BA approve 链；成功标准必须为 "
                    f"https://www.paypal.com/agreements/approve?ba_token=...；"
                    f"当前结果: {provider_url or stripe_redirect_url}"
                )
            return {
                **checkout,
                "payment_method_country": pm_country,
                "payment_method_id": pm_id,
                "stripe_hosted_url": stripe_hosted_url,
                "stripe_redirect_url": stripe_redirect_url,
                "provider_redirect_url": provider_url,
                "fallback": (checkout_country, pm_country) != (requested_country, requested_country),
                "provider_error": "; ".join(failures),
                "long_url": provider_url or hosted_long_url,
                "stripe_amount": stripe_amount,
                "stripe_amount_source": stripe_amount_source,
            }
        except Exception as exc:
            failures.append(f"{checkout_country}+{pm_country}: {opll_short_error(str(exc))}")
    raise RuntimeError(f"所有组合均未提取到 PayPal BA approve 链；{'; '.join(failures)}")


def generate_opll_hosted_long_link(access_token: str, country: str, currency: str,
                                    proxy_url: str = "") -> dict:
    """
    Generate a hosted Stripe checkout URL (no card / GoPay / Apple Pay).
    Used for modes like "无卡长链接 US/USD", "GoPay 长链接 ID/IDR", etc.
    """
    checkout = opll_create_checkout(access_token, country, currency, proxy_url)
    init_payload = opll_stripe_init(checkout["cs_id"], checkout["billing_country"],
                                     checkout["currency"], proxy_url, checkout=checkout)
    stripe_hosted_url = str(init_payload.get("stripe_hosted_url") or "").strip()
    if not stripe_hosted_url:
        raise RuntimeError(f"stripe init response missing stripe_hosted_url, "
                           f"keys={sorted(init_payload.keys())}")
    stripe_amount, stripe_amount_source = opll_require_zero_amount(init_payload, checkout["currency"])
    long_url = opll_to_openai_pay_url(stripe_hosted_url) or opll_stripe_checkout_long_url(
        checkout["cs_id"], checkout["billing_country"], checkout.get("processor_entity", ""))
    return {
        **checkout,
        "stripe_hosted_url": stripe_hosted_url,
        "long_url": long_url,
        "stripe_amount": stripe_amount,
        "stripe_amount_source": stripe_amount_source,
    }


# ===================================================================
# Unified entry point
# ===================================================================

def generate_payment_link(access_token: str, mode: str = "无卡长链接 US/USD",
                           proxy_url: str = "") -> dict:
    """
    Generate a ChatGPT Plus payment link from an access token (Session).

    Args:
        access_token: ChatGPT session access token (raw JSON or bare token).
        mode: Payment mode name, one of PAYMENT_MODES keys:
              - "无卡长链接 US/USD" (hosted Stripe URL, no card)
              - "无卡长链接 BR/BRL"
              - "无卡长链接 DE/EUR"
              - "无卡长链接 FR/EUR"
              - "无卡长链接 GB/GBP"
              - "无卡长链接 CA/CAD"
              - "无卡长链接 AU/AUD"
              - "无卡长链接 JP/JPY"
              - "GoPay 长链接 ID/IDR"
              - "PayPal 长链接 US/USD" (PayPal BA approve URL)
              - "PayPal 长链接 FR/EUR"
              - "PayPal 长链接 JP/JPY"
              - "Apple Pay 支付页 US/USD"
              - "Apple Pay 支付页 JP/JPY"
        proxy_url: Optional HTTP proxy URL, e.g. "http://127.0.0.1:7890".

    Returns:
        dict with keys: long_url, cs_id, billing_country, currency,
                        stripe_hosted_url, processor_entity, etc.
    """
    mode_config = PAYMENT_MODES.get(mode)
    if not mode_config:
        raise ValueError(f"Unknown payment mode: {mode}. Available: {list(PAYMENT_MODES.keys())}")

    token = parse_session_json(access_token) or str(access_token or "").strip()
    if not token:
        raise RuntimeError("无法从输入内容中解析 Access Token")

    country = str(mode_config.get("country") or "US")
    currency = str(mode_config.get("currency") or currency_for_country(country))
    is_paypal = mode.startswith("PayPal 长链接")
    apple_pay_hosted = bool(mode_config.get("apple_pay_hosted"))

    if is_paypal:
        result = generate_opll_paypal_long_link(token, country, currency, proxy_url)
    else:
        # 无卡长链接 / GoPay / Apple Pay — all use hosted
        result = generate_opll_hosted_long_link(token, country, currency, proxy_url)

    return {
        "success": True,
        "mode": mode,
        "long_url": str(result.get("long_url") or ""),
        "cs_id": str(result.get("cs_id") or ""),
        "billing_country": str(result.get("billing_country") or country),
        "currency": str(result.get("currency") or currency),
        "stripe_hosted_url": str(result.get("stripe_hosted_url") or ""),
        "processor_entity": str(result.get("processor_entity") or ""),
        "payment_method_country": str(result.get("payment_method_country") or ""),
        "stripe_amount": str(result.get("stripe_amount") or ""),
        "stripe_amount_source": str(result.get("stripe_amount_source") or ""),
        "fallback": bool(result.get("fallback")),
        "raw_result": result,
    }


# ===================================================================
# Proxy chain server (for chaining local + dynamic proxy)
# ===================================================================

class ProxyChainServer:
    """Chain local proxy -> dynamic proxy -> target, with runtime switching."""

    def __init__(self, local_proxy: str, dynamic_proxy: str,
                 log_callback=None):
        self.local_proxy = normalize_proxy_url(local_proxy)
        self.dynamic_proxy = normalize_proxy_url(dynamic_proxy)
        self.log = log_callback or (lambda msg: None)
        self.lock = threading.Lock()
        self.active_sockets: set[socket.socket] = set()
        self.server: socket.socket | None = None
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.url = ""

    def __enter__(self):
        if not self.local_proxy and not self.dynamic_proxy:
            return self
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(64)
        port = self.server.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.close()

    def close(self) -> None:
        self.stop_event.set()
        if self.server:
            try:
                self.server.close()
            except Exception:
                pass
        self.server = None

    def set_dynamic_proxy(self, dynamic_proxy: str) -> None:
        sockets: list[socket.socket]
        with self.lock:
            self.dynamic_proxy = normalize_proxy_url(dynamic_proxy)
            sockets = list(self.active_sockets)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass

    def _track_socket(self, sock: socket.socket) -> None:
        with self.lock:
            self.active_sockets.add(sock)

    def _untrack_socket(self, sock: socket.socket) -> None:
        with self.lock:
            self.active_sockets.discard(sock)

    def _serve(self) -> None:
        assert self.server is not None
        while not self.stop_event.is_set():
            try:
                client, _addr = self.server.accept()
            except OSError:
                break
            threading.Thread(target=self._handle_client, args=(client,), daemon=True).start()

    def _handle_client(self, client: socket.socket) -> None:
        upstream = None
        self._track_socket(client)
        try:
            client.settimeout(30)
            head = self._read_http_head(client)
            if not head:
                return
            first_line = head.split(b"\r\n", 1)[0].decode("latin1", errors="replace")
            parts = first_line.split()
            if len(parts) < 3:
                return
            method, target, version = parts[0].upper(), parts[1], parts[2]
            if method == "CONNECT":
                upstream = self._open_chain_to_target(target)
                self._track_socket(upstream)
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self._relay(client, upstream)
                return
            rewritten = self._rewrite_plain_request(head, method, target, version)
            upstream = self._open_chain_to_target(self._target_from_plain_request(method, target, head))
            self._track_socket(upstream)
            upstream.sendall(rewritten)
            self._relay(client, upstream)
        except Exception:
            try:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            except Exception:
                pass
        finally:
            self._untrack_socket(client)
            if upstream:
                self._untrack_socket(upstream)
            try:
                client.close()
            except Exception:
                pass

    def _read_http_head(self, client: socket.socket) -> bytes:
        data = b""
        while b"\r\n\r\n" not in data and len(data) < 65536:
            chunk = client.recv(4096)
            if not chunk:
                break
            data += chunk
        return data

    def _target_from_plain_request(self, method: str, target: str, head: bytes) -> str:
        if target.startswith("http://") or target.startswith("https://"):
            parsed = urlparse(target)
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            return f"{parsed.hostname}:{port}"
        host = ""
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"host:"):
                host = line.split(b":", 1)[1].strip().decode("latin1")
                break
        return host

    def _rewrite_plain_request(self, head: bytes, method: str, target: str, version: str) -> bytes:
        if not (target.startswith("http://") or target.startswith("https://")):
            return head
        parsed = urlparse(target)
        path = parsed.path or "/"
        if parsed.query:
            path += f"?{parsed.query}"
        lines = head.split(b"\r\n")
        lines[0] = f"{method} {path} {version}".encode("latin1")
        return b"\r\n".join(lines)

    def _open_chain_to_target(self, target: str) -> socket.socket:
        with self.lock:
            local_proxy = self.local_proxy
            dynamic_proxy = self.dynamic_proxy
        if local_proxy:
            sock = self._connect_proxy(local_proxy)
            self._send_connect(sock, self._proxy_connect_target(dynamic_proxy) if dynamic_proxy else target)
            if dynamic_proxy:
                self._send_connect(sock, target, proxy_url=dynamic_proxy)
            return sock
        if dynamic_proxy:
            sock = self._connect_proxy(dynamic_proxy)
            self._send_connect(sock, target, proxy_url=dynamic_proxy)
            return sock
        host, port = self._split_host_port(target, 80)
        return socket.create_connection((host, port), timeout=30)

    def _connect_proxy(self, proxy_url: str) -> socket.socket:
        parsed = urlparse(proxy_url)
        if parsed.scheme not in ("http", "https"):
            raise RuntimeError(f"链式代理当前只支持 http/https 代理: {proxy_url}")
        host = parsed.hostname
        if not host:
            raise RuntimeError(f"代理地址缺少 host: {proxy_url}")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        raw = socket.create_connection((host, port), timeout=30)
        if parsed.scheme == "https":
            return ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        return raw

    def _proxy_connect_target(self, proxy_url: str) -> str:
        parsed = urlparse(proxy_url)
        if not parsed.hostname:
            raise RuntimeError(f"动态代理地址缺少 host: {proxy_url}")
        return f"{parsed.hostname}:{parsed.port or (443 if parsed.scheme == 'https' else 80)}"

    def _send_connect(self, sock: socket.socket, target: str, proxy_url: str = "") -> None:
        headers = [f"CONNECT {target} HTTP/1.1", f"Host: {target}", "Proxy-Connection: keep-alive"]
        auth = self._proxy_auth(proxy_url)
        if auth:
            headers.append(f"Proxy-Authorization: Basic {auth}")
        request = ("\r\n".join(headers) + "\r\n\r\n").encode("latin1")
        sock.sendall(request)
        response = self._read_http_head(sock)
        status = response.split(b"\r\n", 1)[0].decode("latin1", errors="replace")
        if " 200 " not in f" {status} ":
            raise RuntimeError(f"代理 CONNECT 失败: {status}")

    def _proxy_auth(self, proxy_url: str) -> str:
        parsed = urlparse(proxy_url)
        if not parsed.username:
            return ""
        username = unquote(parsed.username)
        password = unquote(parsed.password or "")
        return base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")

    def _split_host_port(self, target: str, default_port: int) -> tuple[str, int]:
        if target.startswith("["):
            host, rest = target[1:].split("]", 1)
            port = int(rest[1:]) if rest.startswith(":") else default_port
            return host, port
        if ":" in target:
            host, port = target.rsplit(":", 1)
            return host, int(port)
        return target, default_port

    def _relay(self, left: socket.socket, right: socket.socket) -> None:
        sockets = [left, right]
        for sock in sockets:
            sock.settimeout(None)
        try:
            while True:
                readable, _, _ = select.select(sockets, [], [], 60)
                if not readable:
                    return
                for src in readable:
                    dst = right if src is left else left
                    data = src.recv(65536)
                    if not data:
                        return
                    dst.sendall(data)
        finally:
            try:
                right.close()
            except Exception:
                pass
