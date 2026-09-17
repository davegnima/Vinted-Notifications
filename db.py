import sqlite3
from traceback import print_exc
from logger import get_logger

logger = get_logger(__name__)

DB_PATH = "./data/vinted_notifications.db"

# core.py's scraper and item-extractor run as separate OS processes (see
# vinted_notifications.py) and each opens its own short-lived sqlite3
# connection per call. SQLite serializes writers at the file level, so under
# load (5 queries x 3s refresh, two processes hitting the same file) a writer
# can be told "database is locked" by the OS-level lock instead of getting in.
# Every db.py function used to swallow that with print_exc(), which goes to
# stderr - invisible if only the app logger's output is watched on Railway.
# This connects with a busy_timeout so sqlite retries internally for a bit
# before giving up, and logs any exception that still escapes via the app's
# own logger so a locked-database failure is no longer silent.
DB_BUSY_TIMEOUT_MS = 5000


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=DB_BUSY_TIMEOUT_MS / 1000)
    conn.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
    return conn


def get_db_connection():
    conn = _connect()
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_brand_column():
    """
    Add the 'brand' column to the items table if it isn't there yet.

    The items table is created with 'CREATE TABLE IF NOT EXISTS', so on an
    existing database (any install from before this change) adding a column
    to initial_db.sql alone has no effect - the table already exists and
    that statement is skipped. This runs an explicit ALTER TABLE instead,
    guarded by a check against the actual columns so it's safe to call on
    every startup: it's a no-op once the column exists, and it never touches
    already-stored rows (they just get NULL for brand until re-scraped).
    """
    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(items)")
        existing_columns = {row[1] for row in cursor.fetchall()}
        if "brand" not in existing_columns:
            cursor.execute("ALTER TABLE items ADD COLUMN brand TEXT")
            conn.commit()
    except Exception as e:
        logger.error(f"db.ensure_brand_column failed: {e}", exc_info=True)
        print_exc()
    finally:
        if conn:
            conn.close()


def create_or_update_sqlite_db(db_path):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # Using the sql script
        with open(db_path, "r", encoding="utf-8") as sql_file:
            sql_script = sql_file.read()
            cursor.executescript(sql_script)

        conn.commit()
    except Exception as e:
        logger.error(f"db.create_or_update_sqlite_db failed: {e}", exc_info=True)
        print_exc()
    finally:
        if conn:
            conn.close()

    # Runs on every startup, right after the CREATE TABLE IF NOT EXISTS pass
    # above. On a fresh db it's a no-op (the column doesn't exist yet only
    # because initial_db.sql hasn't been updated with it - the ALTER TABLE
    # below handles both fresh and pre-existing databases the same way).
    ensure_brand_column()


def is_item_in_db_by_id(id):
    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT() FROM items WHERE item=?", (id,))
        if cursor.fetchone()[0]:
            return True
        return False
    except Exception as e:
        logger.error(f"db.is_item_in_db_by_id failed: {e}", exc_info=True)
        print_exc()
    finally:
        if conn:
            conn.close()


def get_last_timestamp(query_id):
    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor()
        cursor.execute("SELECT last_item FROM queries WHERE id=?", (query_id,))
        result = cursor.fetchone()
        if result:
            return result[0]
        return None
    except Exception as e:
        logger.error(f"db.get_last_timestamp failed: {e}", exc_info=True)
        print_exc()
        return None
    finally:
        if conn:
            conn.close()


def update_last_timestamp(query_id, timestamp):
    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE queries SET last_item=? WHERE id=?", (timestamp, query_id)
        )
        conn.commit()
    except Exception as e:
        logger.error(f"db.update_last_timestamp failed: {e}", exc_info=True)
        print_exc()
    finally:
        if conn:
            conn.close()


def add_item_to_db(
    id, title, query_id, price, timestamp, photo_url, currency="EUR", brand=None
):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # Insert into db the id and the query_id related to the item
        cursor.execute(
            "INSERT INTO items (item, title, price, currency, timestamp, photo_url, query_id, brand) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (id, title, price, currency, timestamp, photo_url, query_id, brand),
        )
        # Update the last item for the query
        cursor.execute(
            "UPDATE queries SET last_item=? WHERE id=?", (timestamp, query_id)
        )
        conn.commit()
    except Exception as e:
        logger.error(f"db.add_item_to_db failed: {e}", exc_info=True)
        print_exc()
    finally:
        if conn:
            conn.close()


def get_queries():
    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor()
        cursor.execute("SELECT id, query, last_item, query_name FROM queries")
        return cursor.fetchall()
    except Exception as e:
        logger.error(f"db.get_queries failed: {e}", exc_info=True)
        print_exc()
    finally:
        if conn:
            conn.close()


def is_query_in_db(processed_query):
    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor()
        # replace spaces in searched_text by % to match any query containing the searched text

        cursor.execute(
            "SELECT COUNT() FROM queries WHERE query = ?", (processed_query,)
        )
        if cursor.fetchone()[0]:
            return True
        return False
    except Exception as e:
        logger.error(f"db.is_query_in_db failed: {e}", exc_info=True)
        print_exc()
        return False
    finally:
        if conn:
            conn.close()


def add_query_to_db(query, name=None):
    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor()
        if name:
            cursor.execute(
                "INSERT INTO queries (query, last_item, query_name) VALUES (?, NULL, ?)",
                (query, name),
            )
        else:
            cursor.execute(
                "INSERT INTO queries (query, last_item) VALUES (?, NULL)", (query,)
            )
        conn.commit()
    except Exception as e:
        logger.error(f"db.add_query_to_db failed: {e}", exc_info=True)
        print_exc()
    finally:
        if conn:
            conn.close()


def get_query_id_by_rowid(rowid):
    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor()
        query = f"SELECT id FROM (SELECT id, ROW_NUMBER() OVER (ORDER BY ROWID) rn FROM queries) t WHERE rn={rowid}"
        cursor.execute(query)
        result = cursor.fetchone()
        if result:
            return result[0]
        return None
    except Exception as e:
        logger.error(f"db.get_query_id_by_rowid failed: {e}", exc_info=True)
        print_exc()
        return None
    finally:
        if conn:
            conn.close()


def remove_query_from_db(query_number):
    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor()
        # Delete items associated with this query using query_id
        cursor.execute("DELETE FROM items WHERE query_id=?", (query_number,))
        # Delete the query
        cursor.execute("DELETE FROM queries WHERE id=?", (query_number,))
        conn.commit()
    except Exception as e:
        logger.error(f"db.remove_query_from_db failed: {e}", exc_info=True)
        print_exc()
    finally:
        if conn:
            conn.close()


def remove_all_queries_from_db():
    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor()
        # Delete all items first to maintain foreign key integrity
        cursor.execute("DELETE FROM items")
        # Then delete all queries
        cursor.execute("DELETE FROM queries")
        conn.commit()
    except Exception as e:
        logger.error(f"db.remove_all_queries_from_db failed: {e}", exc_info=True)
        print_exc()
    finally:
        if conn:
            conn.close()


def update_query_in_db(query_id, query, name):
    """
    Update an existing query in the database.

    Args:
        query_id (int): The ID of the query to update
        query (str): The new query URL
        name (str, optional): The new name for the query

    Returns:
        bool: True if the query was updated successfully, False otherwise
    """
    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE queries SET query=?, query_name=? WHERE id=?",
            (query, name, query_id),
        )
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"db.update_query_in_db failed: {e}", exc_info=True)
        print_exc()
        return False
    finally:
        if conn:
            conn.close()


def add_to_allowlist(country):
    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO allowlist VALUES (?)", (country,))
        conn.commit()
    except Exception as e:
        logger.error(f"db.add_to_allowlist failed: {e}", exc_info=True)
        print_exc()
    finally:
        if conn:
            conn.close()


def remove_from_allowlist(country):
    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM allowlist WHERE country=?", (country,))
        conn.commit()
    except Exception as e:
        logger.error(f"db.remove_from_allowlist failed: {e}", exc_info=True)
        print_exc()
    finally:
        if conn:
            conn.close()


def get_allowlist():
    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM allowlist")
        # Get list of countries
        countries = [country[0] for country in cursor.fetchall()]
        # Return 0 if there are no countries in the allowlist
        if not countries:
            return 0
        return countries
    finally:
        if conn:
            conn.close()


def clear_allowlist():
    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM allowlist")
        conn.commit()
    except Exception as e:
        logger.error(f"db.clear_allowlist failed: {e}", exc_info=True)
        print_exc()
    finally:
        if conn:
            conn.close()


def get_parameter(key):
    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM parameters WHERE key=?", (key,))
        result = cursor.fetchone()
        return result[0] if result else None
    except Exception as e:
        logger.error(f"db.get_parameter failed: {e}", exc_info=True)
        print_exc()
    finally:
        if conn:
            conn.close()


def set_parameter(key, value):
    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor()
        cursor.execute("UPDATE parameters SET value=? WHERE key=?", (value, key))
        conn.commit()
    except Exception as e:
        logger.error(f"db.set_parameter failed: {e}", exc_info=True)
        print_exc()
    finally:
        if conn:
            conn.close()


def get_all_parameters():
    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM parameters")
        return {row[0]: row[1] for row in cursor.fetchall()}
    except Exception as e:
        logger.error(f"db.get_all_parameters failed: {e}", exc_info=True)
        print_exc()
        return {}
    finally:
        if conn:
            conn.close()


def get_items(limit=50, query=None):
    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor()
        if query:
            # Get the query_id for the given query
            cursor.execute("SELECT id FROM queries WHERE query=?", (query,))
            result = cursor.fetchone()
            if result:
                query_id = result[0]
                # Get items with the matching query_id
                cursor.execute(
                    "SELECT i.item, i.title, i.price, i.currency, i.timestamp, q.query, i.photo_url, q.query_name, i.brand FROM items i JOIN queries q ON i.query_id = q.id WHERE i.query_id=? ORDER BY i.timestamp DESC LIMIT ?",
                    (query_id, limit),
                )
            else:
                return []
        else:
            # Join with queries table to get the query text
            cursor.execute(
                "SELECT i.item, i.title, i.price, i.currency, i.timestamp, q.query, i.photo_url, q.query_name, i.brand FROM items i JOIN queries q ON i.query_id = q.id ORDER BY i.timestamp DESC LIMIT ?",
                (limit,),
            )
        return cursor.fetchall()
    except Exception as e:
        logger.error(f"db.get_items failed: {e}", exc_info=True)
        print_exc()
        return []
    finally:
        if conn:
            conn.close()


def get_total_items_count():
    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM items")
        return cursor.fetchone()[0]
    except Exception as e:
        logger.error(f"db.get_total_items_count failed: {e}", exc_info=True)
        print_exc()
        return 0
    finally:
        if conn:
            conn.close()


def get_total_queries_count():
    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM queries")
        return cursor.fetchone()[0]
    except Exception as e:
        logger.error(f"db.get_total_queries_count failed: {e}", exc_info=True)
        print_exc()
        return 0
    finally:
        if conn:
            conn.close()


def get_last_found_item():
    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT i.item, i.title, i.price, i.currency, i.timestamp, q.query, i.photo_url FROM items i JOIN queries q ON i.query_id = q.id ORDER BY i.timestamp DESC LIMIT 1"
        )
        return cursor.fetchone()
    except Exception as e:
        logger.error(f"db.get_last_found_item failed: {e}", exc_info=True)
        print_exc()
        return None
    finally:
        if conn:
            conn.close()


def get_items_per_day():
    conn = None
    try:
        conn = _connect()
        cursor = conn.cursor()

        # Get total items
        cursor.execute("SELECT COUNT(*) FROM items")
        total_items = cursor.fetchone()[0]

        if total_items == 0:
            return 0

        # Get earliest and latest timestamps
        cursor.execute("SELECT MIN(timestamp), MAX(timestamp) FROM items")
        min_timestamp, max_timestamp = cursor.fetchone()

        # Calculate number of days (add 1 to include both start and end days)
        import datetime

        min_date = datetime.datetime.fromtimestamp(min_timestamp).date()
        max_date = datetime.datetime.fromtimestamp(max_timestamp).date()
        days_diff = (max_date - min_date).days + 1

        # Ensure at least 1 day to avoid division by zero
        days_diff = max(1, days_diff)

        # Calculate items per day
        return round(total_items / days_diff, 1)
    except Exception as e:
        logger.error(f"db.get_items_per_day failed: {e}", exc_info=True)
        print_exc()
        return 0
    finally:
        if conn:
            conn.close()
