import os
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change-this-secret-key-in-render')

# Automatically use Render PostgreSQL if DATABASE_URL is set, otherwise SQLite
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
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), default='member')  # 'cashier' or 'member'
    personal_balance = db.Column(db.Float, default=0.0)

class PoolLedger(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tx_type = db.Column(db.String(20), nullable=False)  # 'opening', 'credit', 'debit'
    amount = db.Column(db.Float, nullable=False)
    category = db.Column(db.String(100), nullable=False)
    remarks = db.Column(db.String(255), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    user = db.relationship('User', backref='pool_transactions')

class BillClaim(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    category = db.Column(db.String(100), nullable=False)
    remarks = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(20), default='pending')  # 'pending', 'approved', 'rejected'
    created_at = db.Column(db.DateTime, default=datetime.now)
    user = db.relationship('User', backref='claims')

class PersonalTransaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    tx_type = db.Column(db.String(20), nullable=False)  # 'credit' or 'debit'
    amount = db.Column(db.Float, nullable=False)
    category = db.Column(db.String(100), nullable=False)
    remarks = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)

class ChatMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    message = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    user = db.relationship('User', backref='messages')

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def cashier_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if current_user.role != 'cashier':
            flash("Only the Cashier can perform this action.", "danger")
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated

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

        # First registered account automatically becomes the Cashier
        is_first = User.query.count() == 0
        role = 'cashier' if is_first else 'member'

        user = User(
            name=name,
            email=email,
            password_hash=generate_password_hash(password),
            role=role
        )
        db.session.add(user)
        db.session.commit()
        flash(f"Registered successfully as {role.upper()}! Please log in.", "success")
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
            return redirect(url_for('dashboard'))
        flash("Invalid email or password.", "danger")
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# ---------------- MAIN TABS ----------------
@app.route('/')
@login_required
def dashboard():
    now = datetime.now()
    month_start = datetime(now.year, now.month, 1)

    all_entries = PoolLedger.query.all()
    total_credits = sum(e.amount for e in all_entries if e.tx_type in ['opening', 'credit'])
    total_debits = sum(e.amount for e in all_entries if e.tx_type == 'debit')
    available_balance = total_credits - total_debits

    month_entries = PoolLedger.query.filter(PoolLedger.created_at >= month_start).all()
    month_opening = sum(e.amount for e in month_entries if e.tx_type == 'opening')
    month_expenses = sum(e.amount for e in month_entries if e.tx_type == 'debit')

    # Category-wise expenses (current month)
    category_totals = {cat: 0.0 for cat in CATEGORIES}
    for e in month_entries:
        if e.tx_type == 'debit' and e.category in category_totals:
            category_totals[e.category] += e.amount

    pending_claims = BillClaim.query.filter_by(status='pending').order_by(BillClaim.created_at.desc()).all()
    members = User.query.order_by(User.name).all()

    return render_template(
        'dashboard.html',
        available_balance=available_balance,
        month_opening=month_opening,
        month_expenses=month_expenses,
        category_totals=category_totals,
        categories=CATEGORIES,
        pending_claims=pending_claims,
        members=members
    )

@app.route('/cashier/transaction', methods=['POST'])
@login_required
@cashier_required
def cashier_transaction():
    tx_type = request.form['tx_type']  # 'opening', 'credit', 'debit'
    amount = float(request.form['amount'])
    category = request.form['category']
    remarks = request.form.get('remarks', '').strip()
    target_user_id = request.form.get('user_id') or None

    if target_user_id:
        target_user_id = int(target_user_id)

    entry = PoolLedger(
        tx_type=tx_type,
        amount=amount,
        category=category,
        remarks=remarks,
        user_id=target_user_id if target_user_id else current_user.id
    )
    db.session.add(entry)

    # Also log to individual account if a specific member was selected
    if target_user_id:
        member = User.query.get(target_user_id)
        if member:
            p_type = 'credit' if tx_type in ['opening', 'credit'] else 'debit'
            if p_type == 'credit':
                member.personal_balance += amount
            else:
                member.personal_balance -= amount
            db.session.add(PersonalTransaction(
                user_id=member.id,
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
    amount = float(request.form['amount'])
    category = request.form['category']
    remarks = request.form['remarks'].strip()

    claim = BillClaim(
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
    claim = BillClaim.query.get_or_404(claim_id)
    if claim.status != 'pending':
        flash("Claim already processed.", "warning")
        return redirect(url_for('dashboard'))

    if action == 'approve':
        claim.status = 'approved'
        # 1. Debit from Pool Available Balance
        pool_debit = PoolLedger(
            tx_type='debit',
            amount=claim.amount,
            category=claim.category,
            remarks=f"Approved Bill ({claim.user.name}): {claim.remarks}",
            user_id=claim.user_id
        )
        db.session.add(pool_debit)

        # 2. Credit into Member's Individual Virtual Account
        member = User.query.get(claim.user_id)
        member.personal_balance += claim.amount
        personal_credit = PersonalTransaction(
            user_id=member.id,
            tx_type='credit',
            amount=claim.amount,
            category=claim.category,
            remarks=f"Bill Approved: {claim.remarks}"
        )
        db.session.add(personal_credit)
        flash(f"Approved ₹{claim.amount:.2f} for {member.name}.", "success")

    elif action == 'reject':
        claim.status = 'rejected'
        flash("Bill claim rejected.", "secondary")

    db.session.commit()
    return redirect(url_for('dashboard'))

@app.route('/my-transactions')
@login_required
def my_transactions():
    # Cashier can view any member's account via query param; members only see their own
    selected_user_id = current_user.id
    if current_user.role == 'cashier' and request.args.get('user_id'):
        selected_user_id = int(request.args.get('user_id'))

    viewed_user = User.query.get_or_404(selected_user_id)
    txs = PersonalTransaction.query.filter_by(user_id=viewed_user.id).order_by(PersonalTransaction.created_at.desc()).all()
    members = User.query.all() if current_user.role == 'cashier' else []

    return render_template(
        'my_transactions.html',
        viewed_user=viewed_user,
        transactions=txs,
        members=members
    )

@app.route('/all-transactions')
@login_required
def all_transactions():
    person_filter = request.args.get('person', '')
    month_filter = request.args.get('month', '')  # format YYYY-MM
    category_filter = request.args.get('category', '')

    query = PoolLedger.query

    if person_filter:
        query = query.filter(PoolLedger.user_id == int(person_filter))
    if category_filter:
        query = query.filter(PoolLedger.category == category_filter)

    entries = query.order_by(PoolLedger.created_at.desc()).all()

    if month_filter:
        entries = [e for e in entries if e.created_at.strftime('%Y-%m') == month_filter]

    members = User.query.order_by(User.name).all()
    return render_template(
        'all_transactions.html',
        entries=entries,
        members=members,
        categories=CATEGORIES,
        person_filter=person_filter,
        month_filter=month_filter,
        category_filter=category_filter
    )

@app.route('/chat', methods=['GET', 'POST'])
@login_required
def chat():
    if request.method == 'POST':
        msg_text = request.form.get('message', '').strip()
        if msg_text:
            db.session.add(ChatMessage(user_id=current_user.id, message=msg_text))
            db.session.commit()
        return redirect(url_for('chat'))

    messages = ChatMessage.query.order_by(ChatMessage.created_at.asc()).limit(100).all()
    return render_template('chat.html', messages=messages)

@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        current_user.name = request.form['name'].strip()
        new_pass = request.form.get('new_password', '').strip()
        if new_pass:
            current_user.password_hash = generate_password_hash(new_pass)
        db.session.commit()
        flash("Profile updated!", "success")
        return redirect(url_for('profile'))

    members = User.query.all() if current_user.role == 'cashier' else []
    return render_template('profile.html', members=members)

@app.route('/profile/transfer-cashier/<int:user_id>', methods=['POST'])
@login_required
@cashier_required
def transfer_cashier(user_id):
    new_cashier = User.query.get_or_404(user_id)
    current_user.role = 'member'
    new_cashier.role = 'cashier'
    db.session.commit()
    flash(f"{new_cashier.name} is now the Cashier.", "success")
    return redirect(url_for('profile'))

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True)
