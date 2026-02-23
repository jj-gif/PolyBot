"""
database.py — SQLite persistence.

Tables:
  users        — registered Discord users with wallet addresses + virtual balances
  transactions — deposit/withdrawal/fee history per user
  tx_hashes    — seen blockchain tx hashes (prevents double-crediting)
  watches      — per-user market watches
  positions    — per-user open/closed trading positions
  btc_sessions — BTC 5m auto-trader config per user
"""
import sqlite3

DB_PATH = "polymarket_bot.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
        -- ── Users ──────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS users (
            discord_id       TEXT PRIMARY KEY,
            discord_name     TEXT NOT NULL,
            wallet_address   TEXT NOT NULL UNIQUE,
            virtual_balance  REAL NOT NULL DEFAULT 0.0,
            total_deposited  REAL NOT NULL DEFAULT 0.0,
            registered_at    TEXT DEFAULT (datetime('now')),
            is_active        INTEGER DEFAULT 1
        );

        -- ── Transactions ─────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS transactions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id   TEXT NOT NULL REFERENCES users(discord_id),
            type         TEXT NOT NULL,  -- 'deposit' | 'withdraw' | 'fee' | 'trade_debit' | 'trade_credit'
            amount       REAL NOT NULL,
            tx_hash      TEXT,
            note         TEXT,
            created_at   TEXT DEFAULT (datetime('now'))
        );

        -- ── Seen TX hashes (prevents double-crediting deposits) ───────────
        CREATE TABLE IF NOT EXISTS tx_hashes (
            tx_hash    TEXT PRIMARY KEY,
            created_at TEXT DEFAULT (datetime('now'))
        );

        -- ── Watches ─────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS watches (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id  TEXT NOT NULL REFERENCES users(discord_id),
            token_id    TEXT NOT NULL,
            question    TEXT NOT NULL,
            outcome     TEXT NOT NULL,
            buy_price   REAL NOT NULL,
            stop_loss   REAL,
            stop_pct    REAL,
            take_profit REAL,
            trade_size  REAL NOT NULL,
            status      TEXT DEFAULT 'watching',
            created_at  TEXT DEFAULT (datetime('now'))
        );

        -- ── BTC Sessions ─────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS btc_sessions (
            discord_id       TEXT PRIMARY KEY,
            buy_threshold    REAL NOT NULL,
            sell_threshold   REAL NOT NULL,
            dead_zone        REAL NOT NULL DEFAULT 0.03,
            base_size        REAL NOT NULL,
            multi_bet        INTEGER DEFAULT 0,
            add_pct          REAL DEFAULT 2.0,
            max_bets         INTEGER DEFAULT 5,
            full_auto        INTEGER DEFAULT 1,
            max_rounds       INTEGER DEFAULT 10,
            savings_wallet   TEXT DEFAULT '',
            created_at       TEXT DEFAULT (datetime('now'))
        );

        -- ── Positions ────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS positions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id   TEXT NOT NULL REFERENCES users(discord_id),
            watch_id     INTEGER REFERENCES watches(id),
            token_id     TEXT NOT NULL,
            question     TEXT NOT NULL,
            outcome      TEXT NOT NULL,
            avg_price    REAL NOT NULL,
            shares       REAL NOT NULL,
            cost_basis   REAL NOT NULL,
            order_id     TEXT,
            status       TEXT DEFAULT 'open',
            close_reason TEXT,
            opened_at    TEXT DEFAULT (datetime('now')),
            closed_at    TEXT
        );
        """)

        # ── Migrations: safely add new columns to existing databases ──────
        migrations = [
            "ALTER TABLE users ADD COLUMN virtual_balance REAL NOT NULL DEFAULT 0.0",
            "ALTER TABLE users ADD COLUMN total_deposited REAL NOT NULL DEFAULT 0.0",
        ]
        for sql in migrations:
            try:
                conn.execute(sql)
                conn.commit()
            except Exception:
                pass  # Column already exists — safe to ignore


# ── User CRUD ─────────────────────────────────────────────────────────────────

def register_user(discord_id: str, discord_name: str, wallet_address: str):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO users (discord_id, discord_name, wallet_address, encrypted_key)
            VALUES (?,?,?,'')
            ON CONFLICT(discord_id) DO UPDATE SET
                wallet_address = excluded.wallet_address,
                discord_name   = excluded.discord_name,
                is_active      = 1
        """, (discord_id, discord_name, wallet_address))


def get_user(discord_id: str):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE discord_id=? AND is_active=1",
            (discord_id,)
        ).fetchone()


def get_user_by_wallet(wallet_address: str):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE LOWER(wallet_address)=LOWER(?) AND is_active=1",
            (wallet_address,)
        ).fetchone()


def update_wallet(discord_id: str, new_address: str) -> bool:
    """Update a user's wallet address. Returns False if address is taken by another user."""
    # Check not already claimed by someone else
    existing = get_user_by_wallet(new_address)
    if existing and existing["discord_id"] != discord_id:
        return False
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET wallet_address=? WHERE discord_id=?",
            (new_address, discord_id)
        )
    return True


def get_all_users():
    with get_conn() as conn:
        return conn.execute(
            "SELECT discord_id, discord_name, wallet_address, "
            "virtual_balance, total_deposited, registered_at "
            "FROM users WHERE is_active=1 ORDER BY registered_at"
        ).fetchall()


def deregister_user(discord_id: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET is_active=0 WHERE discord_id=?",
            (discord_id,)
        )


# ── Balance operations ────────────────────────────────────────────────────────

def credit_balance(discord_id: str, amount: float, tx_hash: str = None,
                   note: str = "deposit"):
    """Add funds to a user's virtual balance."""
    with get_conn() as conn:
        conn.execute("""
            UPDATE users
            SET virtual_balance = virtual_balance + ?,
                total_deposited = total_deposited + ?
            WHERE discord_id = ?
        """, (amount, amount, discord_id))
        conn.execute("""
            INSERT INTO transactions (discord_id, type, amount, tx_hash, note)
            VALUES (?, 'deposit', ?, ?, ?)
        """, (discord_id, amount, tx_hash, note))
        if tx_hash:
            conn.execute(
                "INSERT OR IGNORE INTO tx_hashes (tx_hash) VALUES (?)",
                (tx_hash,)
            )


def debit_balance(discord_id: str, amount: float, note: str = "trade"):
    """Deduct funds from a user's virtual balance. Returns False if insufficient."""
    with get_conn() as conn:
        user = conn.execute(
            "SELECT virtual_balance FROM users WHERE discord_id=?",
            (discord_id,)
        ).fetchone()
        if not user or user["virtual_balance"] < amount:
            return False
        conn.execute("""
            UPDATE users SET virtual_balance = virtual_balance - ?
            WHERE discord_id = ?
        """, (amount, discord_id))
        conn.execute("""
            INSERT INTO transactions (discord_id, type, amount, note)
            VALUES (?, 'trade_debit', ?, ?)
        """, (discord_id, amount, note))
        return True


def credit_trade_profit(discord_id: str, amount: float, note: str = "trade profit"):
    """Credit trade winnings back to virtual balance (no deposit total change)."""
    with get_conn() as conn:
        conn.execute("""
            UPDATE users SET virtual_balance = virtual_balance + ?
            WHERE discord_id = ?
        """, (amount, discord_id))
        conn.execute("""
            INSERT INTO transactions (discord_id, type, amount, note)
            VALUES (?, 'trade_credit', ?, ?)
        """, (discord_id, amount, note))


def process_withdrawal(discord_id: str, amount: float) -> dict:
    """
    Calculate withdrawal with 2% fee on profits only.
    Users can never withdraw more than their current virtual_balance.
    Returns { net_amount, fee, ok, reason }
    """
    with get_conn() as conn:
        user = conn.execute(
            "SELECT virtual_balance, total_deposited FROM users WHERE discord_id=?",
            (discord_id,)
        ).fetchone()

        if not user:
            return {"ok": False, "reason": "User not found"}

        bal       = round(user["virtual_balance"], 4)
        deposited = round(user["total_deposited"], 4)

        # Round requested amount to 2 decimal places to prevent float tricks
        amount = round(amount, 2)

        if amount < 1.0:
            return {"ok": False, "reason": "Minimum withdrawal is $1.00"}

        # Hard cap — cannot withdraw more than current balance, ever
        if amount > bal:
            return {
                "ok":     False,
                "reason": f"Amount exceeds your balance. Maximum you can withdraw: `${bal:.2f}`"
            }

        # Calculate profit portion (balance above what was deposited)
        profit_portion   = max(0.0, bal - deposited)
        withdrawn_profit = min(amount, profit_portion)
        fee = round(withdrawn_profit * 0.02, 4)
        net = round(amount - fee, 4)

        # Deduct from virtual balance — cannot go below 0
        new_balance = round(max(0.0, bal - amount), 4)
        # Reduce total_deposited by non-profit portion withdrawn
        non_profit_withdrawn = amount - withdrawn_profit
        new_deposited = round(max(0.0, deposited - non_profit_withdrawn), 4)

        conn.execute("""
            UPDATE users
            SET virtual_balance = ?,
                total_deposited = ?
            WHERE discord_id = ?
        """, (new_balance, new_deposited, discord_id))

        conn.execute("""
            INSERT INTO transactions (discord_id, type, amount, note)
            VALUES (?, 'withdraw', ?, ?)
        """, (discord_id, amount, f"Withdrew ${amount:.2f}, fee=${fee:.4f}"))

        if fee > 0:
            conn.execute("""
                INSERT INTO transactions (discord_id, type, amount, note)
                VALUES (?, 'fee', ?, '2% profit fee on withdrawal')
            """, (discord_id, fee))

        return {"ok": True, "net_amount": net, "fee": fee, "gross": amount}


def is_tx_seen(tx_hash: str) -> bool:
    with get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM tx_hashes WHERE tx_hash=?", (tx_hash,)
        ).fetchone() is not None


def get_transaction_history(discord_id: str, limit: int = 10):
    with get_conn() as conn:
        return conn.execute("""
            SELECT type, amount, note, tx_hash, created_at
            FROM transactions WHERE discord_id=?
            ORDER BY created_at DESC LIMIT ?
        """, (discord_id, limit)).fetchall()


# ── Watch CRUD ────────────────────────────────────────────────────────────────

def add_watch(discord_id, token_id, question, outcome,
              buy_price, stop_loss, stop_pct, take_profit, trade_size):
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO watches
                (discord_id, token_id, question, outcome, buy_price,
                 stop_loss, stop_pct, take_profit, trade_size)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (discord_id, token_id, question, outcome, buy_price,
              stop_loss, stop_pct, take_profit, trade_size))
        return cur.lastrowid


def get_active_watches():
    with get_conn() as conn:
        return conn.execute("""
            SELECT w.*, u.wallet_address, u.virtual_balance
            FROM watches w
            JOIN users u ON u.discord_id = w.discord_id
            WHERE w.status = 'watching' AND u.is_active = 1
        """).fetchall()


def get_user_watches(discord_id: str):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM watches WHERE discord_id=? ORDER BY created_at DESC LIMIT 20",
            (discord_id,)
        ).fetchall()


def mark_watch_filled(watch_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE watches SET status='filled' WHERE id=?", (watch_id,))


def remove_watch(discord_id: str, watch_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE watches SET status='closed' WHERE id=? AND discord_id=?",
            (watch_id, discord_id)
        )


# ── Position CRUD ─────────────────────────────────────────────────────────────

def open_position(discord_id, watch_id, token_id, question,
                  outcome, avg_price, shares, order_id):
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO positions
                (discord_id, watch_id, token_id, question, outcome,
                 avg_price, shares, cost_basis, order_id)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (discord_id, watch_id, token_id, question, outcome,
              avg_price, shares, round(avg_price * shares, 4), order_id))
        return cur.lastrowid


def get_open_positions():
    with get_conn() as conn:
        return conn.execute("""
            SELECT p.*, u.wallet_address, u.virtual_balance
            FROM positions p
            JOIN users u ON u.discord_id = p.discord_id
            WHERE p.status = 'open' AND u.is_active = 1
        """).fetchall()


def get_user_positions(discord_id: str):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM positions WHERE discord_id=? AND status='open'",
            (discord_id,)
        ).fetchall()


def close_position(position_id: int, reason: str):
    with get_conn() as conn:
        conn.execute("""
            UPDATE positions
            SET status='closed', close_reason=?, closed_at=datetime('now')
            WHERE id=?
        """, (reason, position_id))