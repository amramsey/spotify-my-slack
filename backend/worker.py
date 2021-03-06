"""
Worker module
"""
import asyncio
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
import sqlalchemy

from backend.database import DATABASE
from backend.database.users import User
from backend.config import LOGGER, SETTINGS
from backend.utils.emojis import get_custom_emoji
from backend.utils.slack import (
    SlackApiError,
    UserProfileArgs,
    set_status,
)
from backend.utils.spotify import (
    GrantType,
    PlayerData,
    SpotifyApiError,
    TokenExchangeData,
    TrackItem,
    calc_spotify_expiry,
    get_new_access_token,
    get_player,
)


UPDATE_THRESHOLD = datetime.now(timezone.utc)


async def _update_user(user: User) -> None:
    """
    Update a single user
    """
    global UPDATE_THRESHOLD  # pylint:disable=global-statement
    update_threshold_delta = UPDATE_THRESHOLD - datetime.now(timezone.utc)
    if update_threshold_delta.total_seconds() > 0:
        LOGGER.debug(
            "Sleeping update thread for %s for %ss",
            user.id,
            update_threshold_delta.total_seconds(),
        )
        await asyncio.sleep(update_threshold_delta.total_seconds())

    # Handle Spotify token refreshes
    spotify_token_expired = user.spotifyExpiresAt <= datetime.now(
        timezone.utc
    ) - timedelta(minutes=5)
    if spotify_token_expired and not user.spotifyRefreshToken:
        LOGGER.warning(
            "Deleting user %s as their Spotify token is expired and no "
            "refresh token is available :(",
            user.id,
        )
        await user.delete()
        return

    if spotify_token_expired:
        LOGGER.debug("Refreshing Spotify token for user %s", user.id)
        update_ok = await _update_spotify_tokens(user)
        if not update_ok:
            return
        LOGGER.debug("Refreshing Spotify token for user %s COMPLETE", user.id)

    # Retrieve Spotify player status
    try:
        player: Optional[PlayerData] = await get_player(
            user.spotifyAccessToken
        )
    except SpotifyApiError as err:
        if err.retry_after is None:
            LOGGER.warning(
                "Exiting update loop. Could not retrieve player data for user %s: "
                "%s",
                user.id,
                err,
            )
        else:
            UPDATE_THRESHOLD = datetime.now(timezone.utc) + timedelta(
                seconds=err.retry_after
            )
            LOGGER.warning(
                "Exiting update loop. Spotify is throttling for %ss",
                err.retry_after,
            )
        return
    if player is not None and player.item is not None and player.is_playing:
        user_profile_args = UserProfileArgs(
            status_text=_calc_status_text(player.item),
            status_emoji=get_custom_emoji(user, player.item),
        )
        LOGGER.debug("Setting user status %s", user_profile_args)  # TODO rm
        await _set_user_status(user, user_profile_args, True)
    elif user.statusSetLastTime:
        user_profile_args = UserProfileArgs(status_text="", status_emoji="")
        await _set_user_status(user, user_profile_args, False)


async def _update_spotify_tokens(user: User) -> bool:
    """
    Update the user's Spotify tokens. Returns success status
    """
    try:
        exchange_data: TokenExchangeData = await get_new_access_token(
            user.spotifyRefreshToken, GrantType.REFRESH_TOKEN
        )
    except SpotifyApiError as err:
        LOGGER.warning(
            "Exiting update loop. Could not refresh Spotify token for "
            "user %s: %s",
            user.id,
            err,
        )
        err_dict = err.response_json()
        if err_dict.get("error_description") == "Refresh token revoked":
            LOGGER.warning(
                "Deleting user %s as their Spotify refresh token is revoked "
                "and we have no way to recover :(",
                user.id,
            )
            await user.delete()
        return False
    await user.update(
        spotifyExpiresAt=calc_spotify_expiry(exchange_data.expires_in),
        spotifyAccessToken=exchange_data.access_token,
        spotifyRefreshToken=exchange_data.refresh_token or "",
        updatedAt=datetime.now(timezone.utc),
    )
    return True


async def _set_user_status(
    user: User, user_profile_args: UserProfileArgs, status_set_last_time: bool
) -> bool:
    """
    Set the user status & update their database entry. Returns success status
    """
    try:
        await set_status(user_profile_args, user.slackAccessToken)
    except SlackApiError as err:
        LOGGER.warning(
            "Exiting update loop. Could not set status for user %s: " "%s",
            user.id,
            err,
        )
        return False
    await user.update(
        statusSetLastTime=status_set_last_time,
        updatedAt=datetime.now(timezone.utc),
    )
    return True


def _calc_status_text(track: TrackItem) -> str:
    """
    Calculate the status text based on a track
    """
    if len(track.artists) == 0:
        by_artists = ""
    else:
        by_artists = f' by {", ".join(a.name for a in track.artists)}'
    status_text = f"{track.name}{by_artists}"
    if len(status_text) > 100:
        status_text = f"{status_text[:99].strip()}…"
    return status_text


async def _throttled_update_user(user, sem):
    async with sem:  # semaphore limits num of simultaneous updated
        try:
            await _update_user(user)
        except (httpx.HTTPError, sqlalchemy.exc.SQLAlchemyError) as err:
            LOGGER.error(
                "Fatal error in update loop for user %s: %s", user.id, err
            )


async def worker_entrypoint() -> None:
    """
    The entrypoint for the worker. Currently a stub
    """
    # pylint:disable=protected-access
    DATABASE._connection_context = ContextVar("connection_context")  # Hack :(
    sem = asyncio.Semaphore(SETTINGS.worker_coroutines)
    while True:
        LOGGER.debug("Starting global update loop")
        update_tasks = [
            _throttled_update_user(user=user, sem=sem)
            for user in await User.objects.all()
        ]
        await asyncio.gather(*update_tasks)
