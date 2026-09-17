"""Shared country aliases used by the dashboard and country resolver."""

COUNTRY_CODES: dict[str, str] = {
    "afghanistan": "AF", "albania": "AL", "algeria": "DZ", "angola": "AO",
    "argentina": "AR", "armenia": "AM", "australia": "AU", "austria": "AT",
    "azerbaijan": "AZ", "bahrain": "BH", "bangladesh": "BD", "belarus": "BY",
    "belgium": "BE", "bolivia": "BO", "bolivia (plurinational state of)": "BO",
    "bosnia and herzegovina": "BA", "botswana": "BW", "brazil": "BR",
    "bulgaria": "BG", "cambodia": "KH", "cameroon": "CM", "canada": "CA",
    "chile": "CL", "china": "CN", "colombia": "CO", "costa rica": "CR",
    "croatia": "HR", "cuba": "CU", "czech republic": "CZ", "czechia": "CZ",
    "denmark": "DK", "dominican republic": "DO", "ecuador": "EC", "egypt": "EG",
    "el salvador": "SV", "ethiopia": "ET", "finland": "FI", "france": "FR",
    "georgia": "GE", "germany": "DE", "ghana": "GH", "greece": "GR",
    "guatemala": "GT", "honduras": "HN", "hungary": "HU", "india": "IN",
    "indonesia": "ID", "iran": "IR", "iraq": "IQ", "ireland": "IE",
    "israel": "IL", "italy": "IT", "ivory coast": "CI", "cote d'ivoire": "CI",
    "jamaica": "JM", "japan": "JP", "jordan": "JO", "kazakhstan": "KZ",
    "kenya": "KE", "kuwait": "KW", "kyrgyzstan": "KG", "laos": "LA",
    "latvia": "LV", "lebanon": "LB", "libya": "LY", "lithuania": "LT",
    "luxembourg": "LU", "malaysia": "MY", "mali": "ML", "mexico": "MX",
    "moldova": "MD", "morocco": "MA", "mozambique": "MZ", "myanmar": "MM",
    "namibia": "NA", "nepal": "NP", "netherlands": "NL", "new zealand": "NZ",
    "nicaragua": "NI", "nigeria": "NG", "north korea": "KP", "norway": "NO",
    "oman": "OM", "pakistan": "PK", "panama": "PA", "paraguay": "PY",
    "peru": "PE", "philippines": "PH", "poland": "PL", "portugal": "PT",
    "qatar": "QA", "romania": "RO", "russia": "RU", "russian federation": "RU",
    "saudi arabia": "SA", "senegal": "SN", "serbia": "RS", "singapore": "SG",
    "slovakia": "SK", "somalia": "SO", "south africa": "ZA",
    "south korea": "KR", "republic of korea": "KR", "spain": "ES",
    "sri lanka": "LK", "sudan": "SD", "sweden": "SE", "switzerland": "CH",
    "syria": "SY", "syrian arab republic": "SY", "taiwan": "TW",
    "tajikistan": "TJ", "tanzania": "TZ", "united republic of tanzania": "TZ",
    "thailand": "TH", "tunisia": "TN", "turkey": "TR", "turkmenistan": "TM",
    "ukraine": "UA", "united arab emirates": "AE", "united kingdom": "GB",
    "united states": "US", "usa": "US", "uruguay": "UY", "uzbekistan": "UZ",
    "venezuela": "VE", "venezuela (bolivarian republic of)": "VE",
    "vietnam": "VN", "viet nam": "VN", "yemen": "YE", "zambia": "ZM",
    "zimbabwe": "ZW",
}


def country_key(name: str) -> str:
    """Compare known names and codes without guessing unknown countries."""
    value = str(name or "").strip().casefold()
    return COUNTRY_CODES.get(value, value.upper() if value.upper() in COUNTRY_CODES.values() else value)
