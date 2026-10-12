# Putting the employee app on the Google Play Store

The Play Store app is a **Trusted Web Activity**: an Android app that opens `https://attendance.madarwala.cloud/m/`
full screen, with camera and GPS working. Changes to the website show up in the app straight away; a new Play Store
upload is only needed to change the app's name or icon.

Everything for the listing is in the [`play-store/`](play-store) folder: the icon, the feature graphic and the phone
screenshots.

---

## 1. Get the server ready (5 minutes)

1. Update the server (Hostinger Web console):
   ```
   cd /opt/attendance-system && git pull && docker compose -f docker-compose.nginx.yml up -d --build
   ```
2. Add a contact for the privacy policy to `/opt/attendance-system/.env` (use `nano .env`), for example:
   ```
   PRIVACY_CONTACT=hr@madarwala.com
   ```
   then `docker compose -f docker-compose.nginx.yml up -d`. Check https://attendance.madarwala.cloud/privacy opens.
3. **A login for Google's reviewers.** Google tests the app before publishing it and needs a way in. In the dashboard
   go to **Employees > Add employee**: ID `DEMO`, name `Play Review`, tick **Can check in from anywhere** (reviewers
   are not at your office), no salary. Then **Set PIN** (for example `246810`). Untick Active after the app is approved
   if you like, or keep it for future updates.

## 2. Build the app on GitHub (automatic)

The Android project is in [`android/`](android) and GitHub builds it with
[`.github/workflows/android.yml`](.github/workflows/android.yml) every time it changes (or on demand).

**One-time setup: the signing key.** Every version of the app must be signed with the same *upload key*. You have it
as three files (`upload.jks`, `ANDROID_KEYSTORE_PASSWORD.txt`, `ANDROID_KEYSTORE_B64.txt`). Keep them safe in two
places; they are never stored in the repository. Add two secrets on GitHub: **repository > Settings > Secrets and
variables > Actions > New repository secret**:

| Name | Value |
|---|---|
| `ANDROID_KEYSTORE_B64` | the whole content of `ANDROID_KEYSTORE_B64.txt` |
| `ANDROID_KEYSTORE_PASSWORD` | the content of `ANDROID_KEYSTORE_PASSWORD.txt` |

**Building.** Open the repository's **Actions** tab > **Android app** > **Run workflow** (or just wait for the run that
starts after a change). When it is green, open the run and download the file under **Artifacts**. It contains:

- `madarwala-attendance-N.aab`: **upload this to Play Console**
- `app-release.apk`: install it directly on your phone to try the app first

Each build gets a higher version code automatically, so later uploads are accepted as updates.

*(Alternative without GitHub: [PWABuilder](https://www.pwabuilder.com) can make the same app from
`https://attendance.madarwala.cloud/m/`: package ID `com.madarwala.attendance`, location and notification delegation
on. If you use it, use the same signing key: "Use mine" with `upload.jks`.)*

## 3. Create the app in Play Console

Open **https://play.google.com/console > Create app**: name `Madarwala Attendance`, language English (India), App,
Free. Accept the declarations.

### Store listing (Grow > Store presence > Main store listing)

- **App name:** Madarwala Attendance
- **Short description** (78 characters):
  > Check in with GPS and a selfie, apply for leave, work from home, get payslips.
- **Full description:**
  > The official attendance app for employees of Madarwala Tours and Travels.
  >
  > • Check in and check out from your phone with GPS and a selfie, at the office or on an approved work-from-home day
  > • See today's check-ins at a glance
  > • Apply for Casual, Adhoc / Sick, Festive and half-day leave, with your balance and the exact effect on your salary shown before you apply
  > • Request work from home and see when your team lead approves it
  > • Download your monthly payslips as PDF
  >
  > The app is for Madarwala Tours and Travels staff only. Your employee ID and PIN are given to you by HR.
  > Location and camera are used only at the moment you check in. Nothing is tracked in the background.
- **App icon:** `play-store/icon-512.png`
- **Feature graphic:** `play-store/feature-graphic.png`
- **Phone screenshots:** all five files in `play-store/screenshots/`
- **Category:** Business. **Contact email:** your HR or IT email.

### App content (Policy > App content)

| Section | Answer |
|---|---|
| Privacy policy | `https://attendance.madarwala.cloud/privacy` |
| App access | *All or some functionality is restricted*. Add the reviewer login: Employee ID `DEMO`, PIN (the one you set). Instructions: "Sign in, allow camera and location, tap Check in." |
| Ads | No ads |
| Content rating | Fill in the questionnaire: category *Utility, productivity, communication or other*; answer No to everything. Result: Everyone |
| Target audience | 18 and over |
| News app / Government app | No |
| Financial features | My app doesn't provide any financial features |
| Health | No |
| Data safety | See the table below |

**Data safety answers.** Collected: Yes. Shared with third parties: **No**. Encrypted in transit: **Yes**.
Users can ask for their data to be deleted: Yes (through HR).

| Data type | Collected | Purpose | Optional? |
|---|---|---|---|
| Location > Precise location | Yes (only when checking in) | App functionality, Fraud prevention / security | Required |
| Photos and videos > Photos | Yes (selfie at check-in) | App functionality, Fraud prevention / security | Required |
| Personal info > Name | Yes | App functionality, Account management | Required |
| Personal info > User IDs | Yes (employee ID) | App functionality, Account management | Required |
| App activity > Other user-generated content | Yes (leave and WFH reasons) | App functionality | Optional |

## 4. Release to your staff

1. **Test and release > Testing > Internal testing > Create new release.** Upload `app-release-bundle.aab`, add
   release notes ("First version"), save and roll out.
2. **Testers:** create an email list with your employees' Gmail addresses (up to 100), then share the
   **join link** with them. They open it on their phone, accept, and install from the Play Store. Internal testing is
   available within minutes and does not need Google's full review, so this is the quickest way to get the app to staff.
3. **Public listing (Production):** if your Play developer account is a *personal* account created after November 2023,
   Google first requires a **closed test with at least 12 testers for 14 days**. Organisation accounts do not have this
   rule. For a company-only app, internal testing can simply stay as the way you distribute it.

## 5. Remove the browser bar (link the app to the website)

Until this is done the app works but shows a small address bar at the top.

1. In Play Console open **Test and release > App integrity > App signing**. Copy the **SHA-256 certificate fingerprint**
   of the *App signing key*, and also the one of the *Upload key* (it is also shown on every GitHub build's summary page).
2. Add both to `.env` on the server, separated by a comma:
   ```
   ANDROID_CERT_SHA256=AB:CD:...:12,98:76:...:EF
   ```
3. `docker compose -f docker-compose.nginx.yml up -d`, then check that
   https://attendance.madarwala.cloud/.well-known/assetlinks.json shows your fingerprints. Close and reopen the app.

## Updating the app later

- **Features and fixes:** nothing to do in the Play Store. Update the website and the app shows the changes.
- **New icon or name:** change it in `android/`, let GitHub build it (the version code goes up by itself) and upload the
  new `.aab` as a new release.
