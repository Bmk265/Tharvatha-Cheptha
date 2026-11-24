import os
import re
import json
import uuid
import hmac
import hashlib
import base64
from datetime import datetime
from functools import wraps
from urllib.parse import urlparse, urljoin, quote

import requests
from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, send_from_directory, jsonify
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, login_required,
    logout_user, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from sqlalchemy import text, inspect

try:
    from pywebpush import webpush, WebPushException
except ImportError:
    webpush = None
    WebPushException = Exception

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change-this-secret-key-in-render')

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024  # 20 MB max upload

VAPID_PUBLIC_KEY = os.environ.get('VAPID_PUBLIC_KEY', '').strip()
VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY', '').strip()
VAPID_CLAIMS_EMAIL = os.environ.get('VAPID_CLAIMS_EMAIL', 'mailto:admin@teampool.app').strip()

# Cashfree Payment Gateway (PG) only — Payouts intentionally omitted
CASHFREE_ENV = os.environ.get('CASHFREE_ENV', 'SANDBOX').strip().upper()
CF_PG_BASE = "https://sandbox.cashfree.com/pg" if CASHFREE_ENV == "SANDBOX" else "https://api.cashfree.com/pg"
CF_PG_ID = os.environ.get('CASHFREE_PG_APP_ID', '').strip()
CF_PG_SECRET = os.environ.get('CASHFREE_PG_SECRET_KEY', '').strip()

ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

db_url = os.environ.get('DATABASE_URL', 'sqlite:///team_expenses.db')
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

CATEGORIES = [
    "Monthly Deposit",
    "Vegetables",
    "Monthly Shopping",
    "Petrol",
    "Rice Bag",
    "Oil",
    "Chicken",
    "Curd",
    "Masala",
    "Water",
    "Outing Expenses",
    "Others (Specify)"
]

EXPENSE_CATEGORIES = [c for c in CATEGORIES if c != "Monthly Deposit"]


def _is_safe_url(target):
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ('http', 'https') and ref_url.netloc == test_url.netloc


def _allowed_image(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS


def _save_uploaded_image(file_storage):
    if not file_storage or not file_storage.filename:
        return None
    if not _allowed_image(file_storage.filename):
        return None
    ext = file_storage.filename.rsplit('.', 1)[1].lower()
    unique_name = f"{uuid.uuid4().hex}_{secure_filename(file_storage.filename)}"
    if not unique_name.lower().endswith(f".{ext}"):
        unique_name = f"{uuid.uuid4().hex}.{ext}"
    save_path = os.path.join(app.config['UPLOAD_FOLDER'], unique_name)
    file_storage.save(save_path)
    return f"uploads/{unique_name}"


def _format_category_and_remarks(category, custom_specify, remarks):
    spec = (custom_specify or '').strip()
    rem = (remarks or '').strip()
    if category == "Others (Specify)" and spec:
        combined = f"[{spec}] {rem}".strip() if rem else spec
        return category, combined
    return category, rem


def _build_rich_urn(user_name, material_label, prefix="RM"):
    safe_name = re.sub(r'[^A-Za-z0-9]', '', (user_name or 'USER').upper())[:8] or "USER"
    safe_mat = re.sub(r'[^A-Za-z0-9]', '', (material_label or 'ITEM').upper())[:10] or "ITEM"
    ts = datetime.now().strftime('%Y%m%d-%H%M')
    token = uuid.uuid4().hex[:4].upper()
    urn = f"{prefix}-{safe_name}-{ts}-{safe_mat}-{token}"
    while WalletTransaction.query.filter_by(urn=urn).first():
        urn = f"{prefix}-{safe_name}-{ts}-{safe_mat}-{uuid.uuid4().hex[:4].upper()}"
    return urn


def _extract_urn_from_remarks(remarks_text):
    if not remarks_text:
        return None
    match = re.search(r'\[URN:\s*([A-Za-z0-9\-]+)\]', remarks_text)
    return match.group(1) if match else None


@app.errorhandler(413)
def file_too_large(e):
    flash("Image file is too large. Please choose a photo under 20 MB.", "warning")
    return redirect(request.referrer or url_for('feed'))


# ---------------- MODELS ----------------
class Team(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    city = db.Column(db.String(100), nullable=True)
    invite_code = db.Column(db.String(32), unique=True, nullable=False)
    created_by = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)


class TeamMembership(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('team.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    role = db.Column(db.String(20), default='member')  # 'cashier' or 'member'
    personal_balance = db.Column(db.Float, default=0.0)
    joined_at = db.Column(db.DateTime, default=datetime.now)
    team = db.relationship('Team', backref=db.backref('memberships', cascade='all, delete-orphan'))
    user = db.relationship('User', backref='memberships')


class LeaveRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('team.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    reason = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(20), default='pending')  # 'pending', 'approved', 'rejected'
    created_at = db.Column(db.DateTime, default=datetime.now)
    team = db.relationship('Team', backref=db.backref('leave_requests', cascade='all, delete-orphan'))
    user = db.relationship('User', backref='leave_requests')


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    avatar_path = db.Column(db.String(255), nullable=True)
    is_super_admin = db.Column(db.Boolean, default=False)
    role = db.Column(db.String(20), default='member')
    personal_balance = db.Column(db.Float, default=0.0)
    active_team_id = db.Column(db.Integer, nullable=True)


class PoolLedger(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('team.id'), nullable=True)
    tx_type = db.Column(db.String(20), nullable=False)  # 'opening', 'credit', 'debit'
    amount = db.Column(db.Float, nullable=False)
    category = db.Column(db.String(100), nullable=False)
    remarks = db.Column(db.String(255), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    user = db.relationship('User', backref='pool_transactions')


class BillClaim(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('team.id'), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    category = db.Column(db.String(100), nullable=False)
    remarks = db.Column(db.String(255), nullable=False)
    vendor_vpa = db.Column(db.String(120), nullable=True)
    vendor_name = db.Column(db.String(120), nullable=True)
    urn = db.Column(db.String(64), nullable=True)
    status = db.Column(db.String(20), default='pending')  # 'pending', 'approved', 'rejected'
    paid_status = db.Column(db.String(20), default='unpaid')  # 'unpaid', 'paid'
    created_at = db.Column(db.DateTime, default=datetime.now)
    user = db.relationship('User', backref='claims')


class PersonalTransaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('team.id'), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    tx_type = db.Column(db.String(20), nullable=False)  # 'credit' or 'debit'
    amount = db.Column(db.Float, nullable=False)
    category = db.Column(db.String(100), nullable=False)
    remarks = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)


class WalletAccount(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('team.id'), unique=True, nullable=False)
    balance = db.Column(db.Float, default=0.0)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)


class WalletTransaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('team.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    tx_type = db.Column(db.String(20), nullable=False)  # 'topup' or 'vendor_qr_pay'
    amount = db.Column(db.Float, nullable=False)
    vendor_vpa = db.Column(db.String(120), nullable=True)
    vendor_name = db.Column(db.String(120), nullable=True)
    category = db.Column(db.String(100), nullable=False)
    remarks = db.Column(db.String(255), nullable=True)
    gateway_ref_id = db.Column(db.String(120), unique=True, nullable=True)
    urn = db.Column(db.String(64), unique=True, nullable=True)
    status = db.Column(db.String(25), default='pending')
    created_at = db.Column(db.DateTime, default=datetime.now)
    user = db.relationship('User', backref='wallet_transactions')


class ChatMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('team.id'), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    message = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    user = db.relationship('User', backref='messages')


class FeedPost(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('team.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    tag = db.Column(db.String(40), default='Update')
    content = db.Column(db.Text, nullable=False)
    image_path = db.Column(db.String(255), nullable=True)
    poll_question = db.Column(db.String(255), nullable=True)
    shares_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.now)
    author = db.relationship('User', backref='feed_posts')
    likes = db.relationship('FeedLike', backref='post', cascade='all, delete-orphan')
    comments = db.relationship('FeedComment', backref='post', cascade='all, delete-orphan', order_by='FeedComment.created_at.asc()')
    poll_options = db.relationship('PollOption', backref='post', cascade='all, delete-orphan', order_by='PollOption.id.asc()')


class PollOption(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey('feed_post.id'), nullable=False)
    option_text = db.Column(db.String(160), nullable=False)
    votes = db.relationship('PollVote', backref='option', cascade='all, delete-orphan')


class PollVote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey('feed_post.id'), nullable=False)
    option_id = db.Column(db.Integer, db.ForeignKey('poll_option.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)


class FeedLike(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey('feed_post.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)


class FeedComment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey('feed_post.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    comment = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    author = db.relationship('User', backref='feed_comments')


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey('team.id'), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    title = db.Column(db.String(140), nullable=False)
    body = db.Column(db.String(255), nullable=False)
    link_url = db.Column(db.String(255), default='/')
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.now)


class PushSubscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    endpoint = db.Column(db.Text, unique=True, nullable=False)
    subscription_json = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)


def get_or_create_wallet(team_id):
    wallet = WalletAccount.query.filter_by(team_id=team_id).first()
    if not wallet:
        wallet = WalletAccount(team_id=team_id, balance=0.0)
        db.session.add(wallet)
        db.session.flush()
    return wallet


def notify_users(team_id, recipient_user_ids, title, body, link_url='/'):
    unique_ids = set(recipient_user_ids)
    if not unique_ids:
        return

    for uid in unique_ids:
        db.session.add(Notification(
            team_id=team_id,
            user_id=uid,
            title=title,
            body=body,
            link_url=link_url
        ))

    if webpush and VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY:
        subs = PushSubscription.query.filter(PushSubscription.user_id.in_(unique_ids)).all()
        payload = json.dumps({'title': title, 'body': body, 'url': link_url})
        for sub in subs:
            try:
                webpush(
                    subscription_info=json.loads(sub.subscription_json),
                    data=payload,
                    vapid_private_key=VAPID_PRIVATE_KEY,
                    vapid_claims={'sub': VAPID_CLAIMS_EMAIL},
                    ttl=86400
                )
            except WebPushException as ex:
                status = getattr(getattr(ex, 'response', None), 'status_code', None)
                if status in (404, 410):
                    db.session.delete(sub)
            except Exception:
                pass


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


def ensure_schema_and_default_team():
    db.create_all()
    inspector = inspect(db.engine)

    table_columns = {
        'user': [
            ('active_team_id', 'INTEGER'),
            ('avatar_path', 'VARCHAR(255)'),
            ('is_super_admin', 'BOOLEAN DEFAULT FALSE'),
        ],
        'pool_ledger': [('team_id', 'INTEGER')],
        'bill_claim': [
            ('team_id', 'INTEGER'),
            ('vendor_vpa', 'VARCHAR(120)'),
            ('vendor_name', 'VARCHAR(120)'),
            ('urn', 'VARCHAR(64)'),
            ('paid_status', "VARCHAR(20) DEFAULT 'unpaid'"),
        ],
        'personal_transaction': [('team_id', 'INTEGER')],
        'chat_message': [('team_id', 'INTEGER')],
        'feed_post': [('image_path', 'VARCHAR(255)'), ('poll_question', 'VARCHAR(255)')],
        'wallet_transaction': [('urn', 'VARCHAR(64)')],
    }
    for tbl, cols in table_columns.items():
        if tbl in inspector.get_table_names():
            existing = {c['name'] for c in inspector.get_columns(tbl)}
            for col_name, col_type in cols:
                if col_name not in existing:
                    db.session.execute(text(f'ALTER TABLE "{tbl}" ADD COLUMN {col_name} {col_type}'))
    db.session.commit()

    if User.query.count() > 0 and not User.query.filter_by(is_super_admin=True).first():
        first_user = User.query.order_by(User.id.asc()).first()
        if first_user:
            first_user.is_super_admin = True
            db.session.commit()


def get_active_team_context():
    if not current_user.is_authenticated:
        return None, None
    memberships = TeamMembership.query.filter_by(user_id=current_user.id).all()
    if not memberships:
        if current_user.active_team_id is not None:
            current_user.active_team_id = None
            db.session.commit()
        return None, None

    active_m = next((m for m in memberships if m.team_id == current_user.active_team_id), memberships[0])
    if current_user.active_team_id != active_m.team_id:
        current_user.active_team_id = active_m.team_id
        db.session.commit()
    return active_m.team, active_m


@app.context_processor
def inject_globals():
    if current_user.is_authenticated:
        team, membership = get_active_team_context()
        all_memberships = TeamMembership.query.filter_by(user_id=current_user.id).all()
        is_super = bool(getattr(current_user, 'is_super_admin', False))
        recent_notifications = Notification.query.filter_by(user_id=current_user.id).order_by(Notification.created_at.desc()).limit(25).all()
        unread_notifications_count = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
        has_push_sub = PushSubscription.query.filter_by(user_id=current_user.id).count() > 0
        return {
            'active_team': team,
            'active_membership': membership,
            'user_teams': [m.team for m in all_memberships if m.team],
            'is_cashier': ((membership.role == 'cashier') if membership else False) or is_super,
            'is_super_admin': is_super,
            'global_categories': CATEGORIES,
            'global_expense_categories': EXPENSE_CATEGORIES,
            'recent_notifications': recent_notifications,
            'unread_notifications_count': unread_notifications_count,
            'vapid_public_key': VAPID_PUBLIC_KEY,
            'has_push_sub': has_push_sub,
            'cashfree_pg_enabled': bool(CF_PG_ID and CF_PG_SECRET),
        }
    return {
        'active_team': None,
        'active_membership': None,
        'user_teams': [],
        'is_cashier': False,
        'is_super_admin': False,
        'global_categories': CATEGORIES,
        'global_expense_categories': EXPENSE_CATEGORIES,
        'recent_notifications': [],
        'unread_notifications_count': 0,
        'vapid_public_key': VAPID_PUBLIC_KEY,
        'has_push_sub': False,
        'cashfree_pg_enabled': False,
    }


def team_and_cashier_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        team, membership = get_active_team_context()
        is_super = bool(getattr(current_user, 'is_super_admin', False))
        if is_super:
            return f(*args, **kwargs)
        if not team or not membership:
            flash("Create a team or open an invite link from your Profile first.", "warning")
            return redirect(url_for('profile'))
        if membership.role != 'cashier':
            flash("Only a Cashier of this team can perform this action.", "danger")
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated


def super_admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not getattr(current_user, 'is_super_admin', False):
            flash("Only the Super Admin can perform this action.", "danger")
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated


def _unique_invite_code(base_name):
    clean = ''.join(ch for ch in base_name.upper() if ch.isalnum())[:6] or "TEAM"
    code = f"{clean}_{uuid.uuid4().hex[:6].upper()}"
    while Team.query.filter_by(invite_code=code).first():
        code = f"{clean}_{uuid.uuid4().hex[:6].upper()}"
    return code


def _remove_member_from_team(team_id, user_id):
    membership = TeamMembership.query.filter_by(team_id=team_id, user_id=user_id).first()
    if not membership:
        return None
    team = membership.team
    db.session.delete(membership)
    db.session.flush()

    remaining = TeamMembership.query.filter_by(team_id=team_id).order_by(TeamMembership.id.asc()).all()
    if remaining and not any(m.role == 'cashier' for m in remaining):
        remaining[0].role = 'cashier'
        if remaining[0].user:
            remaining[0].user.role = 'cashier'
    elif not remaining and team:
        db.session.delete(team)

    user_obj = User.query.get(user_id)
    if user_obj and user_obj.active_team_id == team_id:
        next_m = TeamMembership.query.filter_by(user_id=user_id).first()
        user_obj.active_team_id = next_m.team_id if next_m else None
        user_obj.role = next_m.role if next_m else 'member'
    return team


# ---------------- PWA & PUSH ROUTES ----------------
@app.route('/manifest.json')
def manifest():
    return send_from_directory('static', 'manifest.json')


@app.route('/sw.js')
def service_worker():
    return send_from_directory('static', 'sw.js', mimetype='application/javascript')


@app.route('/push/subscribe', methods=['POST'])
@login_required
def push_subscribe():
    data = request.get_json(silent=True) or {}
    endpoint = data.get('endpoint')
    if not endpoint:
        return jsonify({'ok': False, 'error': 'Missing endpoint'}), 400

    existing = PushSubscription.query.filter_by(endpoint=endpoint).first()
    if existing:
        existing.user_id = current_user.id
        existing.subscription_json = json.dumps(data)
    else:
        db.session.add(PushSubscription(
            user_id=current_user.id,
            endpoint=endpoint,
            subscription_json=json.dumps(data)
        ))
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/notifications/read-all', methods=['POST'])
@login_required
def mark_all_notifications_read():
    Notification.query.filter_by(user_id=current_user.id, is_read=False).update({'is_read': True})
    db.session.commit()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'ok': True})
    return redirect(request.referrer or url_for('dashboard'))


# ---------------- AUTH ROUTES ----------------
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form['name'].strip()
        email = request.form['email'].strip().lower()
        password = request.form['password']

        if User.query.filter_by(email=email).first():
            flash("Email already registered.", "danger")
            return redirect(url_for('register'))

        is_first_user = (User.query.count() == 0)
        user = User(
            name=name,
            email=email,
            password_hash=generate_password_hash(password),
            is_super_admin=is_first_user,
            role='cashier' if is_first_user else 'member'
        )
        db.session.add(user)
        db.session.commit()
        if is_first_user:
            flash("Account created! You are registered as SUPER ADMIN.", "success")
        else:
            flash("Account created! Please sign in to continue.", "success")
        next_url = request.args.get('next')
        return redirect(url_for('login', next=next_url) if next_url else url_for('login'))

    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email'].strip().lower()
        password = request.form['password']
        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            next_url = request.args.get('next')
            if next_url and _is_safe_url(next_url):
                return redirect(next_url)
            team, _ = get_active_team_context()
            if not team:
                flash("Signed in! Create a team or open an invite link to join.", "info")
                return redirect(url_for('profile'))
            return redirect(url_for('dashboard'))
        flash("Invalid email or password.", "danger")
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


# ---------------- TEAM SWITCH / CREATE / INVITE-LINK JOIN / LEAVE ----------------
@app.route('/team/switch', methods=['POST'])
@login_required
def switch_team():
    team_id = int(request.form['team_id'])
    membership = TeamMembership.query.filter_by(user_id=current_user.id, team_id=team_id).first()
    if membership:
        current_user.active_team_id = team_id
        current_user.role = membership.role
        db.session.commit()
        flash(f"Switched workspace to '{membership.team.name}'.", "info")
    return redirect(request.referrer or url_for('dashboard'))


@app.route('/team/create', methods=['POST'])
@login_required
def create_team():
    name = request.form['team_name'].strip()
    city = request.form.get('team_city', '').strip()
    if not name:
        flash("Please enter a team name.", "warning")
        return redirect(url_for('profile'))

    team = Team(
        name=name,
        city=city,
        invite_code=_unique_invite_code(name),
        created_by=current_user.id
    )
    db.session.add(team)
    db.session.flush()

    db.session.add(TeamMembership(
        team_id=team.id,
        user_id=current_user.id,
        role='cashier',
        personal_balance=0.0
    ))
    get_or_create_wallet(team.id)
    current_user.active_team_id = team.id
    current_user.role = 'cashier'
    db.session.commit()
    flash(f"Workspace '{team.name}' created! Share its invite link from Profile.", "success")
    return redirect(url_for('dashboard'))


@app.route('/join/<code>', methods=['GET', 'POST'])
@login_required
def join_via_invite_link(code):
    team = Team.query.filter_by(invite_code=code.strip()).first()
    if not team:
        flash("Invalid or expired team invite link.", "danger")
        return redirect(url_for('profile'))

    existing = TeamMembership.query.filter_by(team_id=team.id, user_id=current_user.id).first()
    if not existing:
        db.session.add(TeamMembership(
            team_id=team.id,
            user_id=current_user.id,
            role='member',
            personal_balance=0.0
        ))
        other_ids = [m.user_id for m in TeamMembership.query.filter_by(team_id=team.id).all() if m.user_id != current_user.id]
        notify_users(
            team.id,
            other_ids,
            f"New Member Joined • {team.name}",
            f"{current_user.name} joined the workspace.",
            url_for('profile')
        )
    current_user.active_team_id = team.id
    db.session.commit()
    flash(f"Joined '{team.name}' via invite link!", "success")
    return redirect(url_for('dashboard'))


@app.route('/team/leave', methods=['POST'])
@login_required
def leave_team():
    team_id = int(request.form.get('team_id') or (current_user.active_team_id or 0))
    confirm_step_1 = request.form.get('confirm_step_1') == 'yes'
    confirm_step_2 = request.form.get('confirm_step_2') == 'yes'
    reason = request.form.get('reason', '').strip()

    if not (confirm_step_1 and confirm_step_2):
        flash("Please complete both confirmation steps to leave the team.", "warning")
        return redirect(url_for('profile'))

    membership = TeamMembership.query.filter_by(user_id=current_user.id, team_id=team_id).first()
    if not membership:
        flash("You are not a member of that team.", "warning")
        return redirect(url_for('profile'))

    team = membership.team
    all_members = TeamMembership.query.filter_by(team_id=team_id).all()

    if len(all_members) <= 1:
        _remove_member_from_team(team_id, current_user.id)
        db.session.commit()
        flash(f"You left '{team.name}'.", "info")
        return redirect(url_for('profile'))

    if membership.role == 'cashier':
        other_cashiers = [m for m in all_members if m.role == 'cashier' and m.user_id != current_user.id]
        if not other_cashiers:
            flash("You are the only Cashier. Add a Co-Cashier or transfer the Cashier role first before leaving.", "warning")
            return redirect(url_for('profile'))
        _remove_member_from_team(team_id, current_user.id)
        db.session.commit()
        flash(f"You left '{team.name}'.", "info")
        return redirect(url_for('profile'))

    existing_req = LeaveRequest.query.filter_by(team_id=team_id, user_id=current_user.id, status='pending').first()
    if existing_req:
        flash("Your exit request is already pending Cashier approval.", "info")
        return redirect(url_for('profile'))

    db.session.add(LeaveRequest(
        team_id=team_id,
        user_id=current_user.id,
        reason=reason or "Requested to leave team"
    ))
    cashier_ids = [m.user_id for m in all_members if m.role == 'cashier']
    notify_users(
        team_id,
        cashier_ids,
        f"Exit Request • {team.name}",
        f"{current_user.name} requested to leave the team.",
        url_for('dashboard')
    )
    db.session.commit()
    flash(f"Exit request submitted for '{team.name}'. Awaiting Cashier approval.", "info")
    return redirect(url_for('profile'))


@app.route('/team/leave-requests/<int:req_id>/<action>', methods=['POST'])
@login_required
@team_and_cashier_required
def process_leave_request(req_id, action):
    team, _ = get_active_team_context()
    leave_req = LeaveRequest.query.filter_by(id=req_id, team_id=team.id).first_or_404()
    if leave_req.status != 'pending':
        flash("Leave request already processed.", "warning")
        return redirect(url_for('dashboard'))

    if action == 'approve':
        leave_req.status = 'approved'
        _remove_member_from_team(team.id, leave_req.user_id)
        flash(f"Approved exit request for {leave_req.user.name}.", "success")
    elif action == 'reject':
        leave_req.status = 'rejected'
        notify_users(
            team.id,
            [leave_req.user_id],
            f"Exit Request Update • {team.name}",
            "Your request to leave the team was declined by the Cashier.",
            url_for('profile')
        )
        flash(f"Rejected exit request for {leave_req.user.name}.", "secondary")

    db.session.commit()
    return redirect(url_for('dashboard'))


# ---------------- CASHFREE PG TOP-UP & SCAN-PAY -> AUTO-CLAIM -> CASHIER PAY ----------------
@app.route('/wallet/pg/create-order', methods=['POST'])
@login_required
@team_and_cashier_required
def create_cashfree_topup_order():
    team, _ = get_active_team_context()
    data = request.get_json(silent=True) or request.form
    try:
        amount = round(float(data.get('amount') or 0), 2)
    except (TypeError, ValueError):
        amount = 0.0

    if amount < 1:
        return jsonify({'ok': False, 'error': 'Minimum top-up amount is ₹1.00'}), 400

    if not CF_PG_ID or not CF_PG_SECRET:
        return jsonify({'ok': False, 'error': 'Cashfree PG credentials are not configured yet.'}), 400

    order_id = f"TP_{team.id}_{uuid.uuid4().hex[:12].upper()}"
    db.session.add(WalletTransaction(
        team_id=team.id,
        user_id=current_user.id,
        tx_type='topup',
        amount=amount,
        category='Wallet Top-Up',
        remarks='Cashfree PG Collection',
        gateway_ref_id=order_id,
        status='pending'
    ))
    db.session.commit()

    payload = {
        "order_id": order_id,
        "order_amount": amount,
        "order_currency": "INR",
        "customer_details": {
            "customer_id": f"user_{current_user.id}",
            "customer_name": current_user.name,
            "customer_email": current_user.email,
            "customer_phone": "9999999999"
        },
        "order_meta": {
            "return_url": url_for('dashboard', _external=True) + f"?order_id={order_id}",
            "notify_url": url_for('cashfree_pg_webhook', _external=True)
        }
    }
    headers = {
        "x-api-version": "2023-08-01",
        "x-client-id": CF_PG_ID,
        "x-client-secret": CF_PG_SECRET,
        "Content-Type": "application/json"
    }

    try:
        resp = requests.post(f"{CF_PG_BASE}/orders", json=payload, headers=headers, timeout=15)
        res_json = resp.json()
    except Exception as exc:
        return jsonify({'ok': False, 'error': f'Gateway request failed: {exc}'}), 502

    if resp.status_code >= 400 or not res_json.get('payment_session_id'):
        return jsonify({'ok': False, 'error': res_json.get('message', 'Cashfree order creation failed')}), 400

    return jsonify({
        'ok': True,
        'order_id': order_id,
        'payment_session_id': res_json['payment_session_id'],
        'env': 'sandbox' if CASHFREE_ENV == 'SANDBOX' else 'production'
    })


@app.route('/webhooks/cashfree/pg', methods=['POST'])
def cashfree_pg_webhook():
    raw_body = request.data.decode('utf-8')
    timestamp = request.headers.get('x-webhook-timestamp', '')
    signature = request.headers.get('x-webhook-signature', '')

    if CF_PG_SECRET:
        expected_sig = base64.b64encode(
            hmac.new(CF_PG_SECRET.encode('utf-8'), (timestamp + raw_body).encode('utf-8'), hashlib.sha256).digest()
        ).decode('utf-8')
        if not hmac.compare_digest(expected_sig, signature):
            return jsonify({'error': 'Invalid signature'}), 401

    event = json.loads(raw_body or '{}')
    if event.get('type') == 'PAYMENT_SUCCESS_WEBHOOK':
        order_data = event.get('data', {}).get('order', {})
        payment_data = event.get('data', {}).get('payment', {})
        order_id = order_data.get('order_id')

        tx = WalletTransaction.query.filter_by(gateway_ref_id=order_id, tx_type='topup').first()
        if tx and tx.status != 'success':
            tx.status = 'success'
            tx.urn = payment_data.get('bank_reference') or str(payment_data.get('cf_payment_id', ''))
            wallet = get_or_create_wallet(tx.team_id)
            wallet.balance = round((wallet.balance or 0.0) + tx.amount, 2)
            db.session.add(PoolLedger(
                team_id=tx.team_id,
                tx_type='credit',
                amount=tx.amount,
                category='Monthly Deposit',
                remarks=f"Cashfree PG Top-Up • Order: {order_id}",
                user_id=tx.user_id
            ))
            db.session.commit()

    return jsonify({'status': 'ok'}), 200


@app.route('/wallet/scan-pay/pay-and-claim', methods=['POST'])
@login_required
def scan_pay_and_auto_claim():
    team, _ = get_active_team_context()
    if not team:
        return jsonify({'ok': False, 'error': 'Join or create a team workspace first.'}), 400

    vendor_vpa = (request.form.get('vendor_vpa') or '').strip()
    vendor_name = (request.form.get('vendor_name') or 'Shop Vendor').strip()
    material = (request.form.get('material') or '').strip()
    optional_msg = (request.form.get('remarks') or '').strip()
    raw_category = (request.form.get('category') or 'Vegetables').strip()
    custom_cat = (request.form.get('custom_category') or '').strip()

    try:
        amount = round(float(request.form.get('amount') or 0), 2)
    except (TypeError, ValueError):
        amount = 0.0

    if '@' not in vendor_vpa or amount <= 0 or not material:
        return jsonify({'ok': False, 'error': 'Scan/enter a valid UPI ID, what you are paying for, and a valid amount.'}), 400

    category, _ = _format_category_and_remarks(raw_category, custom_cat, '')
    urn = _build_rich_urn(current_user.name, material, prefix="RM")

    parts = [f"Material: {material}", f"Shop: {vendor_name} ({vendor_vpa})"]
    if optional_msg:
        parts.append(f"Msg: {optional_msg}")
    full_remarks = f"[URN: {urn}] " + " • ".join(parts)

    db.session.add(WalletTransaction(
        team_id=team.id,
        user_id=current_user.id,
        tx_type='vendor_qr_pay',
        amount=amount,
        vendor_vpa=vendor_vpa,
        vendor_name=vendor_name,
        category=category,
        remarks=full_remarks,
        urn=urn,
        status='pending'
    ))

    claim = BillClaim(
        team_id=team.id,
        user_id=current_user.id,
        amount=amount,
        category=category,
        remarks=full_remarks,
        vendor_vpa=vendor_vpa,
        vendor_name=vendor_name,
        urn=urn,
        status='pending',
        paid_status='unpaid'
    )
    db.session.add(claim)

    cashier_ids = [
        m.user_id for m in TeamMembership.query.filter_by(team_id=team.id, role='cashier').all()
        if m.user_id != current_user.id
    ]
    notify_users(
        team.id,
        cashier_ids,
        f"Auto-Bill ({urn}) • {team.name}",
        f"{current_user.name} paying ₹{amount:.2f} for {material} at {vendor_name}.",
        url_for('dashboard')
    )
    db.session.commit()

    upi_url = (
        f"upi://pay?pa={quote(vendor_vpa)}"
        f"&pn={quote(vendor_name)}"
        f"&am={amount:.2f}&cu=INR&tn={quote(urn)}"
    )

    return jsonify({
        'ok': True,
        'urn': urn,
        'claim_id': claim.id,
        'vendor_vpa': vendor_vpa,
        'vendor_name': vendor_name,
        'amount': f"{amount:.2f}",
        'upi_url': upi_url,
        'message': f"URN {urn} generated & bill auto-submitted for Cashier approval."
    })


@app.route('/claims/<int:claim_id>/approve-and-pay', methods=['POST'])
@login_required
@team_and_cashier_required
def approve_and_record_claim_payment(claim_id):
    claim = BillClaim.query.get_or_404(claim_id)
    team_name = claim.team.name if getattr(claim, 'team', None) else 'TEAM POOL'
    payment_method = (request.form.get('payment_method') or 'UPI App').strip()
    urn = claim.urn or _extract_urn_from_remarks(claim.remarks) or _build_rich_urn(
        claim.user.name if claim.user else 'USER',
        claim.category,
        prefix="CSH"
    )
    claim.urn = urn

    if claim.status != 'approved':
        claim.status = 'approved'
        is_deposit = (claim.category == "Monthly Deposit")
        ledger_type = 'credit' if is_deposit else 'debit'

        db.session.add(PoolLedger(
            team_id=claim.team_id,
            tx_type=ledger_type,
            amount=claim.amount,
            category=claim.category,
            remarks=f"[URN: {urn}] Approved & Paid ({payment_method}) [{claim.user.name}]: {claim.remarks}",
            user_id=claim.user_id
        ))

        tm = TeamMembership.query.filter_by(team_id=claim.team_id, user_id=claim.user_id).first()
        if tm:
            if is_deposit:
                tm.personal_balance += claim.amount
            else:
                tm.personal_balance -= claim.amount

        db.session.add(PersonalTransaction(
            team_id=claim.team_id,
            user_id=claim.user_id,
            tx_type=ledger_type,
            amount=claim.amount,
            category=claim.category,
            remarks=f"[URN: {urn}] Paid via {payment_method}: {claim.remarks}"
        ))

    claim.paid_status = 'paid'
    wtx = WalletTransaction.query.filter_by(urn=urn).first()
    if wtx:
        wtx.status = 'success'

    notify_users(
        claim.team_id,
        [claim.user_id],
        f"Bill Approved & Paid • {team_name}",
        f"₹{claim.amount:.2f} ({claim.category}) settled via {payment_method} • URN: {urn}",
        url_for('my_transactions')
    )
    db.session.commit()
    flash(f"Bill approved & recorded as paid via {payment_method} • URN: {urn}", "success")
    return redirect(url_for('dashboard'))


# ---------------- MAIN TABS ----------------
@app.route('/')
@login_required
def dashboard():
    team, membership = get_active_team_context()
    if not team:
        return redirect(url_for('profile'))

    now = datetime.now()
    month_start = datetime(now.year, now.month, 1)
    current_month_label = now.strftime('%B %Y')

    all_entries = PoolLedger.query.filter_by(team_id=team.id).all()

    prior_entries = [e for e in all_entries if e.created_at < month_start]
    prior_credits = sum(e.amount for e in prior_entries if e.tx_type in ['opening', 'credit'])
    prior_debits = sum(e.amount for e in prior_entries if e.tx_type == 'debit')
    carry_forward_opening = prior_credits - prior_debits

    month_entries = [e for e in all_entries if e.created_at >= month_start]
    explicit_month_opening = sum(e.amount for e in month_entries if e.tx_type == 'opening')
    month_credits = sum(e.amount for e in month_entries if e.tx_type == 'credit')
    month_expenses = sum(e.amount for e in month_entries if e.tx_type == 'debit')

    month_opening = carry_forward_opening + explicit_month_opening
    available_balance = month_opening + month_credits - month_expenses

    wallet = get_or_create_wallet(team.id)
    wallet_balance = round(wallet.balance or 0.0, 2)

    category_totals = {cat: 0.0 for cat in EXPENSE_CATEGORIES}
    for e in month_entries:
        if e.tx_type == 'debit':
            if e.category in category_totals:
                category_totals[e.category] += e.amount
            else:
                category_totals["Others (Specify)"] += e.amount

    month_bills = BillClaim.query.filter(
        BillClaim.team_id == team.id,
        BillClaim.created_at >= month_start
    ).order_by(BillClaim.created_at.desc()).all()

    pending_claims = BillClaim.query.filter_by(
        team_id=team.id,
        status='pending'
    ).order_by(BillClaim.created_at.desc()).all()

    approved_unpaid_claims = BillClaim.query.filter_by(
        team_id=team.id,
        status='approved',
        paid_status='unpaid'
    ).order_by(BillClaim.created_at.desc()).all()

    pending_credits = sum(c.amount for c in pending_claims if c.category == "Monthly Deposit")
    pending_debits = sum(c.amount for c in pending_claims if c.category != "Monthly Deposit")
    pending_amount = pending_credits + pending_debits
    projected_balance = available_balance + pending_credits - pending_debits

    pending_by_user_map = {}
    for c in pending_claims:
        uid = c.user_id
        is_dep = (c.category == "Monthly Deposit")
        if uid not in pending_by_user_map:
            pending_by_user_map[uid] = {
                'user_id': uid,
                'user_name': c.user.name if c.user else 'Unknown',
                'total_amount': 0.0,
                'credit_amount': 0.0,
                'debit_amount': 0.0,
                'count': 0,
                'items': []
            }
        pending_by_user_map[uid]['total_amount'] += c.amount
        if is_dep:
            pending_by_user_map[uid]['credit_amount'] += c.amount
        else:
            pending_by_user_map[uid]['debit_amount'] += c.amount
        pending_by_user_map[uid]['count'] += 1
        pending_by_user_map[uid]['items'].append({
            'id': c.id,
            'category': c.category,
            'is_deposit': is_dep,
            'amount': c.amount,
            'remarks': c.remarks,
            'urn': c.urn or _extract_urn_from_remarks(c.remarks),
            'date': c.created_at.strftime('%d %b %Y, %I:%M %p')
        })
    pending_by_user = sorted(pending_by_user_map.values(), key=lambda x: x['total_amount'], reverse=True)

    pending_leave_requests = LeaveRequest.query.filter_by(team_id=team.id, status='pending').order_by(LeaveRequest.created_at.desc()).all()
    team_memberships = TeamMembership.query.filter_by(team_id=team.id).all()
    members = sorted([m.user for m in team_memberships if m.user], key=lambda u: u.name.lower())

    all_teams = Team.query.order_by(Team.created_at.desc()).all() if current_user.is_super_admin else []
    all_users = User.query.order_by(User.id.asc()).all() if current_user.is_super_admin else []
    all_ledger_entries = PoolLedger.query.order_by(PoolLedger.created_at.desc()).limit(50).all() if current_user.is_super_admin else []
    all_claims = BillClaim.query.order_by(BillClaim.created_at.desc()).limit(50).all() if current_user.is_super_admin else []

    return render_template(
        'dashboard.html',
        current_month_label=current_month_label,
        available_balance=available_balance,
        carry_forward_opening=carry_forward_opening,
        month_opening=month_opening,
        month_credits=month_credits,
        month_expenses=month_expenses,
        wallet_balance=wallet_balance,
        category_totals=category_totals,
        categories=CATEGORIES,
        expense_categories=EXPENSE_CATEGORIES,
        pending_claims=pending_claims,
        approved_unpaid_claims=approved_unpaid_claims,
        pending_amount=pending_amount,
        pending_credits=pending_credits,
        pending_debits=pending_debits,
        projected_balance=projected_balance,
        pending_by_user=pending_by_user,
        month_bills=month_bills,
        pending_leave_requests=pending_leave_requests,
        members=members,
        team_memberships=team_memberships,
        all_teams=all_teams,
        all_users=all_users,
        all_ledger_entries=all_ledger_entries,
        all_claims=all_claims
    )


@app.route('/cashier/transaction', methods=['POST'])
@login_required
@team_and_cashier_required
def cashier_transaction():
    team, _ = get_active_team_context()
    tx_type = request.form['tx_type']
    amount = float(request.form['amount'])
    category, remarks = _format_category_and_remarks(
        request.form['category'],
        request.form.get('custom_category', ''),
        request.form.get('remarks', '')
    )
    target_user_id = request.form.get('user_id') or None

    if amount <= 0:
        flash("Amount must be greater than 0.", "warning")
        return redirect(url_for('dashboard'))

    if target_user_id:
        target_user_id = int(target_user_id)

    entry = PoolLedger(
        team_id=team.id,
        tx_type=tx_type,
        amount=amount,
        category=category,
        remarks=remarks,
        user_id=target_user_id if target_user_id else current_user.id
    )
    db.session.add(entry)

    if target_user_id:
        tm = TeamMembership.query.filter_by(team_id=team.id, user_id=target_user_id).first()
        if tm:
            p_type = 'credit' if tx_type in ['opening', 'credit'] else 'debit'
            if p_type == 'credit':
                tm.personal_balance += amount
            else:
                tm.personal_balance -= amount
            db.session.add(PersonalTransaction(
                team_id=team.id,
                user_id=target_user_id,
                tx_type=p_type,
                amount=amount,
                category=category,
                remarks=f"[Cashier Entry] {remarks}"
            ))

    other_members = [m.user_id for m in TeamMembership.query.filter_by(team_id=team.id).all() if m.user_id != current_user.id]
    notify_users(
        team.id,
        other_members,
        f"New Ledger Entry • {team.name}",
        f"{tx_type.upper()}: ₹{amount:.2f} ({category}) — {remarks}",
        url_for('all_transactions')
    )

    db.session.commit()
    flash("Ledger entry recorded successfully.", "success")
    return redirect(url_for('dashboard'))


@app.route('/claims/submit', methods=['POST'])
@login_required
def submit_claim():
    team, _ = get_active_team_context()
    if not team:
        flash("Create a team or join via an invite link first.", "warning")
        return redirect(url_for('profile'))

    amount = float(request.form['amount'])
    category, remarks = _format_category_and_remarks(
        request.form['category'],
        request.form.get('custom_category', ''),
        request.form.get('remarks', '')
    )

    if amount <= 0:
        flash("Amount must be greater than 0.", "warning")
        return redirect(url_for('dashboard'))

    urn = _build_rich_urn(current_user.name, category, prefix="CLM")
    formatted_remarks = f"[URN: {urn}] {remarks or category}"

    claim = BillClaim(
        team_id=team.id,
        user_id=current_user.id,
        amount=amount,
        category=category,
        remarks=formatted_remarks,
        urn=urn,
        status='pending',
        paid_status='unpaid'
    )
    db.session.add(claim)

    cashier_ids = [
        m.user_id for m in TeamMembership.query.filter_by(team_id=team.id, role='cashier').all()
        if m.user_id != current_user.id
    ]
    notify_users(
        team.id,
        cashier_ids,
        f"New Bill Claim ({urn}) • {team.name}",
        f"{current_user.name} submitted ₹{amount:.2f} for {category}.",
        url_for('dashboard')
    )

    db.session.commit()
    flash(f"Submitted to Cashier for approval • URN: {urn}", "info")
    return redirect(url_for('dashboard'))


@app.route('/claims/<int:claim_id>/<action>', methods=['POST'])
@login_required
@team_and_cashier_required
def process_claim(claim_id, action):
    claim = BillClaim.query.get_or_404(claim_id)
    if claim.status != 'pending':
        flash("Claim already processed.", "warning")
        return redirect(url_for('dashboard'))

    team_name = claim.team.name if getattr(claim, 'team', None) else 'TEAM POOL'
    approval_time = datetime.now()
    urn = claim.urn or _extract_urn_from_remarks(claim.remarks) or _build_rich_urn(
        claim.user.name if claim.user else 'USER',
        claim.category,
        prefix="CLM"
    )
    claim.urn = urn

    if action == 'approve':
        claim.status = 'approved'
        is_deposit = (claim.category == "Monthly Deposit")
        ledger_type = 'credit' if is_deposit else 'debit'

        pool_entry = PoolLedger(
            team_id=claim.team_id,
            tx_type=ledger_type,
            amount=claim.amount,
            category=claim.category,
            remarks=f"[URN: {urn}] Approved ({ledger_type.upper()}) [{claim.user.name}]: {claim.remarks}",
            user_id=claim.user_id,
            created_at=approval_time
        )
        db.session.add(pool_entry)

        tm = TeamMembership.query.filter_by(team_id=claim.team_id, user_id=claim.user_id).first()
        if tm:
            if is_deposit:
                tm.personal_balance += claim.amount
            else:
                tm.personal_balance -= claim.amount

        personal_entry = PersonalTransaction(
            team_id=claim.team_id,
            user_id=claim.user_id,
            tx_type=ledger_type,
            amount=claim.amount,
            category=claim.category,
            remarks=f"[URN: {urn}] Approved ({ledger_type.upper()}): {claim.remarks}",
            created_at=approval_time
        )
        db.session.add(personal_entry)

        notify_users(
            claim.team_id,
            [claim.user_id],
            f"Bill Approved • {team_name}",
            f"Your ₹{claim.amount:.2f} ({claim.category}) was approved • URN: {urn}",
            url_for('my_transactions')
        )
        flash(f"Approved ₹{claim.amount:.2f} • URN: {urn}. Tap 'Pay' to settle with this URN.", "success")

    elif action == 'reject':
        claim.status = 'rejected'
        notify_users(
            claim.team_id,
            [claim.user_id],
            f"Bill Rejected • {team_name}",
            f"Your ₹{claim.amount:.2f} ({claim.category}) claim was rejected.",
            url_for('dashboard')
        )
        flash("Bill claim rejected.", "secondary")

    db.session.commit()
    return redirect(url_for('dashboard'))


@app.route('/super-admin/delete-ledger/<int:entry_id>', methods=['POST'])
@login_required
@super_admin_required
def super_admin_delete_ledger(entry_id):
    entry = PoolLedger.query.get_or_404(entry_id)
    db.session.delete(entry)
    db.session.commit()
    flash("Ledger transaction deleted by Super Admin.", "info")
    return redirect(request.referrer or url_for('dashboard'))


@app.route('/super-admin/delete-claim/<int:claim_id>', methods=['POST'])
@login_required
@super_admin_required
def super_admin_delete_claim(claim_id):
    claim = BillClaim.query.get_or_404(claim_id)
    db.session.delete(claim)
    db.session.commit()
    flash("Bill claim deleted by Super Admin.", "info")
    return redirect(request.referrer or url_for('dashboard'))


@app.route('/my-transactions')
@login_required
def my_transactions():
    team, membership = get_active_team_context()
    if not team:
        return redirect(url_for('profile'))

    selected_user_id = current_user.id
    if (membership.role == 'cashier' or current_user.is_super_admin) and request.args.get('user_id'):
        selected_user_id = int(request.args.get('user_id'))

    viewed_user = User.query.get_or_404(selected_user_id)
    viewed_membership = TeamMembership.query.filter_by(team_id=team.id, user_id=viewed_user.id).first()

    txs = PersonalTransaction.query.filter_by(
        team_id=team.id,
        user_id=viewed_user.id
    ).order_by(PersonalTransaction.created_at.desc()).all()

    computed_balance = sum(t.amount if t.tx_type == 'credit' else -t.amount for t in txs)
    if viewed_membership and round(viewed_membership.personal_balance, 2) != round(computed_balance, 2):
        viewed_membership.personal_balance = computed_balance
        db.session.commit()

    team_memberships = TeamMembership.query.filter_by(team_id=team.id).all() if (membership.role == 'cashier' or current_user.is_super_admin) else []
    members = [m.user for m in team_memberships if m.user]

    return render_template(
        'my_transactions.html',
        viewed_user=viewed_user,
        viewed_balance=computed_balance,
        transactions=txs,
        members=members
    )


@app.route('/my-transactions/delete/<int:tx_id>', methods=['POST'])
@login_required
def delete_personal_transaction(tx_id):
    team, _ = get_active_team_context()
    if not team:
        return redirect(url_for('profile'))

    tx = PersonalTransaction.query.filter_by(id=tx_id, team_id=team.id).first_or_404()

    if tx.user_id != current_user.id and not current_user.is_super_admin:
        flash("You can only delete your own individual transactions.", "danger")
        return redirect(url_for('my_transactions'))

    tm = TeamMembership.query.filter_by(team_id=team.id, user_id=tx.user_id).first()
    if tm:
        if tx.tx_type == 'credit':
            tm.personal_balance -= tx.amount
        else:
            tm.personal_balance += tx.amount

    db.session.delete(tx)
    db.session.commit()
    flash("Individual transaction deleted.", "info")
    return redirect(url_for('my_transactions', user_id=tx.user_id if tx.user_id != current_user.id else None))


@app.route('/all-transactions')
@login_required
def all_transactions():
    team, _ = get_active_team_context()
    if not team:
        return redirect(url_for('profile'))

    person_filter = request.args.get('person', '')
    month_filter = request.args.get('month', '')
    category_filter = request.args.get('category', '')
    urn_filter = (request.args.get('urn') or '').strip()

    query = PoolLedger.query.filter_by(team_id=team.id)
    if person_filter:
        query = query.filter(PoolLedger.user_id == int(person_filter))
    if category_filter:
        query = query.filter(PoolLedger.category == category_filter)
    if urn_filter:
        query = query.filter(PoolLedger.remarks.ilike(f"%{urn_filter}%"))

    entries = query.order_by(PoolLedger.created_at.desc()).all()
    if month_filter:
        entries = [e for e in entries if e.created_at.strftime('%Y-%m') == month_filter]

    total_inflow = sum(e.amount for e in entries if e.tx_type in ['opening', 'credit'])
    total_outflow = sum(e.amount for e in entries if e.tx_type == 'debit')

    team_memberships = TeamMembership.query.filter_by(team_id=team.id).all()
    members = sorted([m.user for m in team_memberships if m.user], key=lambda u: u.name.lower())

    return render_template(
        'all_transactions.html',
        entries=entries,
        members=members,
        categories=CATEGORIES,
        person_filter=person_filter,
        month_filter=month_filter,
        category_filter=category_filter,
        urn_filter=urn_filter,
        total_inflow=total_inflow,
        total_outflow=total_outflow
    )


@app.route('/feed', methods=['GET', 'POST'])
@login_required
def feed():
    team, _ = get_active_team_context()
    if not team:
        return redirect(url_for('profile'))

    if request.method == 'POST':
        content = request.form.get('content', '').strip()
        tag = request.form.get('tag', 'Update').strip() or 'Update'
        poll_question = request.form.get('poll_question', '').strip()
        raw_options = [
            request.form.get('poll_opt_1', '').strip(),
            request.form.get('poll_opt_2', '').strip(),
            request.form.get('poll_opt_3', '').strip(),
            request.form.get('poll_opt_4', '').strip(),
        ]
        valid_options = [opt for opt in raw_options if opt]

        photo_file = request.files.get('photo')
        image_rel_path = _save_uploaded_image(photo_file) if photo_file and photo_file.filename else None

        if photo_file and photo_file.filename and not image_rel_path:
            flash("Unsupported image format. Use PNG, JPG, JPEG, GIF, or WEBP.", "warning")
            return redirect(url_for('feed'))

        if poll_question and len(valid_options) < 2:
            flash("Please provide at least 2 options for a poll.", "warning")
            return redirect(url_for('feed'))

        if content or image_rel_path or (poll_question and len(valid_options) >= 2):
            post = FeedPost(
                team_id=team.id,
                user_id=current_user.id,
                tag="Poll" if poll_question else tag,
                content=content or "",
                image_path=image_rel_path,
                poll_question=poll_question or None
            )
            db.session.add(post)
            db.session.flush()

            if poll_question and len(valid_options) >= 2:
                for opt_text in valid_options:
                    db.session.add(PollOption(post_id=post.id, option_text=opt_text))

            other_members = [m.user_id for m in TeamMembership.query.filter_by(team_id=team.id).all() if m.user_id != current_user.id]
            preview_text = poll_question or (content[:80] if content else "Shared a photo")
            notify_users(
                team.id,
                other_members,
                f"New Feed Post • {team.name}",
                f"{current_user.name}: {preview_text}",
                url_for('feed') + f"#post-{post.id}"
            )

            db.session.commit()
            flash("Published to team feed!", "success")
        return redirect(url_for('feed'))

    posts = FeedPost.query.filter_by(team_id=team.id).order_by(FeedPost.created_at.desc()).all()
    liked_post_ids = {lk.post_id for lk in FeedLike.query.filter_by(user_id=current_user.id).all()}
    user_votes = {v.post_id: v.option_id for v in PollVote.query.filter_by(user_id=current_user.id).all()}
    return render_template('feed.html', posts=posts, liked_post_ids=liked_post_ids, user_votes=user_votes)


@app.route('/feed/<int:post_id>/vote', methods=['POST'])
@login_required
def vote_poll(post_id):
    team, _ = get_active_team_context()
    post = FeedPost.query.filter_by(id=post_id, team_id=team.id).first_or_404()
    option_id = int(request.form['option_id'])
    option = PollOption.query.filter_by(id=option_id, post_id=post.id).first_or_404()

    existing_vote = PollVote.query.filter_by(post_id=post.id, user_id=current_user.id).first()
    if existing_vote:
        existing_vote.option_id = option.id
    else:
        db.session.add(PollVote(post_id=post.id, option_id=option.id, user_id=current_user.id))
    db.session.commit()
    return redirect(url_for('feed') + f"#post-{post.id}")


@app.route('/feed/<int:post_id>/like', methods=['POST'])
@login_required
def toggle_like(post_id):
    team, _ = get_active_team_context()
    post = FeedPost.query.filter_by(id=post_id, team_id=team.id).first_or_404()
    existing = FeedLike.query.filter_by(post_id=post.id, user_id=current_user.id).first()
    if existing:
        db.session.delete(existing)
    else:
        db.session.add(FeedLike(post_id=post.id, user_id=current_user.id))
    db.session.commit()
    return redirect(url_for('feed') + f"#post-{post.id}")


@app.route('/feed/<int:post_id>/comment', methods=['POST'])
@login_required
def add_comment(post_id):
    team, _ = get_active_team_context()
    post = FeedPost.query.filter_by(id=post_id, team_id=team.id).first_or_404()
    comment_text = request.form.get('comment', '').strip()
    if comment_text:
        db.session.add(FeedComment(
            post_id=post.id,
            user_id=current_user.id,
            comment=comment_text
        ))
        if post.user_id != current_user.id:
            notify_users(
                team.id,
                [post.user_id],
                f"New Comment • {team.name}",
                f"{current_user.name} commented on your post: {comment_text[:80]}",
                url_for('feed') + f"#post-{post.id}"
            )
        db.session.commit()
    return redirect(url_for('feed') + f"#post-{post.id}")


@app.route('/feed/<int:post_id>/share', methods=['POST'])
@login_required
def track_share(post_id):
    team, _ = get_active_team_context()
    post = FeedPost.query.filter_by(id=post_id, team_id=team.id).first_or_404()
    post.shares_count = (post.shares_count or 0) + 1
    db.session.commit()
    return jsonify({'shares': post.shares_count})


@app.route('/feed/<int:post_id>/delete', methods=['POST'])
@login_required
def delete_feed_post(post_id):
    team, membership = get_active_team_context()
    post = FeedPost.query.filter_by(id=post_id, team_id=team.id).first_or_404()

    is_owner = (post.user_id == current_user.id)
    is_team_cashier = (membership and membership.role == 'cashier')
    is_super = bool(getattr(current_user, 'is_super_admin', False))

    if not (is_owner or is_team_cashier or is_super):
        flash("You do not have permission to delete this post.", "danger")
        return redirect(url_for('feed'))

    if post.image_path:
        full_img_path = os.path.join(BASE_DIR, 'static', post.image_path)
        if os.path.exists(full_img_path):
            try:
                os.remove(full_img_path)
            except OSError:
                pass

    db.session.delete(post)
    db.session.commit()
    flash("Post deleted from feed.", "info")
    return redirect(url_for('feed'))


@app.route('/chat', methods=['GET', 'POST'])
@login_required
def chat():
    team, _ = get_active_team_context()
    if not team:
        return redirect(url_for('profile'))

    if request.method == 'POST':
        msg_text = request.form.get('message', '').strip()
        if msg_text:
            db.session.add(ChatMessage(team_id=team.id, user_id=current_user.id, message=msg_text))
            db.session.commit()
        return redirect(url_for('chat'))

    messages = ChatMessage.query.filter_by(team_id=team.id).order_by(ChatMessage.created_at.asc()).limit(150).all()
    return render_template('chat.html', messages=messages)


@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    team, _ = get_active_team_context()
    if request.method == 'POST':
        current_user.name = request.form['name'].strip()
        new_pass = request.form.get('new_password', '').strip()
        if new_pass:
            current_user.password_hash = generate_password_hash(new_pass)

        avatar_file = request.files.get('avatar')
        if avatar_file and avatar_file.filename:
            saved_avatar = _save_uploaded_image(avatar_file)
            if saved_avatar:
                current_user.avatar_path = saved_avatar
            else:
                flash("Unsupported profile photo format. Use PNG, JPG, JPEG, GIF, or WEBP.", "warning")
                return redirect(url_for('profile'))

        db.session.commit()
        flash("Profile updated!", "success")
        return redirect(url_for('profile'))

    team_memberships = TeamMembership.query.filter_by(team_id=team.id).all() if team else []
    user_memberships = TeamMembership.query.filter_by(user_id=current_user.id).all()
    pending_my_leave_team_ids = {
        lr.team_id for lr in LeaveRequest.query.filter_by(user_id=current_user.id, status='pending').all()
    }
    all_teams = Team.query.order_by(Team.created_at.desc()).all() if current_user.is_super_admin else []
    all_users = User.query.order_by(User.id.asc()).all() if current_user.is_super_admin else []

    return render_template(
        'profile.html',
        team_memberships=team_memberships,
        user_memberships=user_memberships,
        pending_my_leave_team_ids=pending_my_leave_team_ids,
        all_teams=all_teams,
        all_users=all_users
    )


@app.route('/profile/cashier-role/<int:user_id>', methods=['POST'])
@login_required
@team_and_cashier_required
def manage_cashier_role(user_id):
    team, current_membership = get_active_team_context()
    target_membership = TeamMembership.query.filter_by(team_id=team.id, user_id=user_id).first_or_404()
    mode = request.form.get('mode', 'co_cashier')

    if mode == 'co_cashier':
        target_membership.role = 'cashier'
        target_membership.user.role = 'cashier'
        flash(f"{target_membership.user.name} is now also a Cashier for '{team.name}'.", "success")
    elif mode == 'transfer':
        if current_membership:
            current_membership.role = 'member'
        target_membership.role = 'cashier'
        current_user.role = 'member'
        target_membership.user.role = 'cashier'
        flash(f"Transferred Cashier role to {target_membership.user.name}.", "success")
    elif mode == 'remove_cashier':
        cashier_count = TeamMembership.query.filter_by(team_id=team.id, role='cashier').count()
        if cashier_count <= 1:
            flash("At least one Cashier is required for the team.", "warning")
        else:
            target_membership.role = 'member'
            target_membership.user.role = 'member'
            flash(f"{target_membership.user.name} is now a regular Member.", "info")

    db.session.commit()
    return redirect(request.referrer or url_for('profile'))


with app.app_context():
    ensure_schema_and_default_team()

if __name__ == '__main__':
    app.run(debug=True)
