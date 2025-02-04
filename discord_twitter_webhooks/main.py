import functools
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import uuid4

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from loguru import logger
from reader import InvalidFeedURLError, Reader, StorageError
from starlette import status

from discord_twitter_webhooks._dataclasses import (
    ApplicationSettings,
    Group,
    get_app_settings,
    get_group,
    set_app_settings,
)
from discord_twitter_webhooks.reader_settings import get_reader
from discord_twitter_webhooks.send_to_discord import (
    blacklisted,
    has_media,
    send_embed,
    send_link,
    send_text,
    send_to_discord,
    whitelisted,
)
from discord_twitter_webhooks.translate import languages_from, languages_to

if TYPE_CHECKING:
    from reader.types import Entry, EntryLike, FeedLike

app = FastAPI()
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

reader: Reader = get_reader()

# TODO: Add /debug page to show all the database fields
# TODO: Add backup/restore functionality


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    """Return the index page.

    Returns:
        The index page.
    """
    groups = reader.get_tag((), "groups", [])
    list_of_groups = [get_group(reader, group) for group in groups]
    list_of_groups = [group for group in list_of_groups if group]  # Remove None values

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "groups": list_of_groups,
            "app_settings": get_app_settings(reader),
        },
    )


@app.get("/add", response_class=HTMLResponse)
async def add(request: Request) -> Response:
    """Return the add page.

    Returns:
        The add page.
    """
    return templates.TemplateResponse(
        "feed.html",
        {
            "request": request,
            "settings": Group(),
            "modifying": False,
            "group_name": None,
            "languages_from": languages_from,
            "languages_to": languages_to,
            "global_settings": get_app_settings(reader),
        },
    )


@app.get("/modify/{uuid}", response_class=HTMLResponse)
async def modify(request: Request, uuid: str) -> Response:
    """Return the add page.

    Returns:
        The add page.
    """
    # Get the old settings
    group: Group = get_group(reader, uuid)

    return templates.TemplateResponse(
        "feed.html",
        {
            "request": request,
            "settings": group,
            "modifying": True,
            "languages_from": languages_from,
            "languages_to": languages_to,
            "global_settings": get_app_settings(reader),
        },
    )


@app.post("/feed")
async def feed(  # noqa: PLR0913, ANN201
    name: Annotated[str, Form(title="Group Name")],
    webhooks: Annotated[str, Form(title="Webhook URLs")],
    usernames: Annotated[str, Form(title="Twitter Usernames")],
    uuid: Annotated[str, Form(title="UUID")] = "",
    send_retweets: Annotated[bool, Form(title="Include Retweets?")] = False,
    send_replies: Annotated[bool, Form(title="Include Replies?")] = False,
    only_send_if_media: Annotated[bool, Form(title="Only Send if tweet has media?")] = False,
    send_as_text: Annotated[bool, Form(title="Send Text?")] = False,
    send_as_text_username: Annotated[bool, Form(title="Append username before text?")] = False,
    send_as_embed: Annotated[bool, Form(title="Send Embed?")] = False,
    send_as_link: Annotated[bool, Form(title="Send Only Link?")] = False,
    unescape_html: Annotated[bool, Form(title="Unescape HTML?")] = False,
    remove_copyright: Annotated[bool, Form(title="Remove Copyright?")] = False,
    translate: Annotated[bool, Form(title="Translate?")] = False,
    translate_from: Annotated[str, Form(title="Translate From")] = "auto",
    translate_to: Annotated[str, Form(title="Translate To")] = "en-GB",
    link_destination: Annotated[Literal["Twitter", "Nitter"], Form(title="Link destination")] = "Twitter",
    whitelist_enabled: Annotated[bool, Form(title="Whitelist enabled?")] = False,
    whitelist: Annotated[str, Form(title="Whitelist")] = "",
    whitelist_regex: Annotated[str, Form(title="Whitelist regex")] = "",
    blacklist_enabled: Annotated[bool, Form(title="Blacklist enabled?")] = False,
    blacklist: Annotated[str, Form(title="Blacklist")] = "",
    blacklist_regex: Annotated[str, Form(title="Blacklist regex")] = "",
    replace_youtube: Annotated[bool, Form(title="Replace YouTube links?")] = False,
    replace_reddit: Annotated[bool, Form(title="Replace Reddit links?")] = False,
):
    """Create or modify a group."""
    if not uuid:
        logger.info(f"Creating new group {name}")
        uuid = str(uuid4())

    # Set is to remove duplicates
    usernames_split = list(set(usernames.splitlines()))

    # Get the RSS feeds for each username
    # TODO: Check if the RSS feed is valid
    rss_feeds = [f"{get_app_settings(reader).nitter_instance}/{_feed}/rss" for _feed in usernames_split]

    group = Group(
        uuid=uuid,
        name=name,
        webhooks=list(set(webhooks.splitlines())),
        usernames=usernames_split,
        rss_feeds=rss_feeds,
        send_retweets=send_retweets,
        send_replies=send_replies,
        only_send_if_media=only_send_if_media,
        send_as_text=send_as_text,
        send_as_text_username=send_as_text_username,
        send_as_embed=send_as_embed,
        send_as_link=send_as_link,
        unescape_html=unescape_html,
        remove_copyright=remove_copyright,
        translate=translate,
        translate_from=translate_from,
        translate_to=translate_to,
        whitelist_enabled=whitelist_enabled,
        whitelist=list(set(whitelist.splitlines())),
        whitelist_regex=list(set(whitelist_regex.splitlines())),
        blacklist_enabled=blacklist_enabled,
        blacklist=list(set(blacklist.splitlines())),
        blacklist_regex=list(set(blacklist_regex.splitlines())),
        link_destination=link_destination,
        replace_reddit=replace_reddit,
        replace_youtube=replace_youtube,
    )

    # This will be used when adding group.rss_feeds
    rss_feeds = []

    # Add the group to the reader
    reader.set_tag((), uuid, group.__dict__)
    for _name in usernames_split:
        name_url = f"{get_app_settings(reader).nitter_instance}/{_name}/rss"  # TODO: Check if URL is valid
        # Add the rss feed to the reader
        try:
            reader.add_feed(name_url, exist_ok=True)
        except InvalidFeedURLError:
            logger.error(f"Invalid URL {name_url}")
            continue
        except StorageError:
            logger.error(f"Got StorageError when adding {name_url}")
            continue

        # Update the feed
        reader.update_feeds(feed=name_url, workers=4)

        # Mark every entry as read
        _entry: Entry | EntryLike
        for _entry in reader.get_entries(feed=name_url):
            logger.debug(f"Marking entry {_entry} as read")
            reader.mark_entry_as_read(_entry)

        # Add what groups the feed is connected to
        our_feed = reader.get_feed(name_url)
        groups = reader.get_tag(our_feed, "groups", [])  # type: ignore  # noqa: PGH003
        groups.append(uuid)
        reader.set_tag(our_feed, "groups", list(set(groups)))  # type: ignore  # noqa: PGH003
        logger.info(f"Added group {group.uuid} to feed {name_url}")

        rss_feeds.append(name_url)

    # Add the group to the groups list
    groups = reader.get_tag((), "groups", [])
    groups.append(uuid)
    reader.set_tag((), "groups", list(set(groups)))
    logger.info(f"Added group {group.uuid} to groups list")
    logger.info(f"Group list is now {set(groups)}")

    # Redirect to the index page.
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/remove_group")
async def remove_group_post(uuid: Annotated[str, Form()]) -> RedirectResponse:
    """Remove a group.

    Returns:
        str: The index page.
    """
    group: Group = get_group(reader, uuid)
    logger.info(f"Removing group {group}")

    reader.delete_tag((), uuid)

    groups = reader.get_tag((), "groups", [])
    groups.remove(uuid)
    reader.set_tag((), "groups", groups)

    # Remove the group tag from every RSS feed that has it
    _feed: FeedLike
    for _feed in reader.get_feeds():
        if uuid in reader.get_tag(_feed, "groups", []):
            groups = reader.get_tag(_feed, "groups", [])
            groups.remove(uuid)
            reader.set_tag(_feed, "groups", groups)

    # Remove the feed if it is no longer used by any groups
    for _feed in reader.get_feeds():
        if not reader.get_tag(_feed, "groups", []):
            reader.delete_feed(_feed)
            logger.info(f"Removed feed {_feed} due to no groups using it")

    # Redirect to the index page.
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/mark_as_unread/{uuid}")
async def mark_as_unread(uuid: str):  # noqa: ANN201, C901
    """Mark a feed as unread.

    Args:
        uuid: The uuid of the feed to mark as unread.

    Returns:
        The index page. Or an error page if the feed could not be marked as unread.
    """
    logger.info(f"Marking feed {uuid} as unread")

    # Get the group
    group: Group = get_group(reader, uuid)

    if not group.rss_feeds:
        return HTMLResponse(f"No RSS feeds found for group {uuid}")

    # Get the feed
    _feed = reader.get_feed(group.rss_feeds[0], None)
    logger.info(f"Feed is {_feed}")

    # Update the feed
    reader.update_feeds(feed=_feed, workers=4)

    # Get the entries
    entries = reader.get_entries(feed=_feed)
    entries = list(entries)

    if not entries:
        # TODO: Return a proper error page
        return HTMLResponse(f"Failed to mark feed {uuid} as unread. No entries found.")

    # Mark the entry as unread
    entry: EntryLike | Entry
    for entry in entries:
        reader.mark_entry_as_unread(entry)

    for entry in entries:
        if group.whitelist_enabled and not whitelisted(group, entry):
            logger.info(f"Skipping entry {entry} as it is not whitelisted")
            reader.mark_entry_as_read(entry)
            continue

        if group.blacklist_enabled and blacklisted(group, entry):
            logger.info(f"Skipping entry {entry} as it is blacklisted")
            reader.mark_entry_as_read(entry)
            continue

        if not group.send_retweets and entry.title.startswith("RT by "):
            logger.info(f"Skipping entry {entry} as it is a retweet")
            reader.mark_entry_as_read(entry)
            continue

        if not group.send_replies and entry.title.startswith("R to "):
            logger.info(f"Skipping entry {entry} as it is a reply")
            reader.mark_entry_as_read(entry)
            continue

        if group.only_send_if_media and not has_media(entry):
            logger.info(f"Skipping entry {entry} as it has no media attached")
            reader.mark_entry_as_read(entry)
            continue

        if group.send_as_link:
            send_link(entry=entry, group=group)
        if group.send_as_text:
            send_text(entry=entry, group=group)
        if group.send_as_embed:
            send_embed(entry=entry, group=group)

        # Mark the entry as read
        reader.mark_entry_as_read(entry)

    # Redirect to the index page.
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/settings")
async def settings(request: Request) -> Response:
    """Get the settings page.

    Args:
        request: The request object.

    Returns:
        Response: The settings page.
    """
    application_settings: ApplicationSettings = get_app_settings(reader)
    return templates.TemplateResponse("settings.html", {"request": request, "settings": application_settings})


@functools.lru_cache(maxsize=1)
@app.get("/favicon.svg")
async def favicon():  # noqa: ANN201
    """Get the favicon.

    Returns:
        Response: The favicon.
    """
    svg = """
    <svg xmlns="http://www.w3.org/2000/svg">
        <text x="50%" y="50%" dy=".3em" text-anchor="middle">🐦</text>
    </svg>
    """
    return Response(content=svg, media_type="image/svg+xml")


@app.post("/settings")
async def settings_post(
    request: Request,
    nitter_instance: Annotated[str, Form(title="Nitter instance")] = "",
    deepl_auth_key: Annotated[str, Form(title="DeepL auth key")] = "",
    piped_instance: Annotated[str, Form(title="Piped instance")] = "",
    teddit_instance: Annotated[str, Form(title="Teddit instance")] = "",
) -> Response:
    """Save the settings.

    Args:
        request: The request object.
        nitter_instance: The Nitter instance to use.
        deepl_auth_key: The DeepL auth key to use.
        piped_instance: The Piped instance to use.
        teddit_instance: The Teddit instance to use.
    """
    # TODO: Run reader.change_feed_url() on all feeds if the Nitter instance has changed.
    app_settings = ApplicationSettings(
        nitter_instance=nitter_instance,
        deepl_auth_key=deepl_auth_key,
        piped_instance=piped_instance,
        teddit_instance=teddit_instance,
    )

    set_app_settings(reader, app_settings)
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "settings": app_settings,
        },
    )


@app.on_event("startup")
def startup() -> None:
    """This is called when the server starts.

    It adds missing tags and starts the scheduler.
    """
    log_format: str = "<green>{time:YYYY-MM-DD at HH:mm:ss}</green> <level>{level: <5}</level> <white>{message}</white>"
    logger.remove()
    logger.add(
        sys.stderr,
        format=log_format,
        level="DEBUG",
        colorize=True,
        backtrace=False,
        diagnose=False,
        catch=True,
    )

    scheduler: BackgroundScheduler = BackgroundScheduler()

    # Check for new entries every 15 minutes. They will be sent to Discord if they are new.
    # TODO: Make minutes configurable in the settings as your Nitter instance might update more often.
    scheduler.add_job(sched_func, "interval", minutes=15, next_run_time=datetime.now(tz=timezone.utc))
    scheduler.start()


def sched_func() -> None:
    """The scheduler can't call a function with arguments, so we need to wrap it."""
    send_to_discord(reader)


def start() -> None:
    """Start the server."""
    logger.info("Starting server, you can access it at http://localhost:8000")
    uvicorn.run(app, port=8000)


if __name__ == "__main__":
    start()
