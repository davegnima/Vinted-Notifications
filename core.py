import functools
import html
import os
import re
import time
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

import db
import proxies
import requests
from pyVintedVN import Vinted, requester
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from logger import get_logger

# Get logger for this module
logger = get_logger(__name__)

# Vinted stopped publishing listing times in September 2026, so "is this item new?"
# is answered purely by "have we recorded its id before?". That makes a wiped or
# freshly seeded database the only real flood risk, and this caps the blast radius.
MAX_NOTIFICATIONS_PER_RUN = 10

# Night pause (opt-in per instance, 2026-10-08): between NIGHT_PAUSE_FROM and NIGHT_PAUSE_TO (hours, Europe/Rome)
# process_items does nothing, so no request reaches Vinted and no proxy bandwidth is spent. The first run after the
# pause only records what is on the first page, without notifying, so the night backlog is not replayed.
# Both variables empty (default) = no pause.
NIGHT_PAUSE_FROM = os.environ.get("NIGHT_PAUSE_FROM", "").strip()
NIGHT_PAUSE_TO = os.environ.get("NIGHT_PAUSE_TO", "").strip()


def in_night_pause(now=None):
    """True if the Italian hour is inside [NIGHT_PAUSE_FROM, NIGHT_PAUSE_TO), also across midnight."""
    if not NIGHT_PAUSE_FROM or not NIGHT_PAUSE_TO:
        return False
    try:
        start, end = int(NIGHT_PAUSE_FROM), int(NIGHT_PAUSE_TO)
    except ValueError:
        return False
    hour = (now or datetime.now(ZoneInfo("Europe/Rome"))).hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


# True while paused; starts True if the process is started inside the pause window.
_pause_state = {"paused": in_night_pause()}


def process_query(query, name=None):
    """
    Process a Vinted query URL by:
    1. Checking if the URL is a brand URL and converting it to standard format if needed
    2. Parsing the URL and extracting query parameters
    3. Ensuring the order flag is set to "newest_first"
    4. Removing time and search_id parameters
    5. Rebuilding the query string and URL
    6. Checking if the query already exists in the database
    7. Adding the query to the database if it doesn't exist

    Args:
        query (str): The Vinted query URL
        name (str, optional): A name for the query. If provided, it will be used as the query name.

    Returns:
        tuple: (message, is_new_query)
            - message (str): Status message
            - is_new_query (bool): True if query was added, False if it already existed
    """
    # Check if the URL is a brand URL (format: url/brand/id-name)
    parsed_url = urlparse(query)
    path_parts = parsed_url.path.strip("/").split("/")

    if len(path_parts) >= 2 and path_parts[0] == "brand":
        # Extract the brand ID from the format "id-name"
        brand_id_with_name = path_parts[1]
        brand_id = brand_id_with_name.split("-")[0]

        # Create a new URL with the standard format
        new_path = "/catalog"
        new_query_params = {"brand_ids[]": [brand_id]}
        new_query_string = urlencode(new_query_params, doseq=True)

        # Rebuild the URL
        query = urlunparse(
            (parsed_url.scheme, parsed_url.netloc, new_path, "", new_query_string, "")
        )
        logger.info(f"Converted brand URL to standard format: {query}")

        # Parse the URL and extract the query parameters
        parsed_url = urlparse(query)

    query_params = parse_qs(parsed_url.query)

    # Ensure the order flag is set to newest_first
    query_params["order"] = ["newest_first"]
    # Remove time and search_id if provided
    query_params.pop("time", None)
    query_params.pop("search_id", None)
    query_params.pop("disabled_personalization", None)
    query_params.pop("page", None)

    # Rebuild the query string and the entire URL
    new_query = urlencode(query_params, doseq=True)
    processed_query = urlunparse(
        (
            parsed_url.scheme,
            parsed_url.netloc,
            parsed_url.path,
            parsed_url.params,
            new_query,
            parsed_url.fragment,
        )
    )

    # Some queries are made with filters only, so we need to check if the search_text is present
    if db.is_query_in_db(processed_query) is True:
        return "Query already exists.", False
    else:
        # add the query to the db
        db.add_query_to_db(processed_query, name)
        return "Query added.", True


def get_formatted_query_list():
    """
    Get a formatted list of all queries in the database.

    Returns:
        str: A formatted string with all queries, numbered
    """
    all_queries = db.get_queries()
    queries_keywords = []
    for query in all_queries:
        parsed_url = urlparse(query[1])
        query_params = parse_qs(parsed_url.query)

        # Get the name or Extract the value of 'search_text'
        query_name = (
            query[3]
            if query[3] is not None
            else query_params.get("search_text", [None])[0]
        )

        if query_name[0] is None:
            # Use query text instead of the whole query object
            queries_keywords.append([query[1]])
        else:
            queries_keywords.append(query_name)

    query_list = ("\n").join(
        [str(i + 1) + ". " + j for i, j in enumerate(queries_keywords)]
    )
    return query_list


def process_remove_query(number):
    """
    Process the removal of a query from the database.

    Args:
        number (str): The number of the query to remove or "all" to remove all queries

    Returns:
        tuple: (message, success)
            - message (str): Status message
            - success (bool): True if query was removed successfully
    """
    if number == "all":
        db.remove_all_queries_from_db()
        return "All queries removed.", True

    # Check if number is a valid digit
    if number.isdigit():
        # Remove the query from the database
        db.remove_query_from_db(number)
        return "Query removed.", True
    else:
        return "Invalid number.", False


def process_update_query(query_id, query, name):
    """
    Process the update of a query in the database.

    Args:
        query_id (int): The ID of the query to update
        query (str): The new Vinted query URL
        name (str, optional): A new name for the query. If provided, it will be used as the query name.

    Returns:
        tuple: (message, success)
            - message (str): Status message
            - success (bool): True if query was updated successfully
    """
    # Parse the URL and extract the query parameters
    parsed_url = urlparse(query)
    query_params = parse_qs(parsed_url.query)

    # Ensure the order flag is set to newest_first
    query_params["order"] = ["newest_first"]
    # Remove time and search_id if provided
    query_params.pop("time", None)
    query_params.pop("search_id", None)
    query_params.pop("disabled_personalization", None)
    query_params.pop("page", None)

    # Rebuild the query string and the entire URL
    new_query = urlencode(query_params, doseq=True)
    processed_query = urlunparse(
        (
            parsed_url.scheme,
            parsed_url.netloc,
            parsed_url.path,
            parsed_url.params,
            new_query,
            parsed_url.fragment,
        )
    )

    # Update the query in the database
    if db.update_query_in_db(query_id, processed_query, name):
        return "Query updated.", True
    else:
        return "Failed to update query.", False


def process_add_country(country):
    """
    Process the addition of a country to the allowlist.

    Args:
        country (str): The country code to add

    Returns:
        tuple: (message, country_list)
            - message (str): Status message
            - country_list (list): Current list of allowed countries
    """
    # Format the country code (remove spaces)
    country = country.replace(" ", "")
    country_list = db.get_allowlist()

    # Validate the country code (check if it's 2 characters long)
    if len(country) != 2:
        return "Invalid country code", country_list

    # Check if the country is already in the allowlist
    # If country_list is 0, it means the allowlist is empty
    if country_list != 0 and country.upper() in country_list:
        return f'Country "{country.upper()}" already in allowlist.', country_list

    # Add the country to the allowlist
    db.add_to_allowlist(country.upper())
    return "Country added.", db.get_allowlist()


def process_remove_country(country):
    """
    Process the removal of a country from the allowlist.

    Args:
        country (str): The country code to remove

    Returns:
        tuple: (message, country_list)
            - message (str): Status message
            - country_list (list): Current list of allowed countries
    """
    # Format the country code (remove spaces)
    country = country.replace(" ", "")

    # Validate the country code (check if it's 2 characters long)
    if len(country) != 2:
        return "Invalid country code", db.get_allowlist()

    # Remove the country from the allowlist
    db.remove_from_allowlist(country.upper())
    return "Country removed.", db.get_allowlist()


def get_user_country(profile_id):
    """
    Get the country code for a Vinted user.

    Makes an API request to retrieve the user's country code.
    Handles rate limiting by trying an alternative endpoint.

    Args:
        profile_id (str): The Vinted user's profile ID

    Returns:
        str: The user's country code (2-letter ISO code) or "XX" if it can't be determined
    """
    # Users are shared between all Vinted platforms, so we can use whatever locale we want
    url = f"https://www.vinted.fr/api/v2/users/{profile_id}?localize=false"
    response = requester.get(url)
    # That's a LOT of requests, so if we get a 429 we wait a bit before retrying once
    if response.status_code == 429:
        # In case of rate limit, we're switching the endpoint. This one is slower, but it doesn't RL as soon.
        # We're limiting the items per page to 1 to grab as little data as possible
        url = f"https://www.vinted.fr/api/v2/users/{profile_id}/items?page=1&per_page=1"
        response = requester.get(url)
        try:
            user_country = response.json()["items"][0]["user"]["country_iso_code"]
        except KeyError:
            logger.warning(
                "Couldn't get the country due to too many requests. Returning default value."
            )
            user_country = "XX"
    else:
        user_country = response.json()["user"]["country_iso_code"]
    return user_country


def process_items(queue):
    """
    Process all queries from the database, search for items, and put them in the queue.
    Uses the global items_queue by default, but can accept a custom queue for backward compatibility.

    Args:
        queue (Queue, optional): The queue to put the items in. Defaults to the global items_queue.

    Returns:
        None
    """

    if in_night_pause():
        if not _pause_state["paused"]:
            logger.info("Night pause started: no requests to Vinted until it ends")
        _pause_state["paused"] = True
        return
    silent_resume = _pause_state["paused"]
    if silent_resume:
        logger.info("Night pause ended: recording the first page without notifying")
        _pause_state["paused"] = False

    all_queries = db.get_queries()

    # Initialize Vinted
    vinted = Vinted()

    # Get the number of items per query from the database
    items_per_query = int(db.get_parameter("items_per_query"))

    # for each keyword we parse data
    for query in all_queries:
        all_items = vinted.items.search(query[1], nbr_items=items_per_query)
        # TEMP DIAGNOSTIC: `requester` is a module-level singleton (see
        # requester.py: "requester = Requester()" at import time), shared by
        # every Vinted()/Items() instance in this process. Its self.session
        # (cookies AND proxy) is never recreated between cycles - only
        # session.proxies gets overwritten per-call by proxies.configure_proxy().
        # Logging it here tells us which proxy actually served this exact
        # query, so we can check whether the tracker keeps landing on the same
        # (possibly broken/self-caching) proxy for these specific queries.
        # Remove once diagnosed.
        try:
            session_proxies = proxies.mask_proxy(dict(requester.session.proxies))
        except Exception as e:
            session_proxies = f"<error reading proxies: {e}>"
        # Filter to only include new items. This should reduce the amount of db calls.
        data = [item for item in all_items if item.is_new_item()]
        queue.put((data, query[0], silent_resume))
        ids_this_cycle = [item.id for item in data]
        logger.info(
            f"Scraped {len(data)} items for query {query[0]}: ids={ids_this_cycle} "
            f"proxy_used={session_proxies}"
        )


_CATALOG_KEYS_LOGGED = [False]


def log_catalog(item, query_id):
    """Diagnostic (LOG_CATALOG=1): which Vinted catalogue id each new item sits in.

    The ids needed to narrow a search to its category (catalog[]=...) are not in
    the tree the user exported for electronics, appliances, sport; the API payload
    may carry them, so log them next to the title. The raw keys go out once, so a
    payload without the id is visible too.
    """
    if os.environ.get("LOG_CATALOG", "").strip() not in ("1", "true"):
        return
    raw = item.raw_data or {}
    if not _CATALOG_KEYS_LOGGED[0]:
        _CATALOG_KEYS_LOGGED[0] = True
        logger.info(f"CATALOGO keys={sorted(raw.keys())}")
        logger.info(
            f"CATALOGO tracking={str(raw.get('search_tracking_params'))[:400]} "
            f"source={raw.get('content_source')} item_box={str(raw.get('item_box'))[:300]}"
        )
    cat = raw.get("catalog_id")
    if cat is None:
        cat = (
            (raw.get("catalog") or {}).get("id")
            if isinstance(raw.get("catalog"), dict)
            else None
        )
    tracking = raw.get("search_tracking_params") or {}
    if cat is None and isinstance(tracking, dict):
        cat = (
            tracking.get("catalog_id")
            or tracking.get("catalog_ids")
            or tracking.get("category_id")
        )
    logger.info(
        f"CATALOGO q={query_id} item={item.id} cat={cat} price={item.price} {item.title[:60]}"
    )


# Verifica radar (25/10): i modelli della lista con prezzo sopra il buy max (🔴) passano al worker,
# che manda il semaforo al chat privato. Spento con RADAR_INVIA_ROSSI=0 (default).
RADAR_INVIA_ROSSI = os.environ.get("RADAR_INVIA_ROSSI", "").strip() in ("1", "true")


def clear_item_queue(items_queue, new_items_queue):
    """
    Process items from the items_queue.
    This function is scheduled to run frequently.
    """
    if not items_queue.empty():
        data, query_id, silent = items_queue.get()
        banwords_str = db.get_parameter("banwords")

        # Read the watermark once, before the loop. It doubles as the "has this query
        # ever produced anything?" flag, and the updates made below would otherwise
        # cut a first-run priming pass short right after the first item.
        last_query_timestamp = db.get_last_timestamp(query_id)
        is_first_run = last_query_timestamp is None or silent
        if is_first_run:
            logger.info(
                f"First run for query {query_id}: recording {len(data)} item(s) "
                f"without notifying, so the existing catalogue is not replayed."
            )

        to_notify = []
        # Strict mode (TITLE_MUST_MATCH_QUERY=1) requires the search text's words in
        # the title; a model list in the query name ("Cameras :: contax t2 | nikon
        # 35ti") then keeps only titles matching one of the models.
        models = query_models(query_id)
        study = is_study_query(query_id)
        required_words = (
            search_words(query_info(query_id)[1])
            if os.environ.get("TITLE_MUST_MATCH_QUERY", "").strip() in ("1", "true")
            else []
        )
        # TEMP DIAGNOSTIC counters. Remove once diagnosed.
        skipped_already_in_db = []
        # Motivo di scarto di ogni annuncio della batch, per il log di fine batch.
        drops = []
        for item in reversed(data):

            # The watermark is only meaningful when the API actually supplied a
            # listing time. Otherwise raw_timestamp is merely when we saw the item,
            # and comparing it against the watermark would discard every new item.
            if (
                item.has_real_timestamp
                and last_query_timestamp is not None
                and last_query_timestamp >= item.raw_timestamp
            ):
                drops.append("watermark")
                continue
            # In case of multiple queries, we need to check if the item is already in the db
            if db.is_item_in_db_by_id(item.id) is True:
                # We update the timestamp
                db.update_last_timestamp(query_id, item.raw_timestamp)
                skipped_already_in_db.append(item.id)
                drops.append("gia_in_db")
                continue
            log_catalog(item, query_id)
            # If there's an allowlist and
            # If the user's country is not in the allowlist, we just update the timestamp
            if db.get_allowlist() != 0 and (
                get_user_country(item.raw_data["user"]["id"])
            ) not in (db.get_allowlist() + ["XX"]):
                db.update_last_timestamp(query_id, item.raw_timestamp)
                drops.append("paese")
                continue
            # Strict mode (TITLE_MUST_MATCH_QUERY=1): every word of the query's
            # search text must appear in the title. Vinted's text search is fuzzy
            # ("contax t2" returns any camera), so rare-model queries need this.
            if required_words and not title_has_words(item.title, required_words):
                db.update_last_timestamp(query_id, item.raw_timestamp)
                drops.append("parole_titolo")
                continue
            # With a model list, only titles naming a listed model whose price can
            # still reach the target (not 🔴) are notified; study queries record the
            # others too, so their volumes can be measured.
            luce = traffic_light(best_model(item.title, models), item.price) if models else None
            if models and luce in (None, "🔴") and not (RADAR_INVIA_ROSSI and not study and luce == "🔴"):
                if study:
                    db.add_item_to_db(
                        id=item.id,
                        timestamp=item.raw_timestamp,
                        price=item.price,
                        title=item.title,
                        photo_url=item.photo,
                        query_id=query_id,
                        currency=item.currency,
                        brand=item.brand_title,
                    )
                db.update_last_timestamp(query_id, item.raw_timestamp)
                drops.append("fuori_modello_o_rosso")
                continue
            # Check if the item title contains any banwords
            if banwords_str and contains_banwords(item.title, banwords_str):
                # If it contains banwords, just update the timestamp and skip
                db.update_last_timestamp(query_id, item.raw_timestamp)
                drops.append("banwords")
                continue

            # Per-brand banwords (opt-in via BANWORDS_BRAND): same handling as the global banwords
            if BRAND_BANWORDS and contains_brand_banwords(
                item.title, item.brand_title, BRAND_BANWORDS
            ):
                db.update_last_timestamp(query_id, item.raw_timestamp)
                drops.append("banwords_brand")
                continue

            # Being recorded is what stops an item coming back next run, so every
            # item that reaches this point is written to the db whether or not it
            # ends up being announced.
            to_notify.append(item)
            db.add_item_to_db(
                id=item.id,
                timestamp=item.raw_timestamp,
                price=item.price,
                title=item.title,
                photo_url=item.photo,
                query_id=query_id,
                currency=item.currency,
                brand=item.brand_title,
            )

        # TEMP DIAGNOSTIC: summarize what happened to this batch, so we can see
        # whether items are being silently deduped (already_in_db keeps
        # matching ids that should be new) versus never being fetched at all
        # (the gap between consecutive "Scraped ... ids=[...]" log lines is
        # bigger than items_per_query, meaning the API's newest_first window
        # already skipped past them before this code ever saw them).
        # Remove once diagnosed.
        logger.info(
            f"clear_item_queue for query {query_id}: batch_size={len(data)} "
            f"already_in_db={skipped_already_in_db} to_notify={[i.id for i in to_notify]} "
            f"is_first_run={is_first_run} "
            f"scarti={ {k: drops.count(k) for k in sorted(set(drops))} }"
        )

        if is_first_run:
            return

        # data arrives newest first and is walked in reverse, so to_notify runs
        # oldest to newest. Trim from the front: a backlog is worth dropping, the
        # listings that just appeared are not.
        if len(to_notify) > MAX_NOTIFICATIONS_PER_RUN:
            logger.warning(
                f"{len(to_notify)} unseen items for query {query_id}; notifying the "
                f"{MAX_NOTIFICATIONS_PER_RUN} most recent and recording the rest silently."
            )
            to_notify = to_notify[-MAX_NOTIFICATIONS_PER_RUN:]

        # The query that found the items: its name goes in the message text and
        # its search URL in a second button, so whoever reads the notification
        # (person or bot) knows which search produced it.
        query_name, query_url = query_info(query_id)

        for item in to_notify:
            # We create the message
            message_template = db.get_parameter("message_template")
            content = message_template.format(
                title=item.title,
                price=str(item.price) + " " + item.currency,
                brand=item.brand_title,
                image=None if item.photo is None else item.photo,
            )
            if query_name and query_url:
                content += (
                    f'\n🔎 Query: <a href="{html.escape(query_url, quote=True)}">'
                    f"{html.escape(query_name)}</a>"
                )
            elif query_name:
                content += f"\n🔎 Query: {html.escape(query_name)}"
            if models:
                matched = best_model(item.title, models)
                content += model_line(matched, item.price)
            # add the item to the queue
            # photo_url is passed separately (not just embedded in the {image}
            # placeholder inside content) so the Telegram side can send it as a
            # real photo attachment (send_photo) instead of relying on Telegram's
            # link-preview mechanism, which only ever renders a small thumbnail.
            new_items_queue.put(
                (
                    content,
                    item.url,
                    "Open Vinted",
                    query_url,
                    "🔎 Query" if query_url else None,
                    item.photo,
                )
            )
            # new_items_queue.put((content, item.url, "Open Vinted", item.buy_url, "Open buy page", item.photo))


_QUERY_ROWS = {"at": 0.0, "rows": {}}
QUERY_ROWS_TTL = 20  # seconds; names carry model lists of tens of kilobytes


def _query_row(query_id):
    """The query's db row (id, url, last_item, name), None if it is gone; rows are re-read at most every 20 s."""
    now = time.monotonic()
    if now - _QUERY_ROWS["at"] > QUERY_ROWS_TTL or query_id not in _QUERY_ROWS["rows"]:
        _QUERY_ROWS["rows"] = {row[0]: row for row in db.get_queries() or []}
        _QUERY_ROWS["at"] = now
    return _QUERY_ROWS["rows"].get(query_id)


def query_info(query_id):
    """
    Name and search URL of a query, for the notification.

    Returns (name, url); the name falls back to "query <id>" and both are None
    if the query can no longer be found (e.g. removed while items were queued).
    """
    row = _query_row(query_id)
    if row is None:
        return None, None
    return (split_query_name(row[3])[0] or f"query {query_id}"), row[1]


MODELS_SEPARATOR = "::"
STUDY_PREFIX = "STUDIO"


def is_study_query(query_id):
    """True for a query named "STUDIO ...": red and unmatched items are recorded, not dropped."""
    row = _query_row(query_id)
    return row is not None and (row[3] or "").strip().upper().startswith(STUDY_PREFIX)


@functools.lru_cache(maxsize=512)
def split_query_name(raw_name):
    """
    Split a query name into (display name, models).

    "Cameras :: contax t2=850/331 | nikon 35ti | big mini=45/0" gives "Cameras"
    and one model per entry; a name without "::" has no models. A model is
    {"label", "words", "resale", "buy_max"}: the words must all be in the title
    (see title_has_words); "=resale/buy_max" in euros is optional ("?" for an
    unknown buy_max, 0 when no price gives the target margin); a trailing "*"
    marks prices that are an estimate rather than measured from sales. After the
    prices, "!word,other words" lists words that mean the title is a different
    product ("fire emblem gamecube=153/55!advance,sacred stones"); an entry that
    is only "!word,other words" excludes those words for every model of the query.
    """
    if not raw_name or MODELS_SEPARATOR not in raw_name:
        return raw_name, []
    display, _, models_text = raw_name.partition(MODELS_SEPARATOR)
    models, common = [], []
    for entry in re.split(r"[|\n]", models_text):
        if entry.strip().startswith("!"):
            common += [
                e for e in (_normalise(x) for x in entry.strip()[1:].split(",")) if e
            ]
            continue
        label, _, prices = entry.partition("=")
        prices, _, excluded = prices.partition("!")
        words = _normalise(label).split()
        if not words:
            continue
        estimate = prices.strip().endswith("*")
        resale, _, buy = prices.strip().rstrip("*").partition("/")
        models.append(
            {
                "label": " ".join(words),
                "words": words,
                "resale": _euros(resale),
                "buy_max": _euros(buy),
                "estimate": estimate,
                "excluded": [
                    e for e in (_normalise(x) for x in excluded.split(",")) if e
                ],
            }
        )
    for m in models:
        m["excluded"] += common
    return display.strip(), models


def _euros(text):
    text = (text or "").strip()
    return int(float(text)) if re.fullmatch(r"\d+(\.\d+)?", text) else None


def best_model(title, models):
    """
    The most specific model the title matches, None if none: one whose words
    are all whole title words beats one matched only as a word start ("x100v"
    beats "fujifilm x100" on "Fujifilm X100V"), then most words, then longest,
    then a priced one.
    """
    norm = f" {_normalise(title)} "
    matched = [
        m
        for m in models
        if title_has_words(title, m["words"])
        and not any(f" {e} " in norm for e in m.get("excluded", ()))
    ]
    if not matched:
        return None
    tokens = set(_normalise(title).split())
    return max(
        matched,
        key=lambda m: (
            all(w in tokens for w in m["words"]),
            len(m["words"]),
            len("".join(m["words"])),
            m["resale"] is not None,
        ),
    )


TOLERANCE_OVER_BUY_MAX = 1.15
UNCERTAIN_PRICE_CAP = 50


def traffic_light(model, price):
    """
    🟢 price within buy_max (target margin and ROI reachable), 🟡 up to 15% over
    (worth a counter-offer), 🔴 over that or no price reaches the target, ⚪ no
    data; None when no model matched.
    """
    if model is None:
        return None
    try:
        price = float(price)
    except (TypeError, ValueError):
        price = None
    buy = model["buy_max"]
    if buy is None and model["resale"] is not None and price is not None:
        # Uncertain resale: no buy max to trust, so only cheap listings are worth a look.
        return "⚪" if price <= UNCERTAIN_PRICE_CAP else "🔴"
    if model["resale"] is None or buy is None or price is None:
        return "🔴" if buy == 0 else "⚪"
    if buy <= 0 or price > buy * TOLERANCE_OVER_BUY_MAX:
        return "🔴"
    return "🟢" if price <= buy else "🟡"


def model_line(model, price):
    """
    Notification line: which model the title matched and how the price compares.

    🟢 price within buy_max (target margin and ROI reachable), 🟡 up to 15% over
    (worth a counter-offer), 🔴 over that or no price reaches the target, ⚪ no data.
    """
    if model is None:
        return ""
    label = html.escape(model["label"])
    try:
        price = float(price)
    except (TypeError, ValueError):
        price = None
    resale, buy = model["resale"], model["buy_max"]
    if resale is None:
        return f"\n⚪ Modello: {label} · modello preciso non riconosciuto"
    line = f"\n{{}} Modello: {label} · rivendita ~{resale} €"
    if model.get("estimate"):
        line += " (stima)"
    if buy is None:
        return line.format("⚪") + " · stima poco sicura"
    if buy <= 0:
        return line.format("🔴") + " · nessun prezzo rende"
    line += f" · buy max {buy} €"
    if price is None:
        return line.format("⚪")
    if price <= buy:
        return line.format("🟢")
    if price <= buy * TOLERANCE_OVER_BUY_MAX:
        return line.format("🟡") + " · da trattare"
    return line.format("🔴")


def query_models(query_id):
    """Models listed in the query's name; [] if it has none."""
    row = _query_row(query_id)
    return split_query_name(row[3])[1] if row is not None else []


def _normalise(text):
    # Accents dropped ("Pokémon" -> "pokemon", "découpe" -> "decoupe"), as in the bot's model table.
    text = (
        unicodedata.normalize("NFKD", (text or "").lower())
        .encode("ascii", "ignore")
        .decode()
    )
    return " ".join(re.sub(r"[^0-9a-z]+", " ", text).split())


def search_words(query_url):
    """Words of a query's search_text, normalised; [] if there is none."""
    if not query_url:
        return []
    text = " ".join(parse_qs(urlparse(query_url).query).get("search_text", []))
    return _normalise(text).split()


def title_has_words(title, words):
    """
    True if every word is in the title, as a whole word or followed only by a
    model suffix ("gr" -> "gr1v", "mju" -> "mjuii", "fm2" -> "fm2n"), or if the
    words written together are a run of whole title words ("gameboy" for
    "game boy", "mjuii" for "mju ii"). "go" does not match "goonies" and "x10"
    does not match "sx100".
    """
    tokens = _normalise(title).split()
    if all(any(_word_matches(t, w) for t in tokens) for w in words):
        return True
    joined = "".join(words)
    for i in range(len(tokens)):
        run = ""
        for t in tokens[i:]:
            run += t
            if _word_matches(run, joined):
                return True
            if len(run) >= len(joined):
                break
    return False


_MODEL_SUFFIX = re.compile(r"(\d.*|i{1,3}|iv|v|s|n|x|mk\w*)")


def _word_matches(token, word):
    if token == word:
        return True
    return (
        len(word) >= 2
        and token.startswith(word)
        and _MODEL_SUFFIX.fullmatch(token[len(word) :]) is not None
    )


def _fold(text):
    """Lowercase without accents ("Courrèges" -> "courreges")."""
    return "".join(
        c
        for c in unicodedata.normalize("NFKD", (text or "").lower())
        if not unicodedata.combining(c)
    )


def parse_brand_banwords(spec):
    """
    Parse BANWORDS_BRAND (opt-in per instance): rules separated by ";;", each "brand1,brand2::word1|||word2".
    Returns a list of (set of folded brands, list of folded words).
    """
    rules = []
    for rule in (spec or "").split(";;"):
        brands, sep, words = rule.partition("::")
        if not sep:
            continue
        brand_set = {_fold(b).strip() for b in brands.split(",") if b.strip()}
        word_list = [_fold(w).strip() for w in words.split("|||") if w.strip()]
        if brand_set and word_list:
            rules.append((brand_set, word_list))
    return rules


def contains_brand_banwords(title, brand, rules):
    """True if the item's brand has a rule and the title contains one of its words (accent/case-insensitive).
    A word starting with "=" must match as a whole word ("=top" matches "Top loewe", not "laptop").
    """
    brand_folded = _fold(brand).strip()
    title_folded = _fold(title)
    for brand_set, words in rules:
        if brand_folded in brand_set and any(
            (
                re.search(r"\b" + re.escape(w[1:]) + r"\b", title_folded)
                if w.startswith("=")
                else w in title_folded
            )
            for w in words
        ):
            return True
    return False


BRAND_BANWORDS = parse_brand_banwords(os.environ.get("BANWORDS_BRAND", ""))


def contains_banwords(title, banwords_str):
    """
    Check if a title contains any banwords.

    Args:
        title (str): The title to check
        banwords_str (str): List of banwords separated by 3 pipe character
    Returns:
        bool: True if the title contains any banwords, False otherwise
    """

    # Split the banwords string into a list using pipe as delimiter
    banwords = [
        word.strip().lower() for word in banwords_str.split("|||") if word.strip()
    ]

    # If the list is empty, return False
    if not banwords:
        return False

    # Check if any banword is in the title (case-insensitive)
    title_lower = title.lower()
    for word in banwords:
        if word in title_lower:
            return True

    return False


def check_version():
    """
    Check if the application is up to date
    """
    try:
        # Get URL from the database
        github_url = db.get_parameter("github_url")
        # Get version from the database
        ver = db.get_parameter("version")
        # Get latest version from the repository
        url = f"{github_url}/releases/latest"
        response = requests.get(url)

        if response.status_code == 200:
            latest_version = response.url.split("/")[-1]
            is_up_to_date = ver == latest_version
            return is_up_to_date, ver, latest_version, github_url
        else:
            # If we can't check, assume it's up to date
            return True, ver, ver, github_url
    except Exception as e:
        logger.error(f"Error checking for new version: {str(e)}", exc_info=True)
        # If we can't check, assume it's up to date
        return True, ver, ver, github_url
