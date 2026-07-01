"""
Discord OAuth2 login for MultiworldGG WebHostLib.

Uses Authlib's Flask integration. Links the existing anonymous session
(session["_id"], already set on every visitor by WebHostLib's before_request
hook) to a verified Discord account, stored in the DiscordIdentity table.

This does NOT replace the existing session system. It augments it: every
visitor already gets a session["_id"] UUID; logging in with Discord just
attaches a verified discord_id to that UUID so other code (lobby uploads,
room password reveal) can check `current_discord_identity()`.

Routes added:
    GET  /auth/discord/login     -> redirect to Discord
    GET  /auth/discord/callback  -> OAuth callback, links identity, redirects back
    POST /auth/discord/logout    -> unlinks Discord from this session (keeps anon session)
"""

from __future__ import annotations

from pony.orm import db_session, commit
from flask import Blueprint, redirect, request, session, url_for, flash
from authlib.integrations.flask_client import OAuth

from WebHostLib import app
from WebHostLib.models import DiscordIdentity

discord_auth = Blueprint("discord_auth", __name__, url_prefix="/auth/discord")

oauth = OAuth(app)
oauth.register(
    name="discord",
    client_id=app.config.get("DISCORD_CLIENT_ID"),
    client_secret=app.config.get("DISCORD_CLIENT_SECRET"),
    access_token_url="https://discord.com/api/oauth2/token",
    authorize_url="https://discord.com/api/oauth2/authorize",
    api_base_url="https://discord.com/api/",
    client_kwargs={"scope": "identify"},
)


def current_discord_identity() -> DiscordIdentity | None:
    """Return the DiscordIdentity linked to the current session, or None if not linked."""
    sid = session.get("_id")
    if not sid:
        return None
    return DiscordIdentity.get(session_id=sid)


@discord_auth.route("/login")
def login():
    session["_discord_return_to"] = request.args.get("next") or request.referrer or url_for("lobby_list")
    redirect_uri = url_for("discord_auth.callback", _external=True)
    return oauth.discord.authorize_redirect(redirect_uri)


@discord_auth.route("/callback")
def callback():
    token = oauth.discord.authorize_access_token()
    resp = oauth.discord.get("users/@me", token=token)
    resp.raise_for_status()
    profile = resp.json()

    discord_id = str(profile["id"])
    username = profile.get("username", "Unknown")
    avatar = profile.get("avatar")

    sid = session["_id"]  # always present; set by WebHostLib's existing before_request hook

    with db_session:
        identity = DiscordIdentity.get(session_id=sid)
        if identity:
            identity.discord_id = discord_id
            identity.discord_username = username
            identity.discord_avatar = avatar
        else:
            DiscordIdentity(
                session_id=sid,
                discord_id=discord_id,
                discord_username=username,
                discord_avatar=avatar,
            )
        commit()

        # Retroactively stamp this Discord identity onto any LobbyPlayer rows
        # for this session that joined before linking (e.g. clicked login from
        # the upload-blocked prompt).
        from WebHostLib.models import LobbyPlayer
        unlinked_memberships = LobbyPlayer.select(
            lambda p: p.session_id == sid and p.discord_id is None
        )
        for membership in unlinked_memberships:
            membership.discord_id = discord_id
            membership.discord_username = username
            membership.discord_avatar = avatar
        commit()

    flash(f"Linked Discord account: {username}")
    return redirect(session.pop("_discord_return_to", url_for("lobby_list")))


@discord_auth.route("/logout", methods=["POST"])
def logout():
    sid = session.get("_id")
    if sid:
        with db_session:
            identity = DiscordIdentity.get(session_id=sid)
            if identity:
                identity.delete()
                commit()
    flash("Discord account unlinked from this session.")
    return redirect(request.referrer or url_for("lobby_list"))
