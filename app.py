import os, re, uuid, csv, io, tempfile
from datetime import datetime, timedelta
from functools import wraps
from collections import Counter

from flask import Flask, request, redirect, url_for, flash, render_template_string, send_file, abort
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_wtf.csrf import CSRFProtect, generate_csrf
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__, instance_path=os.getenv("INSTANCE_PATH", os.path.join(tempfile.gettempdir(), "civiceye_instance")))
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "change-this-before-deployment")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///civiceye.db").replace("postgres://", "postgresql://", 1)
if app.config["SQLALCHEMY_DATABASE_URI"].startswith("mysql://"):
    app.config["SQLALCHEMY_DATABASE_URI"] = app.config["SQLALCHEMY_DATABASE_URI"].replace("mysql://", "mysql+pymysql://", 1)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
app.config["UPLOAD_FOLDER"] = os.getenv("UPLOAD_FOLDER", "/tmp/civiceye_uploads" if os.getenv("VERCEL") else "uploads")
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
csrf = CSRFProtect(app)

CATEGORIES = ["Road Damage", "Garbage", "Water Leakage", "Streetlight", "Drainage", "Other"]
STATUSES = ["Submitted", "Under Review", "Assigned", "In Progress", "Resolved", "Rejected", "Reopened"]
ALLOWED_EXT = {"png", "jpg", "jpeg", "webp"}

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(160), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="citizen")
    department = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Complaint(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    public_id = db.Column(db.String(20), unique=True, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    assigned_to = db.Column(db.Integer, db.ForeignKey("user.id"))
    title = db.Column(db.String(180), nullable=False)
    description = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(50), nullable=False)
    predicted_category = db.Column(db.String(50))
    confidence = db.Column(db.Float, default=0)
    urgency = db.Column(db.String(20), default="Normal")
    severity = db.Column(db.String(20), default="Medium")
    status = db.Column(db.String(30), default="Submitted")
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)
    image_name = db.Column(db.String(255))
    image_quality = db.Column(db.String(30))
    duplicate_of = db.Column(db.Integer, db.ForeignKey("complaint.id"))
    duplicate_score = db.Column(db.Float, default=0)
    official_notes = db.Column(db.Text)
    after_image = db.Column(db.String(255))
    ssim_score = db.Column(db.Float)
    admin_approved = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    citizen = db.relationship("User", foreign_keys=[user_id])
    official = db.relationship("User", foreign_keys=[assigned_to])

class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    complaint_id = db.Column(db.Integer, db.ForeignKey("complaint.id"))
    action = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Notice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    message = db.Column(db.String(300), nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

def role_required(*roles):
    def deco(fn):
        @wraps(fn)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user.role not in roles:
                abort(403)
            return fn(*args, **kwargs)
        return wrapped
    return deco

def log_action(action, complaint=None):
    db.session.add(AuditLog(user_id=current_user.id if current_user.is_authenticated else None,
                            complaint_id=complaint.id if complaint else None, action=action))

def notify(user_id, message):
    if user_id:
        db.session.add(Notice(user_id=user_id, message=message))

def allowed_file(name):
    return "." in name and name.rsplit(".", 1)[1].lower() in ALLOWED_EXT

def analyze_text(title, description):
    text = (title + " " + description).lower()
    rules = {
        "Road Damage": ["pothole", "road", "crack", "damaged road"],
        "Garbage": ["garbage", "waste", "trash", "dump"],
        "Water Leakage": ["leak", "water", "pipe", "overflow"],
        "Streetlight": ["streetlight", "street light", "lamp", "dark"],
        "Drainage": ["drain", "sewage", "blocked", "stagnant"]
    }
    scores = {cat: sum(1 for word in words if word in text) for cat, words in rules.items()}
    best = max(scores, key=scores.get)
    total = sum(scores.values())
    confidence = min(0.98, 0.55 + scores[best] * 0.1) if total else 0.35
    urgency = "High" if any(w in text for w in ["urgent", "danger", "accident", "school", "hospital", "overflow"]) else "Normal"
    severity = "High" if any(w in text for w in ["large", "major", "severe", "danger", "collapse"]) else ("Low" if any(w in text for w in ["small", "minor"]) else "Medium")
    return best if total else "Other", confidence, urgency, severity

def duplicate_check(title, description, exclude=None):
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity
        rows = Complaint.query.filter(Complaint.status.notin_(["Resolved", "Rejected"])).all()
        rows = [r for r in rows if r.id != exclude]
        if not rows: return None, 0.0
        docs = [title + " " + description] + [r.title + " " + r.description for r in rows]
        matrix = TfidfVectorizer(stop_words="english", ngram_range=(1,2)).fit_transform(docs)
        sims = cosine_similarity(matrix[0:1], matrix[1:]).flatten()
        index = int(sims.argmax())
        return (rows[index], float(sims[index])) if sims[index] >= 0.45 else (None, float(sims[index]))
    except Exception:
        return None, 0.0

def inspect_image(path):
    try:
        import cv2
        image = cv2.imread(path)
        if image is None: return "Unreadable"
        h, w = image.shape[:2]
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        blur = cv2.Laplacian(gray, cv2.CV_64F).var()
        brightness = float(gray.mean())
        if w < 240 or h < 240: return "Low resolution"
        if blur < 35: return "Possibly blurred"
        if brightness < 25 or brightness > 235: return "Poor lighting"
        return "Good"
    except Exception:
        return "Not checked (OpenCV unavailable)"

def compare_images(before_path, after_path):
    try:
        import cv2
        from skimage.metrics import structural_similarity
        a, b = cv2.imread(before_path), cv2.imread(after_path)
        if a is None or b is None: return None
        b = cv2.resize(b, (a.shape[1], a.shape[0]))
        score = structural_similarity(cv2.cvtColor(a, cv2.COLOR_BGR2GRAY),
                                      cv2.cvtColor(b, cv2.COLOR_BGR2GRAY))
        return round(float(score), 4)
    except Exception:
        return None

def page(content, **kwargs):
    shell = """
    <!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="csrf-token" content="{{ csrf_token() }}"><title>{{ title or 'CivicEye' }}</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
    <style>body{background:#f4f7fb;color:#172033}.navbar{background:#10233f}.brand{font-weight:800;letter-spacing:.4px}.hero{background:linear-gradient(120deg,#10233f,#176b87);color:white;border-radius:22px}.card{border:0;border-radius:16px;box-shadow:0 5px 22px #13294b0c}.metric{font-size:2rem;font-weight:800}.muted{color:#718096}.pill{border-radius:30px;padding:5px 11px;background:#e8f2f8;font-size:.8rem}.btn-primary{background:#176b87;border-color:#176b87}.table{vertical-align:middle}.map{height:360px;border-radius:14px}</style></head><body>
    <nav class="navbar navbar-expand-lg navbar-dark"><div class="container"><a class="navbar-brand brand" href="/">CIVICEYE</a><div class="ms-auto d-flex gap-2 align-items-center">
    {% if current_user.is_authenticated %}<span class="text-white small">{{ current_user.name }} · {{ current_user.role|title }}</span><a class="btn btn-sm btn-outline-light" href="{{ url_for('dashboard') }}">Dashboard</a><a class="btn btn-sm btn-light" href="{{ url_for('logout') }}">Logout</a>{% else %}<a class="btn btn-sm btn-outline-light" href="{{ url_for('login') }}">Login</a><a class="btn btn-sm btn-light" href="{{ url_for('register') }}">Register</a>{% endif %}</div></div></nav>
    <main class="container py-4">{% with messages=get_flashed_messages(with_categories=true) %}{% for category,message in messages %}<div class="alert alert-{{ category }} alert-dismissible">{{ message }}<button class="btn-close" data-bs-dismiss="alert"></button></div>{% endfor %}{% endwith %}""" + content + """</main>
    <footer class="container py-4 text-center muted small">CivicEye · AI-assisted public grievance analysis · Human review remains mandatory</footer>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script><script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script></body></html>"""
    return render_template_string(shell, **kwargs)

@app.route("/")
def home():
    if current_user.is_authenticated: return redirect(url_for("dashboard"))
    return page("""
    <section class="hero p-5 mb-4"><div class="row align-items-center"><div class="col-lg-8"><span class="pill text-dark">SMART GOVERNANCE · TRANSPARENT SERVICE</span><h1 class="display-5 fw-bold mt-3">Every complaint deserves action.</h1><p class="lead">A unified platform to report civic issues, track progress and help authorities identify unresolved grievance hotspots.</p><a class="btn btn-light btn-lg me-2" href="{{ url_for('register') }}">Report an issue</a><a class="btn btn-outline-light btn-lg" href="{{ url_for('login') }}">Sign in</a></div><div class="col-lg-4 text-center display-1">🏛️</div></div></section>
    <div class="row g-3"><div class="col-md-4"><div class="card p-4 h-100"><h5>AI-assisted analysis</h5><p class="muted mb-0">Duplicate suggestions, category, urgency and severity signals.</p></div></div><div class="col-md-4"><div class="card p-4 h-100"><h5>Location intelligence</h5><p class="muted mb-0">Map complaint density and reveal recurring public issues.</p></div></div><div class="col-md-4"><div class="card p-4 h-100"><h5>Human-verified closure</h5><p class="muted mb-0">Officials submit evidence; an administrator approves final resolution.</p></div></div></div>""", title="CivicEye")

@app.route("/register", methods=["GET","POST"])
def register():
    if request.method == "POST":
        name=request.form.get("name","").strip(); email=request.form.get("email","").lower().strip(); password=request.form.get("password","")
        if not name or not email or len(password)<8:
            flash("Enter your name, a valid email and a password with at least 8 characters.","warning")
        elif User.query.filter_by(email=email).first():
            flash("That email is already registered.","warning")
        else:
            user=User(name=name,email=email,password_hash=generate_password_hash(password),role="citizen")
            db.session.add(user); db.session.commit(); login_user(user); flash("Welcome! Your citizen account is ready.","success")
            return redirect(url_for("dashboard"))
    return page("""<div class="row justify-content-center"><div class="col-md-6"><div class="card p-4"><h2>Create citizen account</h2><form method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><label class="form-label">Full name</label><input class="form-control mb-3" name="name" required><label class="form-label">Email</label><input class="form-control mb-3" type="email" name="email" required><label class="form-label">Password (minimum 8 characters)</label><input class="form-control mb-3" type="password" name="password" minlength="8" required><button class="btn btn-primary w-100">Create account</button></form></div></div></div>""", title="Register")

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method=="POST":
        user=User.query.filter_by(email=request.form.get("email","").lower().strip()).first()
        if user and check_password_hash(user.password_hash,request.form.get("password","")):
            login_user(user); return redirect(url_for("dashboard"))
        flash("Incorrect email or password.","danger")
    return page("""<div class="row justify-content-center"><div class="col-md-5"><div class="card p-4"><h2>Sign in</h2><form method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><label class="form-label">Email</label><input class="form-control mb-3" type="email" name="email" required><label class="form-label">Password</label><input class="form-control mb-3" type="password" name="password" required><button class="btn btn-primary w-100">Login</button></form></div></div></div>""", title="Login")

@app.route("/logout")
@login_required
def logout():
    logout_user(); return redirect(url_for("home"))

@app.route("/dashboard")
@login_required
def dashboard():
    if current_user.role=="citizen":
        rows=Complaint.query.filter_by(user_id=current_user.id).order_by(Complaint.created_at.desc()).all()
    elif current_user.role=="official":
        rows=Complaint.query.filter_by(assigned_to=current_user.id).order_by(Complaint.created_at.desc()).all()
    else: rows=Complaint.query.order_by(Complaint.created_at.desc()).all()
    counts={s:sum(1 for c in rows if c.status==s) for s in STATUSES}
    notices=Notice.query.filter_by(user_id=current_user.id).order_by(Notice.created_at.desc()).limit(5).all()
    return page("""
    <div class="d-flex justify-content-between align-items-center mb-4"><div><h2 class="fw-bold mb-1">{{ heading }}</h2><p class="muted mb-0">Complaint overview and action centre</p></div>{% if current_user.role=='citizen' %}<a class="btn btn-primary" href="{{ url_for('new_complaint') }}">＋ New complaint</a>{% endif %}</div>
    <div class="row g-3 mb-4">{% for label,value in metrics.items() %}<div class="col-6 col-lg-3"><div class="card p-3"><div class="muted small">{{ label }}</div><div class="metric">{{ value }}</div></div></div>{% endfor %}</div>
    {% if current_user.role=='admin' %}<div class="mb-3 d-flex gap-2 flex-wrap"><a class="btn btn-outline-primary" href="{{ url_for('analytics') }}">Hotspot analytics & map</a><a class="btn btn-outline-primary" href="{{ url_for('users') }}">Manage users</a><a class="btn btn-outline-primary" href="{{ url_for('export_csv') }}">Export CSV</a></div>{% endif %}
    {% if notices %}<div class="card p-3 mb-3"><b>Notifications</b>{% for n in notices %}<div class="small py-1">{{ n.message }} <span class="muted">{{ n.created_at.strftime('%d %b %H:%M') }}</span></div>{% endfor %}</div>{% endif %}
    <div class="card p-3"><div class="d-flex justify-content-between"><h5>Complaints</h5><span class="muted small">{{ rows|length }} records</span></div><div class="table-responsive"><table class="table"><thead><tr><th>ID</th><th>Issue</th><th>Category</th><th>Status</th><th>Priority</th><th>Date</th></tr></thead><tbody>{% for c in rows %}<tr><td><a href="{{ url_for('complaint_detail', complaint_id=c.id) }}">{{ c.public_id }}</a></td><td>{{ c.title }}</td><td>{{ c.category }}</td><td><span class="pill">{{ c.status }}</span></td><td>{{ c.urgency }} / {{ c.severity }}</td><td>{{ c.created_at.strftime('%d %b %Y') }}</td></tr>{% else %}<tr><td colspan="6" class="text-center muted py-4">No complaints yet.</td></tr>{% endfor %}</tbody></table></div></div>
    """, title="Dashboard", heading={"citizen":"My complaints","official":"Assigned complaints","admin":"Administration dashboard"}[current_user.role], rows=rows,
    metrics={"Total":len(rows),"Submitted":counts["Submitted"],"In progress":counts["In Progress"],"Resolved":counts["Resolved"]}, notices=notices)

@app.route("/complaints/new", methods=["GET","POST"])
@role_required("citizen")
def new_complaint():
    if request.method=="POST":
        title=request.form.get("title","").strip(); desc=request.form.get("description","").strip(); category=request.form.get("category","Other")
        if not title or not desc or category not in CATEGORIES:
            flash("Please complete all required fields.","warning")
        else:
            image_name=None; image=request.files.get("photo")
            if image and image.filename:
                if not allowed_file(image.filename): flash("Upload PNG, JPG or WEBP only.","danger"); return redirect(url_for("new_complaint"))
                image_name=uuid.uuid4().hex+"_"+secure_filename(image.filename)
                image.save(os.path.join(app.config["UPLOAD_FOLDER"],image_name))
            predicted,confidence,urgency,severity=analyze_text(title,desc)
            dup,score=duplicate_check(title,desc)
            c=Complaint(public_id="CIV-"+uuid.uuid4().hex[:8].upper(),user_id=current_user.id,title=title,description=desc,category=category,
                predicted_category=predicted,confidence=confidence,urgency=urgency,severity=severity,
                latitude=float(request.form["latitude"]) if request.form.get("latitude") else None,
                longitude=float(request.form["longitude"]) if request.form.get("longitude") else None,
                image_name=image_name,image_quality=inspect_image(os.path.join(app.config["UPLOAD_FOLDER"],image_name)) if image_name else "No photo")
            if dup: c.duplicate_of=dup.id; c.duplicate_score=score
            db.session.add(c); db.session.flush(); log_action("Complaint submitted",c)
            admins=User.query.filter_by(role="admin").all()
            for admin in admins: notify(admin.id,"New complaint "+c.public_id+" requires review.")
            db.session.commit()
            flash("Complaint submitted. Reference: "+c.public_id+(". A possible duplicate was flagged for official review." if dup else ""),"success")
            return redirect(url_for("complaint_detail",complaint_id=c.id))
    return page("""
    <div class="row justify-content-center"><div class="col-lg-8"><div class="card p-4"><h2>Report a civic issue</h2><p class="muted">Add a clear description and optional photo/location.</p><form method="post" enctype="multipart/form-data"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <label class="form-label">Complaint title</label><input class="form-control mb-3" name="title" maxlength="180" required>
    <label class="form-label">Description</label><textarea class="form-control mb-3" name="description" rows="4" required></textarea>
    <label class="form-label">Category</label><select class="form-select mb-3" name="category">{% for c in categories %}<option>{{ c }}</option>{% endfor %}</select>
    <label class="form-label">Photo (PNG/JPG/WEBP, max 8 MB)</label><input class="form-control mb-3" type="file" name="photo" accept=".png,.jpg,.jpeg,.webp">
    <div class="row"><div class="col"><label class="form-label">Latitude</label><input class="form-control mb-3" name="latitude" id="lat" type="number" step="any"></div><div class="col"><label class="form-label">Longitude</label><input class="form-control mb-3" name="longitude" id="lng" type="number" step="any"></div></div>
    <button type="button" class="btn btn-outline-secondary mb-3" onclick="navigator.geolocation?.getCurrentPosition(p=>{document.getElementById('lat').value=p.coords.latitude;document.getElementById('lng').value=p.coords.longitude},()=>alert('Location permission unavailable'))">Use my location</button><button class="btn btn-primary w-100">Submit complaint</button></form></div></div></div>
    """, title="New complaint", categories=CATEGORIES)

@app.route("/complaint/<int:complaint_id>")
@login_required
def complaint_detail(complaint_id):
    c=Complaint.query.get_or_404(complaint_id)
    if current_user.role=="citizen" and c.user_id!=current_user.id: abort(403)
    if current_user.role=="official" and c.assigned_to!=current_user.id: abort(403)
    officials=User.query.filter_by(role="official").all() if current_user.role=="admin" else []
    return page("""
    <div class="d-flex justify-content-between"><div><span class="pill">{{ c.public_id }}</span><h2 class="fw-bold mt-2">{{ c.title }}</h2><p class="muted">{{ c.created_at.strftime('%d %B %Y, %H:%M') }} · {{ c.category }}</p></div><span class="pill align-self-start">{{ c.status }}</span></div>
    <div class="row g-3"><div class="col-lg-7"><div class="card p-4 mb-3"><h5>Description</h5><p>{{ c.description }}</p><hr><div class="row"><div class="col"><b>AI suggested category</b><div>{{ c.predicted_category }} · {{ (c.confidence*100)|round|int }}% confidence</div></div><div class="col"><b>Urgency / Severity</b><div>{{ c.urgency }} / {{ c.severity }}</div></div></div><p class="small muted mt-3">AI output is advisory; uncertain or conflicting results require human review.</p>
    {% if c.duplicate_of %}<div class="alert alert-warning mb-0">Possible duplicate of complaint #{{ c.duplicate_of }} (similarity {{ (c.duplicate_score*100)|round|int }}%). This is a suggestion, not an automatic rejection.</div>{% endif %}
    {% if c.image_quality %}<p class="mt-3 mb-0"><b>Photo quality:</b> {{ c.image_quality }}</p>{% endif %}
    {% if c.latitude and c.longitude %}<p class="mt-2 mb-0"><b>GPS:</b> {{ c.latitude }}, {{ c.longitude }}</p>{% endif %}</div>
    {% if current_user.role in ['official','admin'] %}<div class="card p-4"><h5>Workflow action</h5><form method="post" action="{{ url_for('update_complaint',complaint_id=c.id) }}"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    {% if current_user.role=='admin' %}<label class="form-label">Assign official</label><select class="form-select mb-3" name="assigned_to"><option value="">Unassigned</option>{% for u in officials %}<option value="{{ u.id }}" {% if c.assigned_to==u.id %}selected{% endif %}>{{ u.name }} ({{ u.department or 'General' }})</option>{% endfor %}</select>{% endif %}
    <label class="form-label">Update status</label><select class="form-select mb-3" name="status">{% for s in statuses %}<option {% if c.status==s %}selected{% endif %}>{{ s }}</option>{% endfor %}</select>
    <label class="form-label">Official notes</label><textarea class="form-control mb-3" name="notes" rows="2">{{ c.official_notes or '' }}</textarea><button class="btn btn-primary">Save workflow update</button></form>
    {% if current_user.role=='official' %}<hr><form method="post" enctype="multipart/form-data" action="{{ url_for('submit_resolution',complaint_id=c.id) }}"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><label class="form-label">After-resolution photo</label><input class="form-control mb-2" type="file" name="after_photo" accept=".jpg,.jpeg,.png,.webp" required><button class="btn btn-outline-primary">Submit for admin verification</button></form>{% endif %}
    {% if current_user.role=='admin' and c.after_image %}<hr><p><b>Before/after SSIM:</b> {{ c.ssim_score if c.ssim_score is not none else 'Not available' }} (higher means more visually similar)</p><form method="post" action="{{ url_for('approve_resolution',complaint_id=c.id) }}"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><button class="btn btn-success" {% if c.admin_approved %}disabled{% endif %}>Approve final resolution</button></form>{% endif %}</div>{% endif %}
    </div><div class="col-lg-5"><div class="card p-3"><h5>Evidence</h5>{% if c.image_name %}<img class="img-fluid rounded mb-2" src="{{ url_for('uploaded_file',name=c.image_name) }}" alt="Complaint photo">{% else %}<p class="muted">No original photo uploaded.</p>{% endif %}{% if c.after_image %}<h6 class="mt-3">After-resolution photo</h6><img class="img-fluid rounded" src="{{ url_for('uploaded_file',name=c.after_image) }}" alt="After resolution"></div>{% endif %}</div></div>
    """, title=c.public_id, c=c, officials=officials, statuses=STATUSES)

@app.route("/complaint/<int:complaint_id>/update", methods=["POST"])
@role_required("official","admin")
def update_complaint(complaint_id):
    c=Complaint.query.get_or_404(complaint_id)
    if current_user.role=="official" and c.assigned_to!=current_user.id: abort(403)
    status=request.form.get("status")
    if status in STATUSES:
        c.status=status; c.official_notes=request.form.get("notes","")[:4000]
        if current_user.role=="admin" and request.form.get("assigned_to"):
            official=User.query.filter_by(id=int(request.form["assigned_to"]),role="official").first()
            if official: c.assigned_to=official.id; c.status="Assigned"; notify(official.id,"Complaint "+c.public_id+" assigned to you.")
        notify(c.user_id,"Your complaint "+c.public_id+" status changed to "+c.status+".")
        log_action("Status/assignment updated: "+c.status,c); db.session.commit(); flash("Complaint workflow updated.","success")
    return redirect(url_for("complaint_detail",complaint_id=c.id))

@app.route("/complaint/<int:complaint_id>/resolution", methods=["POST"])
@role_required("official")
def submit_resolution(complaint_id):
    c=Complaint.query.get_or_404(complaint_id)
    if c.assigned_to!=current_user.id: abort(403)
    f=request.files.get("after_photo")
    if not f or not f.filename or not allowed_file(f.filename): flash("Choose a valid PNG/JPG/WEBP photo.","warning")
    else:
        name=uuid.uuid4().hex+"_"+secure_filename(f.filename); path=os.path.join(app.config["UPLOAD_FOLDER"],name); f.save(path)
        c.after_image=name
        before=os.path.join(app.config["UPLOAD_FOLDER"],c.image_name) if c.image_name else None
        c.ssim_score=compare_images(before,path) if before and os.path.exists(before) else None
        c.status="Under Review"; c.admin_approved=False
        for admin in User.query.filter_by(role="admin").all(): notify(admin.id,"Resolution evidence submitted for "+c.public_id+". Admin approval required.")
        notify(c.user_id,"An official submitted resolution evidence for "+c.public_id+". Awaiting administrator review.")
        log_action("Resolution evidence submitted for human review",c); db.session.commit()
        flash("Evidence submitted. The administrator must approve closure.","success")
    return redirect(url_for("complaint_detail",complaint_id=c.id))

@app.route("/complaint/<int:complaint_id>/approve", methods=["POST"])
@role_required("admin")
def approve_resolution(complaint_id):
    c=Complaint.query.get_or_404(complaint_id)
    if not c.after_image: flash("No after photo is available.","warning")
    else:
        c.admin_approved=True; c.status="Resolved"; notify(c.user_id,"Your complaint "+c.public_id+" has been approved as resolved.")
        log_action("Administrator approved final resolution",c); db.session.commit(); flash("Final resolution approved by administrator.","success")
    return redirect(url_for("complaint_detail",complaint_id=c.id))

@app.route("/uploads/<name>")
@login_required
def uploaded_file(name):
    return send_file(os.path.join(app.config["UPLOAD_FOLDER"],secure_filename(name)))

@app.route("/admin/users")
@role_required("admin")
def users():
    people=User.query.order_by(User.created_at.desc()).all()
    return page("""<div class="card p-4"><h3>Account management</h3><p class="muted">Create official/admin accounts. Public registration creates citizen accounts only.</p><form method="post" action="{{ url_for('create_staff') }}" class="row g-2 mb-4"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}"><div class="col-md-3"><input class="form-control" name="name" placeholder="Name" required></div><div class="col-md-3"><input class="form-control" type="email" name="email" placeholder="Email" required></div><div class="col-md-2"><input class="form-control" type="password" name="password" placeholder="Password (8+)" minlength="8" required></div><div class="col-md-2"><select class="form-select" name="role"><option>official</option><option>admin</option></select></div><div class="col-md-2"><input class="form-control" name="department" placeholder="Department"></div><div class="col-12"><button class="btn btn-primary">Create staff account</button></div></form><table class="table"><tr><th>Name</th><th>Email</th><th>Role</th><th>Department</th></tr>{% for u in people %}<tr><td>{{ u.name }}</td><td>{{ u.email }}</td><td>{{ u.role }}</td><td>{{ u.department or '—' }}</td></tr>{% endfor %}</table></div>""",title="Users",people=people)

@app.route("/admin/users/create",methods=["POST"])
@role_required("admin")
def create_staff():
    email=request.form.get("email","").lower().strip(); password=request.form.get("password","")
    if User.query.filter_by(email=email).first() or len(password)<8:
        flash("Email already exists or password is too short.","danger")
    else:
        role=request.form.get("role")
        if role not in ["official","admin"]: abort(400)
        db.session.add(User(name=request.form.get("name","").strip(),email=email,password_hash=generate_password_hash(password),role=role,department=request.form.get("department","")))
        log_action("Created "+role+" account"); db.session.commit(); flash("Staff account created.","success")
    return redirect(url_for("users"))

@app.route("/admin/analytics")
@role_required("admin")
def analytics():
    rows=Complaint.query.all()
    points=[{"lat":c.latitude,"lng":c.longitude,"title":c.title,"category":c.category,"status":c.status} for c in rows if c.latitude is not None and c.longitude is not None]
    clusters=[]
    if len(points)>=3:
        try:
            from sklearn.cluster import DBSCAN
            import numpy as np
            coords=np.radians([[p["lat"],p["lng"]] for p in points])
            labels=DBSCAN(eps=0.5/6371.0,min_samples=3,metric="haversine").fit_predict(coords)
            for label in set(labels):
                if label!=-1:
                    group=[points[i] for i,v in enumerate(labels) if v==label]
                    clusters.append({"lat":sum(p["lat"] for p in group)/len(group),"lng":sum(p["lng"] for p in group)/len(group),"count":len(group)})
        except Exception: clusters=[]
    categories=dict(Counter(c.category for c in rows))
    overdue=[c for c in rows if c.status not in ["Resolved","Rejected"] and c.created_at<datetime.utcnow()-timedelta(days=7)]
    return page("""
    <h2 class="fw-bold">Grievance intelligence</h2><p class="muted">Observed complaint records only. Forecasting is withheld until sufficient historical time-series data exists.</p>
    <div class="row g-3 mb-3"><div class="col-md-4"><div class="card p-3"><div class="muted">GPS-tagged complaints</div><div class="metric">{{ points|length }}</div></div></div><div class="col-md-4"><div class="card p-3"><div class="muted">Observed DBSCAN clusters</div><div class="metric">{{ clusters|length }}</div></div></div><div class="col-md-4"><div class="card p-3"><div class="muted">Open over 7 days</div><div class="metric">{{ overdue|length }}</div></div></div></div>
    <div class="card p-3 mb-3"><h5>Observed complaint locations</h5><div id="map" class="map"></div>{% if not points %}<p class="muted mt-2">No GPS records yet. Add latitude/longitude when submitting complaints.</p>{% endif %}</div>
    <div class="card p-3"><h5>Category distribution</h5><div class="row">{% for k,v in categories.items() %}<div class="col-md-4 py-2"><b>{{ k }}</b><div class="progress"><div class="progress-bar" style="width:{{ (v/total*100) if total else 0 }}%">{{ v }}</div></div></div>{% endfor %}</div></div>
    <script>const points={{ points|tojson }};const clusters={{ clusters|tojson }};const map=L.map('map').setView([20.59,78.96],5);L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{attribution:'© OpenStreetMap'}).addTo(map);points.forEach(p=>L.circleMarker([p.lat,p.lng],{radius:7}).addTo(map).bindPopup(p.title+' · '+p.category+' · '+p.status));clusters.forEach(c=>L.circle([c.lat,c.lng],{radius:350, color:'#e08b28'}).addTo(map).bindPopup('Observed hotspot · '+c.count+' complaints'));if(points.length)map.fitBounds(points.map(p=>[p.lat,p.lng]),{padding:[25,25]});</script>
    """,title="Analytics",points=points,clusters=clusters,categories=categories,total=len(rows),overdue=overdue)

@app.route("/admin/export.csv")
@role_required("admin")
def export_csv():
    output=io.StringIO(); writer=csv.writer(output)
    writer.writerow(["Complaint ID","Title","Category","Status","Urgency","Severity","Latitude","Longitude","Created","Duplicate of"])
    for c in Complaint.query.order_by(Complaint.created_at.desc()).all():
        writer.writerow([c.public_id,c.title,c.category,c.status,c.urgency,c.severity,c.latitude,c.longitude,c.created_at.isoformat(),c.duplicate_of or ""])
    return send_file(io.BytesIO(output.getvalue().encode("utf-8-sig")),mimetype="text/csv",as_attachment=True,download_name="civiceye_complaints.csv")

@app.errorhandler(403)
def forbidden(e): return page("<div class='card p-5 text-center'><h2>Access denied</h2><p>You do not have permission to view this page.</p><a href='/dashboard'>Back to dashboard</a></div>",title="Access denied"),403

@app.errorhandler(413)
def too_large(e): return page("<div class='alert alert-danger'>File is too large. Maximum upload size is 8 MB.</div>",title="Upload too large"),413

with app.app_context():
    db.create_all()
    # First-run demo accounts are optional and must be enabled explicitly.
    if os.getenv("SEED_DEMO_USERS","").lower()=="true" and not User.query.filter_by(email="admin@civiceye.local").first():
        db.session.add(User(name="Demo Administrator",email="admin@civiceye.local",password_hash=generate_password_hash(os.getenv("DEMO_PASSWORD","ChangeMe123!")),role="admin"))
        db.session.add(User(name="Demo Official",email="official@civiceye.local",password_hash=generate_password_hash(os.getenv("DEMO_PASSWORD","ChangeMe123!")),role="official",department="Public Works"))
        db.session.commit()

if __name__ == "__main__":
    app.run(host="0.0.0.0",port=int(os.getenv("PORT",5000)),debug=os.getenv("FLASK_DEBUG")=="1")
