import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv; load_dotenv(Path(__file__).resolve().parents[1] / ".env")
from config.paths import get_upload_fb_excel_path
from src.upload_agent.build_upload_excel import load_reference_data, get_account_info

ref = load_reference_data(get_upload_fb_excel_path())
accounts = ref.get("accounts", {})

print("=== Accounts containing UL869 ===")
found = False
for k in sorted(accounts.keys()):
    if "UL869" in k.upper():
        found = True
        info = accounts[k]
        print(f"  key={k!r}")
        print(f"    link={info.get('link')!r}")
        print(f"    ad_account_id={info.get('ad_account_id')!r}")
        print()

if not found:
    print("  (none found)")

print("\n=== get_account_info('FacebookUL869 CL ML') ===")
result = get_account_info(ref, "FacebookUL869 CL ML")
print(result)

print("\n=== First 10 account keys ===")
for k in list(accounts.keys())[:10]:
    print(f"  {k!r}")
