import os
import time
import uuid
from datetime import datetime, timedelta

import psycopg
from psycopg.rows import dict_row
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask_socketio import SocketIO, join_room, emit

DATABASE_URL = os.environ.get("DATABASE_URL")
SECRET_KEY = os.environ.get("SECRET_KEY")
ALLOWED_IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp"}
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "static", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY or "dev-only-insecure-key-change-me"
app.config["MAX_CONTENT_LENGTH"] = 6 * 1024 * 1024  # 6 Mo max par requête (avatars/stories)
if not SECRET_KEY:
    print("ATTENTION : SECRET_KEY n'est pas définie. Ajoute-la dans les variables d'environnement de Render "
          "(Settings > Environment) avant la mise en production.")

socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")


def allowed_image(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXT


def save_upload(file_storage):
    """Sauvegarde un fichier image uploadé avec un nom sécurisé et unique.
    Retourne le chemin relatif (ex. 'uploads/xxx.jpg') ou None si absent/invalide."""
    if not file_storage or not file_storage.filename:
        return None
    if not allowed_image(file_storage.filename):
        return None
    ext = secure_filename(file_storage.filename).rsplit(".", 1)[1].lower()
    fname = f"{uuid.uuid4().hex}.{ext}"
    file_storage.save(os.path.join(UPLOAD_DIR, fname))
    return f"uploads/{fname}"


# ---------------------------------------------------------------- DB ----

def get_db():
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL n'est pas définie. Sur Render : crée une base PostgreSQL, "
            "puis ajoute son 'Internal Database URL' dans les variables d'environnement du service web."
        )
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True)


def init_db():
    if not DATABASE_URL:
        print("DATABASE_URL absente — la base ne sera initialisée qu'une fois la variable ajoutée sur Render.")
        return
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    avatar TEXT,
                    bio TEXT DEFAULT '',
                    online BOOLEAN DEFAULT FALSE,
                    last_seen TIMESTAMP DEFAULT NOW(),
                    created_at TIMESTAMP DEFAULT NOW()
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id SERIAL PRIMARY KEY,
                    sender_id INTEGER REFERENCES users(id),
                    receiver_id INTEGER REFERENCES users(id),
                    content TEXT NOT NULL,
                    reply_to INTEGER REFERENCES messages(id),
                    is_read BOOLEAN DEFAULT FALSE,
                    is_deleted BOOLEAN DEFAULT FALSE,
                    edited BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS stories (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id),
                    content TEXT,
                    image TEXT,
                    background TEXT DEFAULT '#FF6B5B',
                    created_at TIMESTAMP DEFAULT NOW(),
                    expires_at TIMESTAMP NOT NULL
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS story_views (
                    id SERIAL PRIMARY KEY,
                    story_id INTEGER REFERENCES stories(id),
                    user_id INTEGER REFERENCES users(id),
                    viewed_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(story_id, user_id)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS notifications (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id),
                    from_user_id INTEGER REFERENCES users(id),
                    type TEXT NOT NULL,
                    content TEXT,
                    related_id INTEGER,
                    is_read BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW()
                );
            """)
            cur.execute("ALTER TABLE notifications ADD COLUMN IF NOT EXISTS from_user_id INTEGER REFERENCES users(id);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_pair ON messages (sender_id, receiver_id, created_at);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_stories_user ON stories (user_id, expires_at);")
    print("Base de données initialisée (tables + index).")


# ------------------------------------------------------------ helpers ----

def current_user():
    if "user_id" not in session:
        return None
    with get_db() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, username, display_name, avatar, bio, online FROM users WHERE id=%s", (session["user_id"],))
        return cur.fetchone()


def login_required(fn):
    from functools import wraps
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def conversation_partners(user_id):
    with get_db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT u.id, u.username, u.display_name, u.avatar, u.online, u.last_seen,
                   (SELECT content FROM messages m2
                     WHERE (m2.sender_id=u.id AND m2.receiver_id=%(me)s)
                        OR (m2.sender_id=%(me)s AND m2.receiver_id=u.id)
                     ORDER BY m2.created_at DESC LIMIT 1) AS last_message,
                   (SELECT created_at FROM messages m3
                     WHERE (m3.sender_id=u.id AND m3.receiver_id=%(me)s)
                        OR (m3.sender_id=%(me)s AND m3.receiver_id=u.id)
                     ORDER BY m3.created_at DESC LIMIT 1) AS last_at,
                   (SELECT COUNT(*) FROM messages m4
                     WHERE m4.sender_id=u.id AND m4.receiver_id=%(me)s AND m4.is_read=FALSE) AS unread
            FROM users u
            WHERE u.id != %(me)s
              AND EXISTS (
                SELECT 1 FROM messages m
                WHERE (m.sender_id=u.id AND m.receiver_id=%(me)s)
                   OR (m.sender_id=%(me)s AND m.receiver_id=u.id)
              )
            ORDER BY last_at DESC NULLS LAST;
        """, {"me": user_id})
        return cur.fetchall()


# -------------------------------------------------------------- routes ----

@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm_password") or ""
        if len(username) < 3:
            flash("Le nom d'utilisateur doit faire au moins 3 caractères.")
            return render_template("register.html")
        if len(password) < 6:
            flash("Le mot de passe doit faire au moins 6 caractères.")
            return render_template("register.html")
        if password != confirm:
            flash("Les mots de passe ne correspondent pas.")
            return render_template("register.html")
        try:
            with get_db() as conn, conn.cursor() as cur:
                cur.execute("SELECT id FROM users WHERE username=%s", (username,))
                if cur.fetchone():
                    flash("Ce nom d'utilisateur est déjà pris.")
                    return render_template("register.html")
                cur.execute(
                    "INSERT INTO users (username, password_hash, display_name) VALUES (%s, %s, %s)",
                    (username, generate_password_hash(password), username),
                )
        except RuntimeError as e:
            flash(str(e))
            return render_template("register.html")
        flash("Compte créé avec succès.")
        return redirect(url_for("login"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        try:
            with get_db() as conn, conn.cursor() as cur:
                cur.execute("SELECT id, password_hash FROM users WHERE username=%s", (username,))
                row = cur.fetchone()
                if not row or not check_password_hash(row["password_hash"], password):
                    flash("Nom d'utilisateur ou mot de passe incorrect.")
                    return render_template("login.html")
                cur.execute("UPDATE users SET online=TRUE, last_seen=NOW() WHERE id=%s", (row["id"],))
        except RuntimeError as e:
            flash(str(e))
            return render_template("login.html")
        session["user_id"] = row["id"]
        session["username"] = username
        return redirect(url_for("home"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    if "user_id" in session:
        try:
            with get_db() as conn, conn.cursor() as cur:
                cur.execute("UPDATE users SET online=FALSE, last_seen=NOW() WHERE id=%s", (session["user_id"],))
        except RuntimeError:
            pass
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def home():
    me = current_user()
    partners = conversation_partners(me["id"])
    with get_db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT s.id, s.content, s.image, s.background, s.created_at, u.id AS user_id,
                   u.username, u.display_name, u.avatar
            FROM stories s JOIN users u ON u.id = s.user_id
            WHERE s.expires_at > NOW()
            ORDER BY s.created_at DESC;
        """)
        stories = cur.fetchall()
    return render_template("home.html", me=me, partners=partners, stories=stories)


@app.route("/chat/<int:user_id>")
@login_required
def chat(user_id):
    me = current_user()
    with get_db() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, username, display_name, avatar, online, last_seen FROM users WHERE id=%s", (user_id,))
        partner = cur.fetchone()
        if not partner:
            return redirect(url_for("home"))
        cur.execute("""
            SELECT m.*, u.username AS sender_username
            FROM messages m JOIN users u ON u.id = m.sender_id
            WHERE (sender_id=%(me)s AND receiver_id=%(other)s) OR (sender_id=%(other)s AND receiver_id=%(me)s)
            ORDER BY m.created_at ASC;
        """, {"me": me["id"], "other": user_id})
        messages = cur.fetchall()
        cur.execute("UPDATE messages SET is_read=TRUE WHERE sender_id=%s AND receiver_id=%s AND is_read=FALSE",
                    (user_id, me["id"]))
    partners = conversation_partners(me["id"])
    return render_template("home.html", me=me, partners=partners, active_partner=partner,
                            messages=messages, stories=[])


@app.route("/search")
@login_required
def search():
    q = (request.args.get("q") or "").strip()
    me = current_user()
    if not q:
        return jsonify([])
    with get_db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT id, username, display_name, avatar, online
            FROM users
            WHERE id != %s AND (username ILIKE %s OR display_name ILIKE %s)
            LIMIT 15;
        """, (me["id"], f"%{q}%", f"%{q}%"))
        return jsonify(cur.fetchall())


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    me = current_user()
    if request.method == "POST":
        display_name = (request.form.get("display_name") or me["display_name"]).strip()
        bio = (request.form.get("bio") or "").strip()
        avatar_path = save_upload(request.files.get("avatar"))
        with get_db() as conn, conn.cursor() as cur:
            if avatar_path:
                cur.execute("UPDATE users SET display_name=%s, bio=%s, avatar=%s WHERE id=%s",
                            (display_name, bio, avatar_path, me["id"]))
            else:
                cur.execute("UPDATE users SET display_name=%s, bio=%s WHERE id=%s",
                            (display_name, bio, me["id"]))
        flash("Profil mis à jour.")
        return redirect(url_for("profile"))
    with get_db() as conn, conn.cursor() as cur:
        cur.execute("SELECT username, display_name, avatar, bio, created_at FROM users WHERE id=%s", (me["id"],))
        full = cur.fetchone()
    return render_template("profile.html", me=me, full=full)


# --------------------------------------------------------------- API -----

@app.route("/api/messages/<int:message_id>", methods=["PUT", "DELETE"])
@login_required
def api_message(message_id):
    me = current_user()
    with get_db() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM messages WHERE id=%s", (message_id,))
        msg = cur.fetchone()
        if not msg or msg["sender_id"] != me["id"]:
            return jsonify({"error": "Non autorisé"}), 403
        if request.method == "DELETE":
            cur.execute("UPDATE messages SET is_deleted=TRUE, content='Message supprimé' WHERE id=%s", (message_id,))
            socketio.emit("message_deleted", {"id": message_id}, room=f"user_{msg['receiver_id']}")
            socketio.emit("message_deleted", {"id": message_id}, room=f"user_{msg['sender_id']}")
            return jsonify({"ok": True})
        else:
            new_content = (request.json or {}).get("content", "").strip()
            if not new_content:
                return jsonify({"error": "Contenu vide"}), 400
            cur.execute("UPDATE messages SET content=%s, edited=TRUE, updated_at=NOW() WHERE id=%s",
                        (new_content, message_id))
            payload = {"id": message_id, "content": new_content}
            socketio.emit("message_updated", payload, room=f"user_{msg['receiver_id']}")
            socketio.emit("message_updated", payload, room=f"user_{msg['sender_id']}")
            return jsonify({"ok": True})


@app.route("/api/stories", methods=["POST"])
@login_required
def api_create_story():
    me = current_user()
    content = (request.form.get("content") or "").strip()
    background = request.form.get("background") or "#FF6B5B"
    image_path = save_upload(request.files.get("image"))
    if not content and not image_path:
        return jsonify({"error": "Ajoute un texte ou une image."}), 400
    expires = datetime.utcnow() + timedelta(hours=24)
    with get_db() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO stories (user_id, content, image, background, expires_at) VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (me["id"], content, image_path, background, expires),
        )
        story_id = cur.fetchone()["id"]
    return jsonify({"ok": True, "id": story_id})


@app.route("/api/stories/<int:story_id>/view", methods=["POST"])
@login_required
def api_view_story(story_id):
    me = current_user()
    with get_db() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO story_views (story_id, user_id) VALUES (%s,%s) ON CONFLICT (story_id, user_id) DO NOTHING",
            (story_id, me["id"]),
        )
    return jsonify({"ok": True})


@app.route("/api/stories/<int:story_id>/viewers")
@login_required
def api_story_viewers(story_id):
    with get_db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT u.username, u.display_name, sv.viewed_at
            FROM story_views sv JOIN users u ON u.id = sv.user_id
            WHERE sv.story_id=%s ORDER BY sv.viewed_at DESC;
        """, (story_id,))
        return jsonify(cur.fetchall())


@app.route("/api/notifications")
@login_required
def api_notifications():
    me = current_user()
    with get_db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT n.*, u.display_name AS from_display_name, u.avatar AS from_avatar
            FROM notifications n LEFT JOIN users u ON u.id = n.from_user_id
            WHERE n.user_id=%s ORDER BY n.created_at DESC LIMIT 30;
        """, (me["id"],))
        return jsonify(cur.fetchall())


@app.route("/api/notifications/unread_count")
@login_required
def api_notifications_unread_count():
    me = current_user()
    with get_db() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM notifications WHERE user_id=%s AND is_read=FALSE", (me["id"],))
        return jsonify({"count": cur.fetchone()["c"]})


@app.route("/api/notifications/read_all", methods=["POST"])
@login_required
def api_notifications_read_all():
    me = current_user()
    with get_db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE notifications SET is_read=TRUE WHERE user_id=%s AND is_read=FALSE", (me["id"],))
    return jsonify({"ok": True})


# ----------------------------------------------------------- Socket.IO ----

@socketio.on("connect")
def on_connect():
    if "user_id" in session:
        join_room(f"user_{session['user_id']}")
        with get_db() as conn, conn.cursor() as cur:
            cur.execute("UPDATE users SET online=TRUE WHERE id=%s", (session["user_id"],))
        emit("online_status", {"user_id": session["user_id"], "online": True}, broadcast=True)


@socketio.on("disconnect")
def on_disconnect():
    if "user_id" in session:
        with get_db() as conn, conn.cursor() as cur:
            cur.execute("UPDATE users SET online=FALSE, last_seen=NOW() WHERE id=%s", (session["user_id"],))
        emit("online_status", {"user_id": session["user_id"], "online": False}, broadcast=True)


@socketio.on("send_message")
def on_send_message(data):
    if "user_id" not in session:
        return
    sender_id = session["user_id"]
    receiver_id = data.get("receiver_id")
    content = (data.get("content") or "").strip()
    reply_to = data.get("reply_to")
    if not content or not receiver_id:
        return
    with get_db() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO messages (sender_id, receiver_id, content, reply_to) VALUES (%s,%s,%s,%s) "
            "RETURNING id, created_at",
            (sender_id, receiver_id, content, reply_to),
        )
        row = cur.fetchone()
        cur.execute(
            "INSERT INTO notifications (user_id, from_user_id, type, content, related_id) "
            "VALUES (%s,%s,'message',%s,%s) RETURNING id",
            (receiver_id, sender_id, content[:80], row["id"]),
        )
        notif_id = cur.fetchone()["id"]
        cur.execute("SELECT display_name, avatar FROM users WHERE id=%s", (sender_id,))
        sender = cur.fetchone()
    payload = {
        "id": row["id"], "sender_id": sender_id, "receiver_id": receiver_id,
        "content": content, "reply_to": reply_to,
        "created_at": row["created_at"].isoformat(), "is_read": False,
    }
    emit("receive_message", payload, room=f"user_{receiver_id}")
    emit("receive_message", payload, room=f"user_{sender_id}")
    emit("new_notification", {
        "id": notif_id, "from_user_id": sender_id,
        "from_display_name": sender["display_name"], "content": content[:80],
    }, room=f"user_{receiver_id}")


@socketio.on("typing")
def on_typing(data):
    if "user_id" not in session:
        return
    emit("typing", {"from": session["user_id"]}, room=f"user_{data.get('receiver_id')}")


@socketio.on("stop_typing")
def on_stop_typing(data):
    if "user_id" not in session:
        return
    emit("stop_typing", {"from": session["user_id"]}, room=f"user_{data.get('receiver_id')}")


@socketio.on("message_read")
def on_message_read(data):
    if "user_id" not in session:
        return
    other_id = data.get("other_id")
    with get_db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE messages SET is_read=TRUE WHERE sender_id=%s AND receiver_id=%s AND is_read=FALSE",
                    (other_id, session["user_id"]))
    emit("message_read", {"by": session["user_id"]}, room=f"user_{other_id}")


# --------------------------------------------------------------- errors ---

@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, message="Page introuvable."), 404


@app.errorhandler(500)
def server_error(e):
    return render_template("error.html", code=500, message="Erreur serveur."), 500


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)
