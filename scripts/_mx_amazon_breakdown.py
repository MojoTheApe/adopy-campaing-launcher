import sys, pandas as pd, re
sys.stdout.reconfigure(encoding='utf-8')

df = pd.read_excel(r'C:\Users\EvgeniSedin\Desktop\cursor example.xlsx')
df = df.rename(columns={'# FTDs': 'Deposits', '# Leads': 'Leads'})
df['Country']  = df['Country'].fillna('').astype(str)
df['Ad Asset'] = df['Ad Asset'].fillna('').astype(str)
df['Ad Title'] = df['Ad Title'].fillna('-').astype(str)
df['Adset Name'] = df['Adset Name'].fillna('').astype(str)
for col in ['Deposits', 'Leads', '$ Spend', '$ Net']:
    df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

DEVICE_KW = {'all', 'ios', 'android', 'desktop', 'mobile', 'vid', 'video', 'image', 'img', 'vids', 'videos', 'images'}
CREATIVE_PREFIXES = ('vid', 'image', 'img', 'ccblk', 'ccblack', '134', '700', '1377', '1319', '1538', '2343', 'blue')

def parse_interest(adset: str) -> str:
    parts = [p.strip() for p in adset.split('_') if p.strip()]
    device_idx = -1
    for i, p in enumerate(parts):
        if p.lower() in DEVICE_KW:
            device_idx = i
            break
    if device_idx == -1:
        device_idx = 1
    remaining = parts[device_idx + 1:]
    interest_parts = []
    for p in remaining:
        if re.match(r'^\d{1,2}\+?$', p):  # age
            break
        if re.match(r'^\d{2}\.\d{1,2}$', p):  # date
            break
        low = p.lower()
        is_creative = any(low.startswith(pfx) for pfx in CREATIVE_PREFIXES) or re.match(r'^\d{3,}', p)
        if not is_creative:
            interest_parts.append(p)
    return '/'.join(interest_parts) if interest_parts else '(wide)'

# Mexico + Amazon
mx = df[
    df['Country'].str.upper().str.contains('MEX') &
    (df['Ad Asset'].str.lower() == 'amazon')
].copy()

print(f"Total MX Amazon rows: {len(mx)}, total FTDs: {mx['Deposits'].sum():.0f}, Net: ${mx['$ Net'].sum():,.0f}\n")

mx['Targeting'] = mx['Adset Name'].apply(parse_interest)
mx['Creative']  = mx['Ad Title']

def show(key, label):
    g = mx.groupby(key).agg(
        Deps  =('Deposits', 'sum'),
        Leads =('Leads',    'sum'),
        Spend =('$ Spend',  'sum'),
        Net   =('$ Net',    'sum'),
    ).reset_index()
    g['CPL'] = (g['Spend'] / g['Leads'].replace(0, float('nan'))).round(2)
    g['ROI'] = (g['Net']   / g['Spend'].replace(0, float('nan'))).round(2)
    g = g[g['Deps'] > 0].sort_values('Deps', ascending=False).head(10)
    print(f"=== {label} ===")
    for _, r in g.iterrows():
        roi_s = f"{r['ROI']:.2f}" if pd.notna(r['ROI']) else "—"
        cpl_s = f"${r['CPL']:.2f}" if pd.notna(r['CPL']) else "—"
        print(f"  [{r[key]}]")
        print(f"    FTDs={r['Deps']:.0f}  Leads={r['Leads']:.0f}  CPL={cpl_s}  Net=${r['Net']:,.0f}  ROI={roi_s}")
    print()

show('Targeting', 'TOP ADSETS (таргетинг) — Mexico Amazon')
show('Creative',  'TOP CREATIVES (объявления) — Mexico Amazon')

# Top combos
print("=== TOP COMBOS — Mexico Amazon ===")
combo = mx.groupby(['Targeting', 'Creative']).agg(
    Deps  =('Deposits', 'sum'),
    Leads =('Leads',    'sum'),
    Spend =('$ Spend',  'sum'),
    Net   =('$ Net',    'sum'),
).reset_index()
combo['CPL'] = (combo['Spend'] / combo['Leads'].replace(0, float('nan'))).round(2)
combo['ROI'] = (combo['Net']   / combo['Spend'].replace(0, float('nan'))).round(2)
combo = combo[combo['Deps'] > 0].sort_values('Deps', ascending=False).head(10)
for _, r in combo.iterrows():
    roi_s = f"{r['ROI']:.2f}" if pd.notna(r['ROI']) else "—"
    cpl_s = f"${r['CPL']:.2f}" if pd.notna(r['CPL']) else "—"
    print(f"  Таргет: {r['Targeting']}  +  Объявление: {r['Creative']}")
    print(f"    FTDs={r['Deps']:.0f}  Leads={r['Leads']:.0f}  CPL={cpl_s}  Net=${r['Net']:,.0f}  ROI={roi_s}")
