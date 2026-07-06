# Multilogin: How to Find `folder_id` and `profile_id`

These two IDs are required in `config/ad_account_map.csv` to connect a Facebook account
to its Multilogin browser profile. Without them the RPA publisher cannot launch the correct
browser session.

---

## What they are

| Field | Description |
|-------|-------------|
| `folder_id` | UUID of the **folder** inside Multilogin that contains the profile |
| `profile_id` | UUID of the individual **browser profile** |

Both are UUID strings like `550e8400-e29b-41d4-a716-446655440000`.

---

## Method 1 — Local API (Recommended, works in the desktop app)

When Multilogin is running it exposes a local HTTP server on port **35000**.
Open the URL directly in any browser — no authentication required.

### Step 1: get all folders

Paste this into your browser address bar:

```
http://localhost:35000/profile/folder
```

Response (JSON):
```json
{
  "data": [
    { "folderID": "550e8400-e29b-41d4-a716-446655440000", "name": "Facebook MX" },
    { "folderID": "6ba7b810-9dad-11d1-80b4-00c04fd430c8", "name": "Facebook BR" }
  ]
}
```

Copy the `folderID` of the correct folder → that is your **`folder_id`**.

### Step 2: get profiles inside that folder

```
http://localhost:35000/profile?folderID=<paste_folder_id_here>
```

Response:
```json
{
  "data": [
    { "profileID": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d", "name": "FB_MX_574" },
    { "profileID": "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed", "name": "FB_MX_575" }
  ]
}
```

Copy the `profileID` of the correct profile → that is your **`profile_id`**.

---

## Method 2 — Right-click in the app (may not be available)

1. Open the **Multilogin** desktop application.
2. In the left sidebar, find the folder that contains the Facebook account profile.
3. **Right-click the folder name** → look for **"Copy folder ID"**.
4. If the option is absent, use Method 1 (localhost API).

> In some versions hovering over the folder name shows the UUID as a tooltip.

---

## Method 2 — Multilogin API

If you have Multilogin API access you can retrieve IDs programmatically.

### Get all folders

```
GET https://api.multilogin.com/profile/folder
Authorization: Bearer <your_token>
```

Response example:
```json
{
  "data": [
    { "folderID": "550e8400-...", "name": "Facebook Accounts MX" },
    { "folderID": "6ba7b810-...", "name": "Facebook Accounts BR" }
  ]
}
```

Copy the `folderID` of the correct folder → `folder_id`.

### Get profiles in a folder

```
GET https://api.multilogin.com/profile?folderID=<folder_id>
Authorization: Bearer <your_token>
```

Response example:
```json
{
  "data": [
    { "profileID": "9b1deb4d-...", "name": "FB_MX_Account_574" },
    { "profileID": "1b9d6bcd-...", "name": "FB_MX_Account_575" }
  ]
}
```

Copy the `profileID` of the correct profile → `profile_id`.

---

## Method 3 — From the Multilogin Web UI

1. Open [https://app.multilogin.com](https://app.multilogin.com) in a browser.
2. Navigate to **Profiles** and open the folder.
3. **Click on a profile** to open its details.
4. The URL changes to something like:
   ```
   https://app.multilogin.com/profiles/550e8400-e29b-41d4-a716-446655440000
   ```
   The last UUID segment is the **profile_id**.
5. For **folder_id**: click on the folder itself — the URL becomes:
   ```
   https://app.multilogin.com/folder/6ba7b810-...
   ```

---

## How to fill in the config

Option A — paste directly into the **Campaign Builder** dashboard:

1. Open the morning report dashboard.
2. In the Campaign Builder section for the account that shows **"✕ Missing: folder_id, profile_id"**, fill in the input fields and click **Save config**.
3. The values are saved to `config/ad_account_map.csv` immediately.

Option B — edit `config/ad_account_map.csv` manually:

```csv
account_name,ad_account_id,page_id,multilogin_instance,folder_id,profile_id,...
FacebookUL574 EC Fav,act_1234567890,,A,550e8400-e29b-41d4-a716-446655440000,9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d,...
```

> **`multilogin_instance`** is `A` (default) or `B` if you run two separate Multilogin installations.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| "Account not found in ad_account_map.csv" | Row missing or name mismatch | Add the row; the `account_name` must match the dashboard account label exactly |
| "Multilogin error: profile not found" | Wrong `profile_id` | Double-check using Method 1 or 2 |
| "Multilogin error: folder not found" | Wrong `folder_id` | Verify folder name in the Multilogin app |
| Profile launches but wrong Facebook account | `profile_id` points to a different profile | Confirm which profile has the correct FB cookies |
