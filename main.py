import base64
import hashlib
import os
import secrets
from urllib.parse import urlencode

import jwt
import requests
from flask import Flask, jsonify, redirect, request, send_from_directory, session
from werkzeug.middleware.proxy_fix import ProxyFix


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CLIENT_ID = os.getenv("TELEGRAM_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("TELEGRAM_CLIENT_SECRET", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://web-production-4d1a9.up.railway.app").rstrip("/")
CALLBACK_URL = f"{PUBLIC_URL}/auth/telegram/callback"
SESSION_SECRET = os.getenv("SECRET_KEY") or hashlib.sha256(
    f"{CLIENT_SECRET}|seichas-session".encode()
).hexdigest()

app = Flask(__name__, static_folder=None)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = SESSION_SECRET
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
)


def telegram_configured():
    return bool(CLIENT_ID and CLIENT_SECRET)


def base64url_sha256(value):
    digest = hashlib.sha256(value.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@app.after_request
def security_headers(response):
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
    return response


@app.get("/api/session")
def api_session():
    user = session.get("telegram_user")
    return jsonify(
        authenticated=bool(user),
        telegram_configured=telegram_configured(),
        user=user,
    )


@app.get("/auth/telegram/start")
def telegram_start():
    if not telegram_configured():
        return redirect("/?auth=not_configured")

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    session["oidc_state"] = state
    session["oidc_nonce"] = nonce
    session["oidc_verifier"] = verifier

    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": CALLBACK_URL,
        "response_type": "code",
        "scope": "openid profile phone telegram:bot_access",
        "state": state,
        "nonce": nonce,
        "code_challenge": base64url_sha256(verifier),
        "code_challenge_method": "S256",
    }
    return redirect(f"https://oauth.telegram.org/auth?{urlencode(params)}")


@app.get("/auth/telegram/callback")
def telegram_callback():
    if request.args.get("error"):
        return redirect("/?auth=denied")
    if not telegram_configured() or request.args.get("state") != session.get("oidc_state"):
        return redirect("/?auth=invalid_state")

    code = request.args.get("code", "")
    verifier = session.get("oidc_verifier", "")
    nonce = session.get("oidc_nonce", "")
    if not code or not verifier:
        return redirect("/?auth=missing_code")

    try:
        token_response = requests.post(
            "https://oauth.telegram.org/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": CALLBACK_URL,
                "client_id": CLIENT_ID,
                "code_verifier": verifier,
            },
            auth=(CLIENT_ID, CLIENT_SECRET),
            timeout=15,
        )
        token_response.raise_for_status()
        id_token = token_response.json()["id_token"]
        signing_key = jwt.PyJWKClient(
            "https://oauth.telegram.org/.well-known/jwks.json"
        ).get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=CLIENT_ID,
            issuer="https://oauth.telegram.org",
        )
        if nonce and claims.get("nonce") != nonce:
            raise ValueError("Invalid nonce")

        session.clear()
        session.permanent = True
        session["telegram_user"] = {
            "id": claims.get("id") or claims.get("sub"),
            "name": claims.get("name") or claims.get("given_name") or "Участник",
            "username": claims.get("preferred_username"),
            "picture": claims.get("picture"),
            "phone_number": claims.get("phone_number"),
            "phone_verified": bool(claims.get("phone_number_verified")),
        }
        return redirect("/?auth=success")
    except Exception:
        app.logger.exception("Telegram authentication failed")
        session.clear()
        return redirect("/?auth=failed")


@app.post("/auth/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/<path:path>")
def static_files(path):
    full_path = os.path.join(BASE_DIR, path)
    if os.path.isfile(full_path):
        return send_from_directory(BASE_DIR, path)
    return send_from_directory(BASE_DIR, "index.html")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
