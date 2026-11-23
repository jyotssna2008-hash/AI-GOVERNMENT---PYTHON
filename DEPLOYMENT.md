# CivicEye deployment guide

## 1. Run locally

1. Install Python 3.11 or 3.12.
2. In this repository folder run `python -m venv .venv`.
3. Activate it: Windows PowerShell: `.venv\\Scripts\\Activate.ps1`; macOS/Linux: `source .venv/bin/activate`.
4. Run `pip install -r requirements.txt`.
5. Set a secret key. PowerShell: `$env:SECRET_KEY='use-a-long-random-secret'`. macOS/Linux: `export SECRET_KEY='use-a-long-random-secret'`.
6. Run `python app.py` and open `http://127.0.0.1:5000`.

Citizen accounts can be created from the Register page. For the first administrator, set `SEED_DEMO_USERS=true` and `DEMO_PASSWORD` to a strong password before the first run. This creates `admin@civiceye.local` and `official@civiceye.local`; sign in and change the demo password strategy before public use. Do not enable demo seeding on a public deployment.

## 2. Deploy to Vercel

1. Push this repository to GitHub (this repository is already configured for Vercel's `api/index.py` Python entry point).
2. In Vercel choose **Add New → Project**, import `jyotssna2008-hash/AI-GOVERNMENT---PYTHON`, and deploy.
3. Add environment variables in Vercel Project Settings → Environment Variables:
   - `SECRET_KEY`: long random secret
   - `DATABASE_URL`: production MySQL URL, e.g. `mysql+pymysql://USER:PASSWORD@HOST:3306/DATABASE` (URL-encode special characters in the password)
   - `UPLOAD_FOLDER`: `/tmp/civiceye_uploads` (temporary only)
4. Redeploy after setting variables.

Important: Vercel serverless storage is temporary. Do not use the default SQLite database or local uploads for real production data on Vercel. Connect a managed MySQL database and a persistent object-storage service for uploaded photos before accepting real public complaints. Configure backups, HTTPS, monitoring and a production admin account. Never commit `.env` or database credentials.

## 3. Feature notes and limitations

- NLP category/urgency/severity output is a transparent keyword baseline, not a trained government-domain model. Train and validate a labelled dataset before operational use.
- Duplicate suggestions use TF-IDF + cosine similarity when scikit-learn is available; an official must review them.
- OpenCV checks basic image resolution, blur and brightness. It does not prove authenticity or location and does not currently provide a trained visual category classifier.
- DBSCAN displays observed clusters only when enough GPS-tagged complaints exist. Forecasts are intentionally withheld without adequate historical time-series data.
- SSIM compares before/after images when OpenCV and scikit-image are installed. It is only a visual signal; administrator approval is mandatory.
- SMS/email integration, Tamil/Hindi NLP, PDF reporting, formal 12-table migration and a trained image classifier are future integrations; do not represent these as implemented or validated in this starter deployment.
- The initial app provides CSV export. Review security, privacy, accessibility and local government policies before any real deployment.
