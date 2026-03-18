from psycopg import Connection

from app.repositories.transactions_repository import (
    find_transaction_feed_item_by_external_id,
    list_transaction_feed,
)


def fetch_transaction_feed(conn: Connection, params: dict) -> tuple[list[dict], int]:
    return list_transaction_feed(conn, params)


def fetch_transaction_feed_item(
    conn: Connection, *, user_id: str, transaction_id: str
) -> dict | None:
    return find_transaction_feed_item_by_external_id(
        conn, user_id=user_id, transaction_id=transaction_id
    )
