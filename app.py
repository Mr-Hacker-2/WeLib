import os
import uuid
import boto3
from flask import Flask, request, jsonify, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from flask_jwt_extended import (
    JWTManager, create_access_token,
    jwt_required, get_jwt_identity
)
from datetime import timedelta
from botocore.client import Config
from werkzeug.utils import secure_filename
from functools import lru_cache

# ── App Setup ──────────────────────────────────────────────
app = Flask(__name__, static_folder='.', template_folder='.')

_raw_db = os.environ.get('DATABASE_URL', 'sqlite:///bookvault.db')
app.config['SQLALCHEMY_DATABASE_URI']        = _raw_db.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['JWT_SECRET_KEY']                 = os.environ.get('JWT_SECRET', 'change-me-in-production')
app.config['JWT_ACCESS_TOKEN_EXPIRES']       = timedelta(days=7)
app.config['MAX_CONTENT_LENGTH']             = 200 * 1024 * 1024  # 200 MB

db     = SQLAlchemy(app)
bcrypt = Bcrypt(app)
jwt    = JWTManager(app)

# ── Backblaze B2 ───────────────────────────────────────────
B2_KEY_ID      = os.environ.get('B2_KEY_ID', '')
B2_APP_KEY     = os.environ.get('B2_APP_KEY', '')
B2_BUCKET_NAME = os.environ.get('B2_BUCKET_NAME', '')
B2_ENDPOINT    = os.environ.get('B2_ENDPOINT', '')

@lru_cache(maxsize=1)
def get_b2_client():
    return boto3.client(
        's3',
        endpoint_url=B2_ENDPOINT,
        aws_access_key_id=B2_KEY_ID,
        aws_secret_access_key=B2_APP_KEY,
        config=Config(signature_version='s3v4'),
    )

# ── Admin config from env ──────────────────────────────────
ADMIN_EMAIL    = os.environ.get('ADMIN_EMAIL',    'admin@bookvault.com')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin1')
CASHAPP_HANDLE = os.environ.get('CASHAPP_HANDLE', '$YourCashAppHandle')

# ── Models ─────────────────────────────────────────────────
class User(db.Model):
    __tablename__ = 'users'
    id       = db.Column(db.Integer, primary_key=True)
    name     = db.Column(db.String(120), nullable=False)
    email    = db.Column(db.String(200), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    tier     = db.Column(db.Integer, default=1)
    is_admin = db.Column(db.Boolean, default=False)
    status   = db.Column(db.String(20), default='pending')

class Book(db.Model):
    __tablename__ = 'books'
    id          = db.Column(db.Integer, primary_key=True)
    title       = db.Column(db.String(300), nullable=False)
    author      = db.Column(db.String(200), nullable=False)
    genre       = db.Column(db.String(100))
    year        = db.Column(db.Integer)
    color       = db.Column(db.String(20), default='#1a3a5c')
    description = db.Column(db.Text)
    file_key    = db.Column(db.String(500))
    file_name   = db.Column(db.String(300))

class Manga(db.Model):
    __tablename__ = 'manga'
    id          = db.Column(db.Integer, primary_key=True)
    title       = db.Column(db.String(300), nullable=False)
    author      = db.Column(db.String(200), nullable=False)
    genre       = db.Column(db.String(100))
    chapters    = db.Column(db.Integer)
    status      = db.Column(db.String(50), default='Ongoing')
    color       = db.Column(db.String(20), default='#1a1a2e')
    description = db.Column(db.Text)
    file_key    = db.Column(db.String(500))
    file_name   = db.Column(db.String(300))

# ── DB Init ────────────────────────────────────────────────
def init_db():
    db.create_all()
    try:
        if not User.query.filter_by(email=ADMIN_EMAIL).first():
            db.session.add(User(
                name='Admin', email=ADMIN_EMAIL,
                password=bcrypt.generate_password_hash(ADMIN_PASSWORD).decode(),
                tier=0, is_admin=True, status='active'
            ))
            db.session.commit()
    except Exception:
        db.session.rollback()

with app.app_context():
    init_db()

# ── Helpers ────────────────────────────────────────────────
ALLOWED_EXT       = {'pdf', 'epub', 'txt'}
ALLOWED_EXT_MANGA = {'pdf', 'cbz', 'cbr', 'zip'}

def allowed_file(fn):
    return '.' in fn and fn.rsplit('.', 1)[1].lower() in ALLOWED_EXT

def allowed_manga_file(fn):
    return '.' in fn and fn.rsplit('.', 1)[1].lower() in ALLOWED_EXT_MANGA

def require_admin():
    user = User.query.get(int(get_jwt_identity()))
    if not user or not user.is_admin:
        return None, (jsonify({'error': 'Admin only'}), 403)
    return user, None

def make_stream_url(key, content_type='application/pdf'):
    """Presigned URL for inline reading (1 hour)."""
    if not key or not B2_BUCKET_NAME or not B2_ENDPOINT:
        return None
    try:
        return get_b2_client().generate_presigned_url(
            'get_object',
            Params={'Bucket': B2_BUCKET_NAME, 'Key': key,
                    'ResponseContentDisposition': 'inline',
                    'ResponseContentType': content_type},
            ExpiresIn=3600
        )
    except Exception:
        return None

def make_download_url(key, filename):
    """Presigned URL for downloading (5 min)."""
    if not key or not B2_BUCKET_NAME or not B2_ENDPOINT:
        return None
    try:
        return get_b2_client().generate_presigned_url(
            'get_object',
            Params={'Bucket': B2_BUCKET_NAME, 'Key': key,
                    'ResponseContentDisposition': f'attachment; filename="{filename}"'},
            ExpiresIn=300
        )
    except Exception as e:
        raise e

# ── Frontend ───────────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

# ── Public config ──────────────────────────────────────────
@app.route('/api/config')
def public_config():
    return jsonify({'cashapp_handle': CASHAPP_HANDLE})

# ── Auth ───────────────────────────────────────────────────
@app.route('/api/auth/login', methods=['POST'])
def login():
    data     = request.get_json()
    username = (data.get('username') or '').strip()
    password = (data.get('password') or '')

    # Admin login via env credentials
    if username in ('admin', ADMIN_EMAIL) and password == ADMIN_PASSWORD:
        admin = User.query.filter_by(is_admin=True).first()
        if admin:
            return jsonify({'token': create_access_token(identity=str(admin.id)),
                            'name': admin.name, 'tier': admin.tier, 'is_admin': True})

    user = User.query.filter_by(email=username).first()
    if not user or not bcrypt.check_password_hash(user.password, password):
        return jsonify({'error': 'Invalid credentials'}), 401
    if user.status == 'pending':
        return jsonify({'error': 'Your account is pending admin approval.'}), 403
    if user.status == 'declined':
        return jsonify({'error': 'Your account request was declined.'}), 403

    return jsonify({'token': create_access_token(identity=str(user.id)),
                    'name': user.name, 'tier': user.tier, 'is_admin': user.is_admin})


@app.route('/api/auth/register', methods=['POST'])
def register():
    data  = request.get_json()
    name  = (data.get('name') or '').strip()
    email = (data.get('email') or '').strip().lower()
    pw    = (data.get('password') or '')
    tier  = int(data.get('tier', 1))

    if not name or not email or len(pw) < 4:
        return jsonify({'error': 'Name, email and password (min 4 chars) required'}), 400
    if '@' not in email or '.' not in email.split('@')[-1]:
        return jsonify({'error': 'Invalid email address'}), 400
    if tier not in (1, 2):
        return jsonify({'error': 'Invalid tier'}), 400
    if User.query.filter_by(email=email).first():
        return jsonify({'error': 'Email already registered'}), 409

    user = User(name=name, email=email,
                password=bcrypt.generate_password_hash(pw).decode(),
                tier=tier, is_admin=False, status='pending')
    db.session.add(user)
    db.session.commit()
    return jsonify({'status': 'pending',
                    'message': 'Request submitted! Admin will review your payment and activate your account.'}), 201


@app.route('/api/auth/me', methods=['GET'])
@jwt_required()
def me():
    user = User.query.get(int(get_jwt_identity()))
    if not user:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({'name': user.name, 'tier': user.tier, 'is_admin': user.is_admin})

# ── Books (public list) ────────────────────────────────────
@app.route('/api/books', methods=['GET'])
def list_books():
    books = Book.query.order_by(Book.id.asc()).all()
    return jsonify([{'id': b.id, 'title': b.title, 'author': b.author,
                     'genre': b.genre, 'year': b.year, 'color': b.color,
                     'description': b.description, 'has_file': bool(b.file_key)}
                    for b in books])

# ── Books read — tier 1+ gets inline stream URL ────────────
@app.route('/api/books/<int:book_id>/read', methods=['GET'])
@jwt_required()
def read_book(book_id):
    user = User.query.get(int(get_jwt_identity()))
    if not user or user.tier < 1:
        return jsonify({'error': 'Subscription required'}), 403
    b = Book.query.get_or_404(book_id)
    return jsonify({'id': b.id, 'title': b.title, 'author': b.author,
                    'genre': b.genre, 'year': b.year, 'description': b.description,
                    'has_file': bool(b.file_key),
                    'stream_url': make_stream_url(b.file_key)})

# ── Books download — tier 2 only ───────────────────────────
@app.route('/api/books/<int:book_id>/download', methods=['GET'])
@jwt_required()
def download_book(book_id):
    user = User.query.get(int(get_jwt_identity()))
    if not user or user.tier < 2:
        return jsonify({'error': 'Scholar plan required to download'}), 403
    b = Book.query.get_or_404(book_id)
    if not b.file_key:
        return jsonify({'error': 'No file uploaded for this book'}), 404
    try:
        return jsonify({'url': make_download_url(b.file_key, b.file_name)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── Manga (public list) ────────────────────────────────────
@app.route('/api/manga', methods=['GET'])
def list_manga():
    items = Manga.query.order_by(Manga.id.asc()).all()
    return jsonify([{'id': m.id, 'title': m.title, 'author': m.author,
                     'genre': m.genre, 'chapters': m.chapters, 'status': m.status,
                     'color': m.color, 'description': m.description,
                     'has_file': bool(m.file_key)}
                    for m in items])

# ── Manga read — tier 1+ gets inline stream URL ────────────
@app.route('/api/manga/<int:manga_id>/read', methods=['GET'])
@jwt_required()
def read_manga(manga_id):
    user = User.query.get(int(get_jwt_identity()))
    if not user or user.tier < 1:
        return jsonify({'error': 'Subscription required'}), 403
    m = Manga.query.get_or_404(manga_id)
    return jsonify({'id': m.id, 'title': m.title, 'author': m.author,
                    'genre': m.genre, 'chapters': m.chapters, 'status': m.status,
                    'description': m.description, 'has_file': bool(m.file_key),
                    'stream_url': make_stream_url(m.file_key)})

# ── Manga download — tier 2 only ──────────────────────────
@app.route('/api/manga/<int:manga_id>/download', methods=['GET'])
@jwt_required()
def download_manga(manga_id):
    user = User.query.get(int(get_jwt_identity()))
    if not user or user.tier < 2:
        return jsonify({'error': 'Scholar plan required to download'}), 403
    m = Manga.query.get_or_404(manga_id)
    if not m.file_key:
        return jsonify({'error': 'No file uploaded for this manga'}), 404
    try:
        return jsonify({'url': make_download_url(m.file_key, m.file_name)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── Admin — Books ──────────────────────────────────────────
@app.route('/api/admin/books', methods=['POST'])
@jwt_required()
def admin_upload_book():
    _, err = require_admin()
    if err: return err
    title  = request.form.get('title', '').strip()
    author = request.form.get('author', '').strip()
    if not title or not author:
        return jsonify({'error': 'Title and author required'}), 400

    file_key = file_name = None
    if 'file' in request.files:
        f = request.files['file']
        if f and f.filename and allowed_file(f.filename):
            if not B2_BUCKET_NAME:
                return jsonify({'error': 'File storage not configured'}), 503
            original  = secure_filename(f.filename)
            file_key  = f'books/{uuid.uuid4().hex}/{original}'
            file_name = original
            try:
                get_b2_client().upload_fileobj(f, B2_BUCKET_NAME, file_key)
            except Exception as e:
                return jsonify({'error': f'B2 upload failed: {e}'}), 500

    year = request.form.get('year', '')
    book = Book(
        title=title, author=author,
        genre=request.form.get('genre', ''),
        year=int(year) if year.isdigit() else None,
        color=request.form.get('color', '#1a3a5c'),
        description=request.form.get('description', ''),
        file_key=file_key, file_name=file_name
    )
    db.session.add(book)
    db.session.commit()
    return jsonify({'id': book.id, 'title': book.title}), 201


@app.route('/api/admin/books/<int:book_id>', methods=['DELETE'])
@jwt_required()
def admin_delete_book(book_id):
    _, err = require_admin()
    if err: return err
    book = Book.query.get_or_404(book_id)
    if book.file_key and B2_BUCKET_NAME:
        try: get_b2_client().delete_object(Bucket=B2_BUCKET_NAME, Key=book.file_key)
        except Exception: pass
    db.session.delete(book)
    db.session.commit()
    return jsonify({'deleted': book_id})

# ── Admin — Manga ──────────────────────────────────────────
@app.route('/api/admin/manga', methods=['POST'])
@jwt_required()
def admin_upload_manga():
    _, err = require_admin()
    if err: return err
    title  = request.form.get('title', '').strip()
    author = request.form.get('author', '').strip()
    if not title or not author:
        return jsonify({'error': 'Title and author required'}), 400

    file_key = file_name = None
    if 'file' in request.files:
        f = request.files['file']
        if f and f.filename and allowed_manga_file(f.filename):
            if not B2_BUCKET_NAME:
                return jsonify({'error': 'File storage not configured'}), 503
            original  = secure_filename(f.filename)
            file_key  = f'manga/{uuid.uuid4().hex}/{original}'
            file_name = original
            try:
                get_b2_client().upload_fileobj(f, B2_BUCKET_NAME, file_key)
            except Exception as e:
                return jsonify({'error': f'B2 upload failed: {e}'}), 500

    chapters = request.form.get('chapters', '')
    manga = Manga(
        title=title, author=author,
        genre=request.form.get('genre', ''),
        chapters=int(chapters) if chapters.isdigit() else None,
        status=request.form.get('status', 'Ongoing'),
        color=request.form.get('color', '#1a1a2e'),
        description=request.form.get('description', ''),
        file_key=file_key, file_name=file_name
    )
    db.session.add(manga)
    db.session.commit()
    return jsonify({'id': manga.id, 'title': manga.title}), 201


@app.route('/api/admin/manga/<int:manga_id>', methods=['DELETE'])
@jwt_required()
def admin_delete_manga(manga_id):
    _, err = require_admin()
    if err: return err
    manga = Manga.query.get_or_404(manga_id)
    if manga.file_key and B2_BUCKET_NAME:
        try: get_b2_client().delete_object(Bucket=B2_BUCKET_NAME, Key=manga.file_key)
        except Exception: pass
    db.session.delete(manga)
    db.session.commit()
    return jsonify({'deleted': manga_id})

# ── Admin — Stats ──────────────────────────────────────────
@app.route('/api/admin/stats', methods=['GET'])
@jwt_required()
def admin_stats():
    _, err = require_admin()
    if err: return err
    tier1 = User.query.filter_by(tier=1, status='active').count()
    tier2 = User.query.filter_by(tier=2, status='active').count()
    return jsonify({
        'total_books':     Book.query.count(),
        'total_manga':     Manga.query.count(),
        'total_users':     User.query.filter_by(is_admin=False, status='active').count(),
        'pending_users':   User.query.filter_by(is_admin=False, status='pending').count(),
        'tier1_users':     tier1,
        'tier2_users':     tier2,
        'monthly_revenue': (tier1 * 10) + (tier2 * 20)
    })

# ── Admin — Users ──────────────────────────────────────────
@app.route('/api/admin/users', methods=['GET'])
@jwt_required()
def admin_users():
    _, err = require_admin()
    if err: return err
    users = User.query.filter_by(is_admin=False).filter(User.status != 'pending').order_by(User.id.desc()).all()
    return jsonify([{'id': u.id, 'name': u.name, 'email': u.email, 'tier': u.tier, 'status': u.status}
                    for u in users])


@app.route('/api/admin/users/<int:user_id>', methods=['DELETE'])
@jwt_required()
def admin_delete_user(user_id):
    _, err = require_admin()
    if err: return err
    user = User.query.get_or_404(user_id)
    if user.is_admin:
        return jsonify({'error': 'Cannot delete admin'}), 400
    db.session.delete(user)
    db.session.commit()
    return jsonify({'deleted': user_id})

# ── Admin — Requests ───────────────────────────────────────
@app.route('/api/admin/requests', methods=['GET'])
@jwt_required()
def admin_requests():
    _, err = require_admin()
    if err: return err
    users = User.query.filter_by(is_admin=False, status='pending').order_by(User.id.asc()).all()
    return jsonify([{'id': u.id, 'name': u.name, 'email': u.email, 'tier': u.tier}
                    for u in users])


@app.route('/api/admin/requests/<int:user_id>/approve', methods=['POST'])
@jwt_required()
def admin_approve(user_id):
    _, err = require_admin()
    if err: return err
    user = User.query.get_or_404(user_id)
    user.status = 'active'
    db.session.commit()
    return jsonify({'approved': user_id})


@app.route('/api/admin/requests/<int:user_id>/decline', methods=['POST'])
@jwt_required()
def admin_decline(user_id):
    _, err = require_admin()
    if err: return err
    user = User.query.get_or_404(user_id)
    user.status = 'declined'
    db.session.commit()
    return jsonify({'declined': user_id})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)
