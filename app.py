import os, io, csv, json, secrets, hashlib, hmac, time, threading, re
from pathlib import Path
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Flask, request, redirect, url_for, session, render_template, flash, send_file, abort
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask_sqlalchemy import SQLAlchemy

BASE = Path(__file__).resolve().parent
UPLOAD_DIR = BASE / 'uploads'
UPLOAD_DIR.mkdir(exist_ok=True)
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', f'sqlite:///{BASE / "app.db"}').replace('postgres://','postgresql://')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 30 * 1024 * 1024
db = SQLAlchemy(app)

ALLOWED = {'.png','.jpg','.jpeg','.webp'}
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    plan = db.Column(db.String(30), default='free', nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    jobs = db.relationship('Job', backref='user', lazy=True)
class Job(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    status = db.Column(db.String(30), default='queued')
    total = db.Column(db.Integer, default=0)
    processed = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    finished_at = db.Column(db.DateTime)
    error = db.Column(db.Text, default='')
    results = db.relationship('Result', backref='job', lazy=True, cascade='all, delete-orphan')
class Result(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey('job.id'), nullable=False)
    image_name = db.Column(db.String(255), nullable=False)
    shipping_rs = db.Column(db.Float)
    meesho_price_rs = db.Column(db.Float)
    status = db.Column(db.String(60), default='Needs review')
    checked_at = db.Column(db.DateTime, default=datetime.utcnow)


def current_user():
    uid = session.get('user_id')
    return db.session.get(User, uid) if uid else None

def login_required(fn):
    @wraps(fn)
    def wrapped(*a, **kw):
        if not current_user(): return redirect(url_for('login'))
        return fn(*a, **kw)
    return wrapped

def admin_required(fn):
    @wraps(fn)
    def wrapped(*a, **kw):
        u=current_user()
        if not u: return redirect(url_for('login'))
        if not u.is_admin: abort(403)
        return fn(*a, **kw)
    return wrapped

@app.context_processor
def inject_user(): return {'user': current_user()}

@app.route('/')
def home():
    if not current_user(): return redirect(url_for('login'))
    jobs=Job.query.filter_by(user_id=current_user().id).order_by(Job.created_at.desc()).limit(10).all()
    return render_template('dashboard.html', jobs=jobs)

@app.route('/register', methods=['GET','POST'])
def register():
    if request.method=='POST':
        email=request.form.get('email','').strip().lower(); pw=request.form.get('password','')
        if not email or len(pw)<10:
            flash('Valid email aur kam se kam 10-character password zaroori hai.','error')
        elif User.query.filter_by(email=email).first(): flash('Is email ka account pehle se hai.','error')
        else:
            u=User(email=email,password_hash=generate_password_hash(pw))
            db.session.add(u); db.session.commit(); session['user_id']=u.id
            flash('Account created. Free testing plan active hai.','success'); return redirect(url_for('home'))
    return render_template('auth.html', mode='register')

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method=='POST':
        u=User.query.filter_by(email=request.form.get('email','').strip().lower()).first()
        if u and check_password_hash(u.password_hash, request.form.get('password','')):
            session.clear(); session['user_id']=u.id; return redirect(url_for('home'))
        flash('Email ya password sahi nahi hai.','error')
    return render_template('auth.html', mode='login')

@app.route('/logout', methods=['POST'])
def logout(): session.clear(); return redirect(url_for('login'))

@app.route('/jobs', methods=['POST'])
@login_required
def create_job():
    files=request.files.getlist('images')
    files=[f for f in files if f and f.filename and Path(f.filename).suffix.lower() in ALLOWED]
    if not files: flash('JPG, PNG ya WEBP images select karein.','error'); return redirect(url_for('home'))
    if len(files)>100: flash('Free testing MVP mein ek job mein maximum 100 images allowed hain.','error'); return redirect(url_for('home'))
    u=current_user()
    if u.plan=='free' and Job.query.filter_by(user_id=u.id).count()>=10:
        flash('Free test limit 10 jobs hai. Admin se plan enable karwa sakte hain.','error'); return redirect(url_for('home'))
    job=Job(user_id=u.id,total=len(files),status='queued'); db.session.add(job); db.session.commit()
    jobdir=UPLOAD_DIR/str(job.id); jobdir.mkdir(parents=True,exist_ok=True)
    for f in files:
        name=secure_filename(f.filename) or 'image'; f.save(jobdir/name)
        db.session.add(Result(job_id=job.id,image_name=name,status='Queued'))
    db.session.commit()
    # Explicitly keep cloud automation disabled until a per-user authenticated browser/session is provisioned.
    job.status='needs_setup'; job.error='Images uploaded securely. Cloud Playwright worker requires a dedicated per-user browser session and verified Meesho selectors before real capture.'; db.session.commit()
    flash('Job create ho gaya. Cloud browser integration setup required hai; results ko verified shipping data nahi maana jayega.','success')
    return redirect(url_for('job_detail',job_id=job.id))

@app.route('/jobs/<int:job_id>')
@login_required
def job_detail(job_id):
    job=Job.query.filter_by(id=job_id,user_id=current_user().id).first_or_404()
    return render_template('job.html',job=job,results=job.results)

@app.route('/jobs/<int:job_id>/export/<kind>')
@login_required
def export_job(job_id,kind):
    job=Job.query.filter_by(id=job_id,user_id=current_user().id).first_or_404()
    rows=[{'image_name':r.image_name,'meesho_price_rs':r.meesho_price_rs,'shipping_rs':r.shipping_rs,'status':r.status,'checked_at':r.checked_at.isoformat() if r.checked_at else None} for r in job.results]
    if kind=='json': data=json.dumps(rows,ensure_ascii=False,indent=2).encode('utf-8'); mimetype='application/json'; name=f'job_{job.id}.json'
    elif kind=='csv':
        s=io.StringIO(); w=csv.DictWriter(s,fieldnames=['image_name','meesho_price_rs','shipping_rs','status','checked_at']); w.writeheader(); w.writerows(rows); data=('\ufeff'+s.getvalue()).encode('utf-8'); mimetype='text/csv'; name=f'job_{job.id}.csv'
    else: abort(404)
    return send_file(io.BytesIO(data),mimetype=mimetype,as_attachment=True,download_name=name)

@app.route('/history')
@login_required
def history():
    jobs=Job.query.filter_by(user_id=current_user().id).order_by(Job.created_at.desc()).all()
    return render_template('history.html',jobs=jobs)

@app.route('/admin')
@admin_required
def admin(): return render_template('admin.html',users=User.query.order_by(User.created_at.desc()).all(),jobs=Job.query.order_by(Job.created_at.desc()).limit(100).all())

@app.route('/admin/users/<int:uid>/plan', methods=['POST'])
@admin_required
def admin_plan(uid):
    u=db.session.get(User,uid)
    if not u: abort(404)
    plan=request.form.get('plan','free')
    if plan not in {'free','monthly'}: abort(400)
    u.plan=plan; db.session.commit(); flash(f'{u.email} plan updated: {plan}','success'); return redirect(url_for('admin'))

@app.route('/billing')
@login_required
def billing(): return render_template('billing.html')

@app.route('/browserless-test', methods=['GET','POST'])
@login_required
def browserless_test():
    """Smoke-test the managed browser connection only; does not log in to Meesho."""
    token = os.environ.get('BROWSERLESS_TOKEN', '').strip()
    if not token:
        return {'ok': False, 'error': 'BROWSERLESS_TOKEN is not configured on the server.'}, 503
    endpoint = os.environ.get('BROWSERLESS_WS_ENDPOINT', 'wss://production-sfo.browserless.io').strip()
    # Prevent accidental token exposure through logs or user-facing errors.
    from urllib.parse import urlencode
    ws_url = endpoint.rstrip('/') + '?' + urlencode({'token': token})
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(ws_url, timeout=30000)
            try:
                contexts = browser.contexts
                context = contexts[0] if contexts else browser.new_context()
                page = context.new_page()
                page.goto('https://example.com', wait_until='domcontentloaded', timeout=20000)
                title = page.title()
                page.close()
                if title != 'Example Domain':
                    return {'ok': False, 'error': 'Browser connected but smoke-test page title did not match.'}, 502
                return {'ok': True, 'message': 'Browserless connection successful.', 'test_page_title': title,
                        'note': 'This verifies browser connectivity only. Meesho login, per-user session isolation, and selectors are not yet configured.'}
            finally:
                browser.close()
    except Exception as exc:
        # Do not return raw exception strings because they may contain connection details.
        app.logger.warning('Browserless smoke test failed: %s', type(exc).__name__)
        return {'ok': False, 'error': 'Browserless connection failed. Check token, endpoint, plan limits, and Render logs.',
                'error_type': type(exc).__name__}, 502

@app.route('/health')
def health(): return {'status':'ok','service':'meesho-shipping-checker-saas'}

with app.app_context():
    db.create_all()
    # Bootstrap admin only when explicitly configured; never ship a hard-coded admin password.
    admin_email=os.environ.get('ADMIN_EMAIL','').strip().lower(); admin_password=os.environ.get('ADMIN_PASSWORD','')
    if admin_email and admin_password and not User.query.filter_by(email=admin_email).first():
        db.session.add(User(email=admin_email,password_hash=generate_password_hash(admin_password),is_admin=True,plan='monthly')); db.session.commit()

if __name__=='__main__':
    app.run(host='127.0.0.1',port=int(os.environ.get('PORT','5000')),debug=False)
