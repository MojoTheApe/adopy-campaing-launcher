import sys, json
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8')

kb = json.loads(Path(r'C:\Users\EvgeniSedin\fb_agents\config\knowledge_base.json').read_text(encoding='utf-8'))
offers = kb.get('offers', {})

broken_by_offer = {}
for name, entry in offers.items():
    urls = entry.get('lp_urls') or []
    for u in urls:
        # We don't have status here, so just list all
        pass

# Parse landing_pages.md for broken section
md = Path(r'C:\Users\EvgeniSedin\fb_agents\knowledge_base\landing_pages.md').read_text(encoding='utf-8')

import re
# Find broken section
broken_section = re.search(r'## .*?Broken URLs.*?\n(.*?)(?=\n## [^⚠]|\Z)', md, re.DOTALL)
if broken_section:
    lines = broken_section.group(1).strip().split('\n')
    offers_broken = {}
    for line in lines:
        m = re.match(r'- \*\*(.+?)\*\*: `(.+?)`', line)
        if m:
            offer, url = m.group(1), m.group(2)
            offers_broken.setdefault(offer, []).append(url)
    
    print(f"Всего broken: {sum(len(v) for v in offers_broken.values())} URL в {len(offers_broken)} офферах\n")
    for offer, urls in sorted(offers_broken.items()):
        print(f"  {offer} ({len(urls)} URL):")
        for u in urls:
            print(f"    - {u}")
        print()
