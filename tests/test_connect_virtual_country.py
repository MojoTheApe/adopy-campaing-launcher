"""The connect block of a country that has spend but no linked account yet ("virtual" country, key "")
must still carry the country, so a connect from the builder links the new account to it.

Before the fix the Manage block rendered data-amap-country="" for such a country; the builder's "+ Account"
borrows that block, so the connect saved the account without linking it to the country and the builder
lost it after the reload (buyer report 17.11, Saudi Arabia).
"""
from pathlib import Path
import re

from jinja2 import Environment, FileSystemLoader, Undefined

TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"


def _render(country: dict) -> str:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True, undefined=Undefined)
    for name in ("fmt_usd", "fmt_int", "fmt_pct", "fmt_num"):
        env.filters[name] = lambda v, *a, **k: str(v)
    env.filters["fmt_currency"] = lambda v, *a, **k: str(v)
    env.filters["fmt_summary_amount"] = lambda v, *a, **k: str(v)
    amap = {"countries": [country], "available": [], "all_accounts": [], "live": {}, "dates": [],
            "date_from": "", "date_to": "", "span": ""}
    return env.get_template("_account_map_country.html").render(
        mc=country, amap=amap, mlx={"A": "Inhouse", "B": "Agency"}, compare=None)


def _connect_countries(html: str) -> list[str]:
    return re.findall(r'class="[^"]*amap-add[^"]*"[^>]*data-amap-country="([^"]*)"', html) or \
        re.findall(r'data-amap-country="([^"]*)"', html)


def test_virtual_country_connect_block_carries_the_country_name():
    country = {"country": "Saudi Arabia", "key": "", "rows": [], "virtual": True,
               "spend": 120, "deposits": 0, "leads": 3}
    values = _connect_countries(_render(country))
    assert values, "the country has a connect block"
    assert all(v == "Saudi Arabia" for v in values), values


def test_linked_country_connect_block_keeps_its_key():
    country = {"country": "Saudi Arabia", "key": "SA", "rows": [], "virtual": False,
               "spend": 0, "deposits": 0, "leads": 0}
    values = _connect_countries(_render(country))
    assert values and all(v == "SA" for v in values), values
