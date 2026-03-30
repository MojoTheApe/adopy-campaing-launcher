import sys, pandas as pd
sys.stdout.reconfigure(encoding='utf-8')

path = r'C:\Users\EvgeniSedin\Desktop\Upluad FB.xlsx'
df = pd.read_excel(path, sheet_name="LP's")

# Count unique offers and total URLs
df['Stock_clean'] = df['Stock'].fillna('').astype(str).str.strip()
df['lp_col'] = df["Lp's"].fillna('').astype(str).str.strip()

offers = df[df['Stock_clean'] != '']['Stock_clean'].unique()
print(f"Unique offers (Stock): {len(offers)}")
print()
for offer in sorted(offers):
    rows = df[df['Stock_clean'] == offer]
    urls = []
    for lp_raw in rows['lp_col']:
        for u in lp_raw.split('\n'):
            u = u.strip()
            if u and u.lower().startswith('http'):
                urls.append(u)
    print(f"  {offer}: {len(rows)} rows, {len(urls)} URLs")
