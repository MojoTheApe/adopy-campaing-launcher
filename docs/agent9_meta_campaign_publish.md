# Agent 9: публикация кампаний через Meta Marketing API

Excel из **агента 8** → Graph API (Campaign → Ad Set → Ad Creative → Ad).

## Настройка

1. Скопируйте `config/ad_account_map.example.csv` → `config/ad_account_map.csv`
2. Заполните `.env`:
   - `META_ACCESS_TOKEN` — System User с `ads_management`
   - `FB_CREATIVES_ROOT=C:\Users\EvgeniSedin\Desktop\Creatives`
   - `META_PAGE_ID` — ID Facebook-страницы (или `page_id` в CSV / колонка **Link Object ID** в Excel)

## Dry-run (без вызовов API)

```powershell
cd fb_agents
python run_publish_campaigns.py -i output/upload_plan.xlsx --dry-run `
  -e "C:\Users\EvgeniSedin\Desktop\Upluad FB.xlsx" `
  --map config/ad_account_map.csv
```

## Live publish через Multilogin (разный IP на аккаунт)

Для каждого аккаунта **последовательно**: start профиля → Meta API **через прокси профиля** → stop профиля.

Заполните в `config/ad_account_map.csv` колонки:
`multilogin_instance`, `folder_id`, `profile_id`, `proxy_type`, `proxy_host`, `proxy_port`, `proxy_user`, `proxy_pass`

В `.env`:
```
MULTILOGIN_AUTOMATION_TOKEN_A=...
MULTILOGIN_LAUNCHER_BASE_A=https://launcher.mlx.yt:45001/api/v2
```

Multilogin X должен быть **запущен** на этой машине.

```powershell
python run_publish_campaigns.py -i output/upload_plan.xlsx --confirm --via-multilogin --limit 1 `
  -e "C:\Users\EvgeniSedin\Desktop\Upluad FB.xlsx" `
  --map config/ad_account_map.csv
```

Прокси берётся из CSV (те же host/port, что настроены в профиле Multilogin).

## Live publish (реальные объекты в Meta)

По умолчанию статус **PAUSED**. Обязателен флаг `--confirm`.

```powershell
python run_publish_campaigns.py -i output/upload_plan.xlsx --confirm --limit 1 `
  -e "C:\Users\EvgeniSedin\Desktop\Upluad FB.xlsx" `
  --map config/ad_account_map.csv
```

Включить сразу активными: добавьте `--activate`.

Лог созданных ID: `output/publish_log.jsonl`  
Кэш загруженных креативов: `output/meta_creative_cache.json`

## Полный цикл

```powershell
python run_upload_from_plan.py output/plan.csv -o output/upload_plan.xlsx
python run_publish_campaigns.py -i output/upload_plan.xlsx --dry-run -e "C:\Users\EvgeniSedin\Desktop\Upluad FB.xlsx"
python run_publish_campaigns.py -i output/upload_plan.xlsx --confirm --limit 1 -e "C:\Users\EvgeniSedin\Desktop\Upluad FB.xlsx"
```

Спека: `docs/superpowers/specs/2026-05-28-meta-campaign-agent-design.md`
