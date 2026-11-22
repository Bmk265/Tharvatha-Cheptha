import os
import uuid
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from sqlalchemy import text, inspect

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change-this-secret-key-in-render')

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024  # 8 MB max upload

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
    "Monthly Deposit (Per Person)",
    "Expenses",
    "Room Rent",
    "Outside Shopping",
    "Outside Food"
]


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


# ---------------- MODELS ----------------
class Team(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    city = db.Column(db.String(100), nullable=True)
    invite_code = db.Column(db.String(24), unique=True, nullable=False)
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


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    avatar_path = db.Column(db.String(255), nullable=True)
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
    status = db.Column(db.String(20), default='pending')  # 'pending', 'approved', 'rejected'
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


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


def ensure_schema_and_default_team():
    db.create_all()
    inspector = inspect(db.engine)

    table_columns = {
        'user': [('active_team_id', 'INTEGER'), ('avatar_path', 'VARCHAR(255)')],
        'pool_ledger': [('team_id', 'INTEGER')],
        'bill_claim': [('team_id', 'INTEGER')],
        'personal_transaction': [('team_id', 'INTEGER')],
        'chat_message': [('team_id', 'INTEGER')],
        'feed_post': [('image_path', 'VARCHAR(255)'), ('poll_question', 'VARCHAR(255)')],
    }
    for tbl, cols in table_columns.items():
        if tbl in inspector.get_table_names():
            existing = {c['name'] for c in inspector.get_columns(tbl)}
            for col_name, col_type in cols:
                if col_name not in existing:
                    db.session.execute(text(f'ALTER TABLE "{tbl}" ADD COLUMN {col_name} {col_type}'))
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
        return {
            'active_team': team,
            'active_membership': membership,
            'user_teams': [m.team for m in all_memberships if m.team],
            'is_cashier': (membership.role == 'cashier') if membership else False
        }
    return {
        'active_team': None,
        'active_membership': None,
        'user_teams': [],
        'is_cashier': False
    }


def team_and_cashier_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        team, membership = get_active_team_context()
        if not team or not membership:
            flash("Join or create a team first from your Profile.", "warning")
            return redirect(url_for('profile'))
        if membership.role != 'cashier':
            flash("Only a Cashier of this team can perform this action.", "danger")
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated


def _unique_invite_code(base_name):
    clean = ''.join(ch for ch in base_name.upper() if ch.isalnum())[:6] or "TEAM"
    code = f"{clean}{datetime.now().strftime('%H%M')}"
    counter = 1
    while Team.query.filter_by(invite_code=code).first():
        code = f"{clean}{counter}"
        counter += 1
    return code


# ---------------- PWA ROUTES ----------------
@app.route('/manifest.json')
def manifest():
    return send_from_directory('static', 'manifest.json')


@app.route('/sw.js')
def service_worker():
    return send_from_directory('static', 'sw.js', mimetype='application/javascript')


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

        user = User(
            name=name,
            email=email,
            password_hash=generate_password_hash(password),
            role='member'
        )
        db.session.add(user)
        db.session.commit()
        flash("Account created! Please log in, then create or join a team.", "success")
        return redirect(url_for('login'))

    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email'].strip().lower()
        password = request.form['password']
        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            team, _ = get_active_team_context()
            if not team:
                flash("Logged in! Create or join a team from your Profile to get started.", "info")
                return redirect(url_for('profile'))
            return redirect(url_for('dashboard'))
        flash("Invalid email or password.", "danger")
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


# ---------------- TEAM SWITCH / CREATE / JOIN / LEAVE ----------------
@app.route('/team/switch', methods=['POST'])
@login_required
def switch_team():
    team_id = int(request.form['team_id'])
    membership = TeamMembership.query.filter_by(user_id=current_user.id, team_id=team_id).first()
    if membership:
        current_user.active_team_id = team_id
        current_user.role = membership.role
        db.session.commit()
        flash(f"Switched to team '{membership.team.name}'.", "info")
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
    current_user.active_team_id = team.id
    current_user.role = 'cashier'
    db.session.commit()
    flash(f"Created team '{team.name}' (Invite Code: {team.invite_code}). You are Cashier.", "success")
    return redirect(url_for('dashboard'))


@app.route('/team/join', methods=['POST'])
@login_required
def join_team():
    code = request.form['invite_code'].strip().upper()
    team = Team.query.filter_by(invite_code=code).first()
    if not team:
        flash("No team found with that invite code.", "danger")
        return redirect(url_for('profile'))

    existing = TeamMembership.query.filter_by(team_id=team.id, user_id=current_user.id).first()
    if not existing:
        db.session.add(TeamMembership(
            team_id=team.id,
            user_id=current_user.id,
            role='member',
            personal_balance=0.0
        ))
    current_user.active_team_id = team.id
    db.session.commit()
    flash(f"Joined team '{team.name}'!", "success")
    return redirect(url_for('dashboard'))


@app.route('/team/leave', methods=['POST'])
@login_required
def leave_team():
    team_id = int(request.form.get('team_id') or (current_user.active_team_id or 0))
    membership = TeamMembership.query.filter_by(user_id=current_user.id, team_id=team_id).first()
    if not membership:
        flash("You are not a member of that team.", "warning")
        return redirect(url_for('profile'))

    team = membership.team
    db.session.delete(membership)
    db.session.flush()

    remaining = TeamMembership.query.filter_by(team_id=team_id).order_by(TeamMembership.id.asc()).all()
    has_cashier = any(m.role == 'cashier' for m in remaining)
    if remaining and not has_cashier:
        remaining[0].role = 'cashier'
        if remaining[0].user:
            remaining[0].user.role = 'cashier'
    elif not remaining and team:
        db.session.delete(team)

    next_m = TeamMembership.query.filter_by(user_id=current_user.id).first()
    current_user.active_team_id = next_m.team_id if next_m else None
    current_user.role = next_m.role if next_m else 'member'
    db.session.commit()

    flash(f"Left team '{team.name}'.", "info")
    return redirect(url_for('profile'))


# ---------------- MAIN TABS ----------------
@app.route('/')
@login_required
def dashboard():
    team, membership = get_active_team_context()
    if not team:
        return redirect(url_for('profile'))

    now = datetime.now()
    month_start = datetime(now.year, now.month, 1)

    all_entries = PoolLedger.query.filter_by(team_id=team.id).all()
    total_credits = sum(e.amount for e in all_entries if e.tx_type in ['opening', 'credit'])
    total_debits = sum(e.amount for e in all_entries if e.tx_type == 'debit')
    available_balance = total_credits - total_debits

    month_entries = PoolLedger.query.filter(
        PoolLedger.team_id == team.id,
        PoolLedger.created_at >= month_start
    ).all()
    month_opening = sum(e.amount for e in month_entries if e.tx_type == 'opening')
    month_expenses = sum(e.amount for e in month_entries if e.tx_type == 'debit')

    category_totals = {cat: 0.0 for cat in CATEGORIES}
    for e in month_entries:
        if e.tx_type == 'debit' and e.category in category_totals:
            category_totals[e.category] += e.amount

    pending_claims = BillClaim.query.filter_by(team_id=team.id, status='pending').order_by(BillClaim.created_at.desc()).all()
    team_memberships = TeamMembership.query.filter_by(team_id=team.id).all()
    members = sorted([m.user for m in team_memberships if m.user], key=lambda u: u.name.lower())

    return render_template(
        'dashboard.html',
        available_balance=available_balance,
        month_opening=month_opening,
        month_expenses=month_expenses,
        category_totals=category_totals,
        categories=CATEGORIES,
        pending_claims=pending_claims,
        members=members,
        team_memberships=team_memberships
    )


@app.route('/cashier/transaction', methods=['POST'])
@login_required
@team_and_cashier_required
def cashier_transaction():
    team, _ = get_active_team_context()
    tx_type = request.form['tx_type']
    amount = float(request.form['amount'])
    category = request.form['category']
    remarks = request.form.get('remarks', '').strip()
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

    db.session.commit()
    flash("Pool balance updated successfully.", "success")
    return redirect(url_for('dashboard'))


@app.route('/claims/submit', methods=['POST'])
@login_required
def submit_claim():
    team, _ = get_active_team_context()
    if not team:
        flash("Join or create a team first.", "warning")
        return redirect(url_for('profile'))

    amount = float(request.form['amount'])
    category = request.form['category']
    remarks = request.form['remarks'].strip()

    if amount <= 0:
        flash("Amount must be greater than 0.", "warning")
        return redirect(url_for('dashboard'))

    claim = BillClaim(
        team_id=team.id,
        user_id=current_user.id,
        amount=amount,
        category=category,
        remarks=remarks
    )
    db.session.add(claim)
    db.session.commit()
    flash("Bill submitted to Cashier for approval.", "info")
    return redirect(url_for('dashboard'))


@app.route('/claims/<int:claim_id>/<action>', methods=['POST'])
@login_required
@team_and_cashier_required
def process_claim(claim_id, action):
    team, _ = get_active_team_context()
    claim = BillClaim.query.filter_by(id=claim_id, team_id=team.id).first_or_404()
    if claim.status != 'pending':
        flash("Claim already processed.", "warning")
        return redirect(url_for('dashboard'))

    if action == 'approve':
        claim.status = 'approved'
        is_deposit = (claim.category == "Monthly Deposit (Per Person)")
        ledger_type = 'credit' if is_deposit else 'debit'

        pool_entry = PoolLedger(
            team_id=team.id,
            tx_type=ledger_type,
            amount=claim.amount,
            category=claim.category,
            remarks=f"Approved ({ledger_type.upper()}) [{claim.user.name}]: {claim.remarks}",
            user_id=claim.user_id
        )
        db.session.add(pool_entry)

        tm = TeamMembership.query.filter_by(team_id=team.id, user_id=claim.user_id).first()
        if tm:
            if is_deposit:
                tm.personal_balance += claim.amount
            else:
                tm.personal_balance -= claim.amount

        personal_entry = PersonalTransaction(
            team_id=team.id,
            user_id=claim.user_id,
            tx_type=ledger_type,
            amount=claim.amount,
            category=claim.category,
            remarks=f"Bill Approved ({ledger_type.upper()}): {claim.remarks}"
        )
        db.session.add(personal_entry)
        flash(f"Approved ₹{claim.amount:.2f} as {ledger_type.upper()} for {claim.user.name}.", "success")

    elif action == 'reject':
        claim.status = 'rejected'
        flash("Bill claim rejected.", "secondary")

    db.session.commit()
    return redirect(url_for('dashboard'))


@app.route('/my-transactions')
@login_required
def my_transactions():
    team, membership = get_active_team_context()
    if not team:
        return redirect(url_for('profile'))

    selected_user_id = current_user.id
    if membership.role == 'cashier' and request.args.get('user_id'):
        selected_user_id = int(request.args.get('user_id'))

    viewed_user = User.query.get_or_404(selected_user_id)
    viewed_membership = TeamMembership.query.filter_by(team_id=team.id, user_id=viewed_user.id).first()
    viewed_balance = viewed_membership.personal_balance if viewed_membership else 0.0

    txs = PersonalTransaction.query.filter_by(
        team_id=team.id,
        user_id=viewed_user.id
    ).order_by(PersonalTransaction.created_at.desc()).all()

    team_memberships = TeamMembership.query.filter_by(team_id=team.id).all() if membership.role == 'cashier' else []
    members = [m.user for m in team_memberships if m.user]

    return render_template(
        'my_transactions.html',
        viewed_user=viewed_user,
        viewed_balance=viewed_balance,
        transactions=txs,
        members=members
    )


@app.route('/all-transactions')
@login_required
def all_transactions():
    team, _ = get_active_team_context()
    if not team:
        return redirect(url_for('profile'))

    person_filter = request.args.get('person', '')
    month_filter = request.args.get('month', '')
    category_filter = request.args.get('category', '')

    query = PoolLedger.query.filter_by(team_id=team.id)
    if person_filter:
        query = query.filter(PoolLedger.user_id == int(person_filter))
    if category_filter:
        query = query.filter(PoolLedger.category == category_filter)

    entries = query.order_by(PoolLedger.created_at.desc()).all()
    if month_filter:
        entries = [e for e in entries if e.created_at.strftime('%Y-%m') == month_filter]

    team_memberships = TeamMembership.query.filter_by(team_id=team.id).all()
    members = sorted([m.user for m in team_memberships if m.user], key=lambda u: u.name.lower())

    return render_template(
        'all_transactions.html',
        entries=entries,
        members=members,
        categories=CATEGORIES,
        person_filter=person_filter,
        month_filter=month_filter,
        category_filter=category_filter
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

            db.session.commit()
            flash("Posted to team feed!", "success")
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
    return render_template(
        'profile.html',
        team_memberships=team_memberships,
        user_memberships=user_memberships
    )


@app.route('/profile/cashier-role/<int:user_id>', methods=['POST'])
@login_required
@team_and_cashier_required
def manage_cashier_role(user_id):
    team, current_membership = get_active_team_context()
    target_membership = TeamMembership.query.filter_by(team_id=team.id, user_id=user_id).first_or_404()
    mode = request.form.get('mode', 'co_cashier')  # 'co_cashier', 'transfer', or 'remove_cashier'

    if mode == 'co_cashier':
        target_membership.role = 'cashier'
        target_membership.user.role = 'cashier'
        flash(f"{target_membership.user.name} is now also a Cashier for '{team.name}' (you remain Cashier too).", "success")
    elif mode == 'transfer':
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
