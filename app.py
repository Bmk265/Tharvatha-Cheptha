import os
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import text, inspect

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change-this-secret-key-in-render')

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
    team = db.relationship('Team', backref='memberships')
    user = db.relationship('User', backref='memberships')


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), default='member')  # legacy fallback
    personal_balance = db.Column(db.Float, default=0.0)  # legacy fallback
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
    shares_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.now)
    author = db.relationship('User', backref='feed_posts')
    likes = db.relationship('FeedLike', backref='post', cascade='all, delete-orphan')
    comments = db.relationship('FeedComment', backref='post', cascade='all, delete-orphan', order_by='FeedComment.created_at.asc()')


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

    # Safe column additions for existing deployments
    table_columns = {
        'user': [('active_team_id', 'INTEGER')],
        'pool_ledger': [('team_id', 'INTEGER')],
        'bill_claim': [('team_id', 'INTEGER')],
        'personal_transaction': [('team_id', 'INTEGER')],
        'chat_message': [('team_id', 'INTEGER')],
    }
    for tbl, cols in table_columns.items():
        if tbl in inspector.get_table_names():
            existing = {c['name'] for c in inspector.get_columns(tbl)}
            for col_name, col_type in cols:
                if col_name not in existing:
                    db.session.execute(text(f'ALTER TABLE "{tbl}" ADD COLUMN {col_name} {col_type}'))
    db.session.commit()

    # Ensure at least one team exists and attach existing users/records
    team = Team.query.first()
    if not team:
        first_user = User.query.order_by(User.id.asc()).first()
        team = Team(
            name="Main Room Team",
            city="",
            invite_code="ROOM101",
            created_by=first_user.id if first_user else None
        )
        db.session.add(team)
        db.session.commit()

    for model_cls in [PoolLedger, BillClaim, PersonalTransaction, ChatMessage]:
        model_cls.query.filter(model_cls.team_id.is_(None)).update({model_cls.team_id: team.id})

    users = User.query.all()
    for idx, u in enumerate(users):
        if not u.active_team_id:
            u.active_team_id = team.id
        membership = TeamMembership.query.filter_by(team_id=team.id, user_id=u.id).first()
        if not membership:
            role = u.role if u.role in ('cashier', 'member') else ('cashier' if idx == 0 else 'member')
            db.session.add(TeamMembership(
                team_id=team.id,
                user_id=u.id,
                role=role,
                personal_balance=u.personal_balance or 0.0
            ))
    db.session.commit()


def get_active_team_context():
    if not current_user.is_authenticated:
        return None, None
    memberships = TeamMembership.query.filter_by(user_id=current_user.id).all()
    if not memberships:
        team = Team.query.first()
        if not team:
            team = Team(name="My Team", invite_code=f"TEAM{current_user.id}01", created_by=current_user.id)
            db.session.add(team)
            db.session.commit()
        m = TeamMembership(team_id=team.id, user_id=current_user.id, role='cashier', personal_balance=0.0)
        db.session.add(m)
        current_user.active_team_id = team.id
        db.session.commit()
        return team, m

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


def cashier_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        _, membership = get_active_team_context()
        if not membership or membership.role != 'cashier':
            flash("Only this team's Cashier can perform this action.", "danger")
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
        team_mode = request.form.get('team_mode', 'create')  # 'create' or 'join'
        team_name = request.form.get('team_name', '').strip()
        team_city = request.form.get('team_city', '').strip()
        invite_code = request.form.get('invite_code', '').strip().upper()

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
        db.session.flush()

        if team_mode == 'join' and invite_code:
            target_team = Team.query.filter_by(invite_code=invite_code).first()
            if not target_team:
                db.session.rollback()
                flash("Invalid team invite code.", "danger")
                return redirect(url_for('register'))
            role = 'member'
        else:
            final_team_name = team_name or f"{name}'s Team"
            target_team = Team(
                name=final_team_name,
                city=team_city,
                invite_code=_unique_invite_code(final_team_name),
                created_by=user.id
            )
            db.session.add(target_team)
            db.session.flush()
            role = 'cashier'

        user.role = role
        user.active_team_id = target_team.id
        db.session.add(TeamMembership(
            team_id=target_team.id,
            user_id=user.id,
            role=role,
            personal_balance=0.0
        ))
        db.session.commit()
        flash(f"Welcome {name}! Joined '{target_team.name}' as {role.upper()}.", "success")
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
            get_active_team_context()
            return redirect(url_for('dashboard'))
        flash("Invalid email or password.", "danger")
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


# ---------------- TEAM SWITCH / CREATE / JOIN ----------------
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


# ---------------- MAIN TABS ----------------
@app.route('/')
@login_required
def dashboard():
    team, membership = get_active_team_context()
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
@cashier_required
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
@cashier_required
def process_claim(claim_id, action):
    team, _ = get_active_team_context()
    claim = BillClaim.query.filter_by(id=claim_id, team_id=team.id).first_or_404()
    if claim.status != 'pending':
        flash("Claim already processed.", "warning")
        return redirect(url_for('dashboard'))

    if action == 'approve':
        claim.status = 'approved'
        pool_debit = PoolLedger(
            team_id=team.id,
            tx_type='debit',
            amount=claim.amount,
            category=claim.category,
            remarks=f"Approved Bill ({claim.user.name}): {claim.remarks}",
            user_id=claim.user_id
        )
        db.session.add(pool_debit)

        tm = TeamMembership.query.filter_by(team_id=team.id, user_id=claim.user_id).first()
        if tm:
            tm.personal_balance += claim.amount

        personal_credit = PersonalTransaction(
            team_id=team.id,
            user_id=claim.user_id,
            tx_type='credit',
            amount=claim.amount,
            category=claim.category,
            remarks=f"Bill Approved: {claim.remarks}"
        )
        db.session.add(personal_credit)
        flash(f"Approved ₹{claim.amount:.2f} for {claim.user.name}.", "success")

    elif action == 'reject':
        claim.status = 'rejected'
        flash("Bill claim rejected.", "secondary")

    db.session.commit()
    return redirect(url_for('dashboard'))


@app.route('/my-transactions')
@login_required
def my_transactions():
    team, membership = get_active_team_context()
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
    if request.method == 'POST':
        content = request.form.get('content', '').strip()
        tag = request.form.get('tag', 'Update').strip() or 'Update'
        if content:
            db.session.add(FeedPost(
                team_id=team.id,
                user_id=current_user.id,
                tag=tag,
                content=content
            ))
            db.session.commit()
            flash("Posted to team feed!", "success")
        return redirect(url_for('feed'))

    posts = FeedPost.query.filter_by(team_id=team.id).order_by(FeedPost.created_at.desc()).all()
    liked_post_ids = {lk.post_id for lk in FeedLike.query.filter_by(user_id=current_user.id).all()}
    return render_template('feed.html', posts=posts, liked_post_ids=liked_post_ids)


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
    team, membership = get_active_team_context()
    if request.method == 'POST':
        current_user.name = request.form['name'].strip()
        new_pass = request.form.get('new_password', '').strip()
        if new_pass:
            current_user.password_hash = generate_password_hash(new_pass)
        db.session.commit()
        flash("Profile updated!", "success")
        return redirect(url_for('profile'))

    team_memberships = TeamMembership.query.filter_by(team_id=team.id).all()
    return render_template('profile.html', team_memberships=team_memberships)


@app.route('/profile/transfer-cashier/<int:user_id>', methods=['POST'])
@login_required
@cashier_required
def transfer_cashier(user_id):
    team, current_membership = get_active_team_context()
    target_membership = TeamMembership.query.filter_by(team_id=team.id, user_id=user_id).first_or_404()
    current_membership.role = 'member'
    target_membership.role = 'cashier'
    current_user.role = 'member'
    target_membership.user.role = 'cashier'
    db.session.commit()
    flash(f"{target_membership.user.name} is now Cashier for '{team.name}'.", "success")
    return redirect(url_for('profile'))


with app.app_context():
    ensure_schema_and_default_team()

if __name__ == '__main__':
    app.run(debug=True)
