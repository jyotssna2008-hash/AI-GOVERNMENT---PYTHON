# CivicEye — Government Complaint Tracking & Grievance Analysis

A Flask web application based on the SIMATS capstone **AI-Based Government Complaint Tracking and Unresolved Grievance Analysis System**.

## Included in this starter build
- Citizen registration, complaint submission, GPS coordinates and photo uploads.
- Separate citizen, official and administrator roles with protected routes.
- Complaint assignment, status updates, official notes, notifications and audit events.
- TF-IDF + cosine-similarity duplicate suggestions (when scikit-learn is installed).
- Explainable keyword baseline for category, urgency and severity suggestions.
- OpenCV photo resolution, blur and brightness checks.
- DBSCAN observed-location clusters and Leaflet map for GPS-tagged complaints.
- Before/after SSIM comparison when dependencies and both images are available; admin approval is always required to mark resolved.
- Administrator CSV export.
- Bootstrap responsive interface and Vercel Python entry point.

## Quick start
See [DEPLOYMENT.md](DEPLOYMENT.md) for local setup, initial administrator configuration and Vercel deployment. Install dependencies with `pip install -r requirements.txt`, set `SECRET_KEY`, then run `python app.py`.

## Important limitations
This is an initial working foundation, not a validated government production system. The category/urgency/severity model is a keyword baseline, not a trained classifier; OpenCV performs basic image-quality checks, not trained image-category recognition; hotspot forecasting, SMS/email, Tamil/Hindi NLP, PDF reports and the full 12-table normalized migration are not yet implemented. AI outputs are advisory and never prove image authenticity or physical location. Human review and administrator approval remain mandatory.

Before public use, connect a managed MySQL database and persistent private object storage, configure secrets, backups and monitoring, test authorization and uploads, and train/validate models on an appropriately labelled dataset.
